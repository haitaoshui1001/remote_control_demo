"""摄像头远程配置服务：经 ONVIF 读/改编码参数（分辨率、帧率、码率）。

ONVIF 是阻塞的 HTTP/SOAP 调用，且请求要穿 EWD611 空口桥，一次往返可能
秒级——所以每个动作放独立工作线程跑，结果统一发布到
``Events.CAMERA_CFG`` 主题；界面只订阅总线，不直接等网络。

同步方法 ``load()`` / ``apply()`` 一并保留：自检脚本和 pytest 直接调它们，
不必起线程。合法值域永远来自设备自报的 Options，写入前先 ``clamped()``
过滤，界面拿到的是"设备答应的值"而不是"用户填的值"。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from ..core.config import AppConfig
from ..core.events import EventBus, Events, get_bus
from ..protocol.onvif import (
    EncoderOptions,
    EncoderSettings,
    OnvifClient,
    OnvifError,
)

__all__ = ["CameraConfigService", "CameraParams"]

_LOG = logging.getLogger("hmi.camera")


@dataclass
class CameraParams:
    """一次读/写动作的结果；``ok=False`` 时 ``error`` 可直接上界面。"""

    ok: bool
    action: str = "load"
    stream_index: int = 0
    error: str = ""
    auth_mode: str = ""
    profile_name: str = ""
    current: EncoderSettings | None = None
    options: EncoderOptions | None = None
    applied: EncoderSettings | None = None
    fixed: tuple[str, ...] = field(default_factory=tuple)

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "stream": self.stream_index,
            "error": self.error,
            "profile": self.profile_name,
            "fixed": list(self.fixed),
        }


class CameraConfigService:
    """持有一个按 IP 直连的 ONVIF 客户端；同一时刻只允许一个在途动作。"""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        bus: EventBus | None = None,
        video: Any = None,
        log: logging.Logger | None = None,
        timeout_s: float = 8.0,
    ) -> None:
        self.cfg = cfg
        self.bus = bus if bus is not None else get_bus()
        self.video = video
        self.log = log or _LOG
        self.timeout_s = float(timeout_s)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 客户端
    def _client(self) -> OnvifClient:
        v = self.cfg.video
        if not v.camera_ip:
            raise OnvifError("video.camera_ip 为空，无法直连摄像头")
        port = int(getattr(v, "onvif_port", 80) or 80)
        return OnvifClient(
            v.camera_ip,
            port=port,
            user=v.user,
            password=v.password,
            timeout_s=self.timeout_s,
        )

    # ------------------------------------------------------------------ 同步动作
    def load(self, stream_index: int) -> CameraParams:
        """读指定码流档的当前参数与合法值域。"""
        idx = int(stream_index)
        try:
            cli = self._client()
            profiles = cli.get_profiles()
            if not profiles:
                raise OnvifError("设备没有返回任何 Profile")
            if not 0 <= idx < len(profiles):
                raise OnvifError(
                    f"码流序号 {idx} 越界（设备共 {len(profiles)} 档）"
                )
            p = profiles[idx]
            if not p.encoder_token:
                raise OnvifError(f"Profile[{p.name or p.token}] 无视频编码引用")
            current = cli.get_encoder_configuration(p.encoder_token)
            options = cli.get_encoder_options(p.encoder_token)
            return CameraParams(
                ok=True,
                action="load",
                stream_index=idx,
                auth_mode=cli.auth_mode,
                profile_name=p.name or p.token,
                current=current,
                options=options,
            )
        except OnvifError as exc:
            self.log.warning("摄像头参数读取失败（流 %s）：%s", idx, exc)
            return CameraParams(
                ok=False, action="load", stream_index=idx, error=str(exc)
            )
        except Exception as exc:  # noqa: BLE001 - 界面必须拿到结果，不能卡住
            # 非协议异常（如冻结包缺 XML 解析器的 ImportError）也必须落成
            # 一条可读结果，否则工作线程静默死亡、弹窗永久"读取中"。
            self.log.exception("摄像头参数读取异常（流 %s）", idx)
            return CameraParams(
                ok=False,
                action="load",
                stream_index=idx,
                error=f"{type(exc).__name__}: {exc}",
            )

    def apply(self, st: EncoderSettings) -> CameraParams:
        """按值域过滤后写入；成功则重启取流让新参数生效。"""
        try:
            cli = self._client()
            options = cli.get_encoder_options(st.encoder_token)
            st, fixed = st.clamped(options)
            cli.set_encoder_configuration(st)
        except OnvifError as exc:
            self.log.warning("摄像头参数写入失败：%s", exc)
            return CameraParams(ok=False, action="apply", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - 同 load()，异常必须落成结果
            self.log.exception("摄像头参数写入异常")
            return CameraParams(
                ok=False, action="apply", error=f"{type(exc).__name__}: {exc}"
            )
        if self.video is not None:
            try:
                self.video.restart()
            except Exception as exc:  # noqa: BLE001 - 重启失败不回滚配置
                self.log.warning("参数已写入但取流重启失败：%s", exc)
        self.log.info(
            "摄像头参数已生效：%s %sx%s %sfps %skbps",
            st.encoding, st.width, st.height, st.frame_rate, st.bitrate_kbps,
        )
        return CameraParams(
            ok=True,
            action="apply",
            auth_mode=cli.auth_mode,
            applied=st,
            fixed=tuple(fixed),
        )

    # ------------------------------------------------------------------ 异步入口
    def _spawn(self, fn, *args: Any) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._thread = threading.Thread(
                target=self._run, args=(fn, args), daemon=True,
                name="camera-config",
            )
            self._thread.start()
            return True

    def _run(self, fn, args: tuple[Any, ...]) -> None:
        """在工作线程里跑一个动作；无论成功、失败还是炸了都必须回总线。"""
        try:
            result = fn(*args)
        except Exception as exc:  # noqa: BLE001 - 线程静默死亡 = 界面永久等待
            self.log.exception("摄像头配置线程异常终止")
            result = CameraParams(
                ok=False,
                action=getattr(fn, "__name__", "load"),
                error=f"{type(exc).__name__}: {exc}",
            )
        self.bus.publish(Events.CAMERA_CFG, result)

    def load_async(self, stream_index: int) -> bool:
        """工作线程里跑 ``load()``，结果发总线；忙时返回 False。"""
        return self._spawn(self.load, stream_index)

    def apply_async(self, st: EncoderSettings) -> bool:
        """工作线程里跑 ``apply()``，结果发总线；忙时返回 False。"""
        return self._spawn(self.apply, st)

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def summary(self) -> dict[str, Any]:
        v = self.cfg.video
        return {
            "ip": v.camera_ip,
            "port": int(getattr(v, "onvif_port", 80) or 80),
            "busy": self.busy,
        }
