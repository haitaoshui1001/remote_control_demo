"""摄像头远程配置服务：经 ONVIF 读/改编码参数（分辨率、帧率、码率）。

ONVIF 是阻塞的 HTTP/SOAP 调用，且请求要穿 EWD611 空口桥，一次往返可能
秒级——所以每个动作放独立工作线程跑，结果统一发布到
``Events.CAMERA_CFG`` 主题；界面只订阅总线，不直接等网络。

同步方法 ``load()`` / ``apply()`` 一并保留：自检脚本和 pytest 直接调它们，
不必起线程。合法值域永远来自设备自报的 Options，写入前先 ``clamped()``
过滤，界面拿到的是"设备答应的值"而不是"用户填的值"。
"""

from __future__ import annotations

import base64
import logging
import re
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from ..core.config import AppConfig, VideoCfg
from ..core.events import EventBus, Events, get_bus
from ..protocol.onvif import (
    EncoderOptions,
    EncoderSettings,
    OnvifClient,
    OnvifError,
)

__all__ = ["CameraConfigService", "CameraParams", "CameraProbe", "compose_rtsp_url"]

_LOG = logging.getLogger("hmi.camera")

#: 连接参数保存时允许覆盖的字段白名单——别把 buffer_ms 这类渲染参数一起冲掉。
_CONN_FIELDS = (
    "camera_ip",
    "rtsp_port",
    "onvif_port",
    "user",
    "password",
    "channel",
    "stream_index",
    "url",
    "stream_paths",
    "transport",
)


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


@dataclass
class CameraProbe:
    """一次连通性自检的结果：RTSP 决定"能不能出画面"，ONVIF 决定"能不能改参数"。"""

    ok: bool = False
    action: str = "probe"
    stream_index: int = 0
    rtsp_url: str = ""
    rtsp_ok: bool = False
    rtsp_detail: str = ""
    onvif_ok: bool = False
    onvif_detail: str = ""
    auth_mode: str = ""
    error: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "rtsp_ok": self.rtsp_ok,
            "rtsp_detail": self.rtsp_detail,
            "onvif_ok": self.onvif_ok,
            "onvif_detail": self.onvif_detail,
            "stream": self.stream_index,
        }


def compose_rtsp_url(v: VideoCfg, stream_index: int | None = None) -> str:
    """按 IP / RTSP 端口 / 账号 / 码流路径拼一条取流地址（转调 ``VideoCfg.composed_url``）。

    真正的"生效地址"永远以 ``v.resolved_url(idx)`` 为准——这里只是它的一个分支，
    留着是给界面当"自动拼装"提示用，别再单独发展第二套拼法。
    """
    return v.composed_url(stream_index)


def _strip_credentials(url: str) -> str:
    """把 ``rtsp://user:pwd@host/path`` 里的账号段去掉——请求行不该带凭据。"""
    parts = urllib.parse.urlsplit(url)
    if not parts.username:
        return url
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _rtsp_frame(url: str, method: str, cseq: int, user: str, password: str, extra: str = "") -> bytes:
    lines = [f"{method} {url} RTSP/1.0", f"CSeq: {cseq}", "User-Agent: EWD611-HMI"]
    if extra:
        lines.append(extra)
    if user:
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        lines.append(f"Authorization: Basic {token}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii", "ignore")


def _rtsp_status(head: str) -> int:
    first = head.splitlines()[0] if head else ""
    m = re.search(r"RTSP/\d(?:\.\d)?\s+(\d{3})", first)
    return int(m.group(1)) if m else 0


def _read_rtsp_reply(sock: socket.socket, deadline: float) -> tuple[str, bytes]:
    """读到"头部 + Content-Length 声明的正文"齐了为止；超时返回空串。"""
    buf = bytearray()
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(4096)
        except (TimeoutError, socket.timeout, OSError):
            break
        if not chunk:
            break
        buf.extend(chunk)
        text = bytes(buf)
        cut = text.find(b"\r\n\r\n")
        if cut < 0:
            continue
        head = text[:cut]
        length = 0
        for line in head.split(b"\r\n")[1:]:
            key, _, value = line.partition(b":")
            if key.strip().lower() == b"content-length":
                try:
                    length = int(value.strip())
                except ValueError:
                    length = 0
        if len(text) - cut - 4 >= length:
            return head.decode("latin-1"), text[cut + 4:]
    return "", b""


def _sdp_detail(sdp: str) -> str:
    videos = len(re.findall(r"^m=video", sdp, flags=re.M))
    out = f"DESCRIBE 正常：视频 {videos or 1} 路"
    m = re.search(r"x-dimension:\s*(\d+)\s*[*xX]\s*(\d+)(?:\s*@\s*#?\s*(\d+))?", sdp)
    if m:
        out += f"，{m.group(1)}x{m.group(2)}"
        if m.group(3):
            out += f"@{m.group(3)}fps"
    else:
        fps = re.search(r"^a=framerate:\s*([\d.]+)", sdp, flags=re.M)
        if fps:
            out += f"，{fps.group(1)}fps"
    return out


def _probe_rtsp(url: str, *, timeout_s: float = 5.0) -> tuple[bool, str]:
    """不带解码器的 RTSP 自检：TCP 通 → OPTIONS → DESCRIBE，回「能不能取流 + 人话说明」。

    为什么不用 PyAV 试开一次：解码器一开就是几秒的缓冲与重试，还会和正在跑的取流
    线程抢设备连接。OPTIONS/DESCRIBE 两条明文请求足够区分"IP 不通 / 端口不对 /
    账号密码错 / 推流路径不存在"这四类真实故障，界面上才给得出可执行的下一步。
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("rtsp", "rtspu") or not parts.hostname:
        return False, "地址格式不对：应为 rtsp://账号:密码@IP:端口/推流路径"
    host = parts.hostname
    port = int(parts.port or 554)
    target = _strip_credentials(url)
    user = urllib.parse.unquote(parts.username or "")
    password = urllib.parse.unquote(parts.password or "")
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    try:
        sock = socket.create_connection((host, port), timeout=max(0.5, deadline - time.monotonic()))
    except OSError as exc:
        return False, f"RTSP 端口连不上（{host}:{port}，{exc}）：核对 IP、RTSP 端口，或摄像头是否在线"
    try:
        sock.sendall(_rtsp_frame(target, "OPTIONS", 1, user, password))
        head, _ = _read_rtsp_reply(sock, deadline)
        if not head:
            return False, f"{host}:{port} 能连上但没应答：那上面可能不是 RTSP 服务"
        status = _rtsp_status(head)
        if status in (401, 403):
            return False, f"OPTIONS 回 {status}：账号或密码不对（当前账号 {user or '空'}）"
        if status != 200:
            return False, f"OPTIONS 回 {status}：设备拒绝该请求"
        sock.sendall(
            _rtsp_frame(target, "DESCRIBE", 2, user, password, extra="Accept: application/sdp")
        )
        head, body = _read_rtsp_reply(sock, deadline)
        if not head:
            return False, "OPTIONS 正常但 DESCRIBE 无应答（设备忙或固件限制）"
        status = _rtsp_status(head)
        if status == 404:
            return False, "DESCRIBE 回 404：推流路径不存在，核对主/子码流路径（如 stream1/stream2）"
        if status != 200:
            return False, f"DESCRIBE 回 {status}：地址能到但这条流打不开"
        return True, _sdp_detail(body.decode("latin-1", "ignore"))
    finally:
        try:
            sock.close()
        except OSError:
            pass


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
    def _client(self, v: VideoCfg | None = None) -> OnvifClient:
        """按配置（或对话框里的副本）现建一个 ONVIF 客户端。"""
        v = self.cfg.video if v is None else v
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

    def probe(self, v: VideoCfg | None = None, stream_index: int | None = None) -> CameraProbe:
        """连通性自检：RTSP 能不能取流 + ONVIF 能不能改参数，两项独立判定。

        传 ``v``（对话框里的副本）时按"还没保存的参数"探测，用户不必先保存再测；
        RTSP 走明文 OPTIONS/DESCRIBE，不碰正在跑的取流线程。
        """
        vv = self.cfg.video if v is None else v
        idx = int(vv.stream_index if stream_index is None else stream_index)
        url = str(vv.resolved_url(idx) or "").strip()
        res = CameraProbe(stream_index=idx, rtsp_url=url)
        if not url or not urllib.parse.urlsplit(url).hostname:
            res.error = "取流地址是空的：先填摄像头 IP 和主/子码流路径"
            res.rtsp_detail = res.error
            return res
        try:
            res.rtsp_ok, res.rtsp_detail = _probe_rtsp(url, timeout_s=self.timeout_s)
        except Exception as exc:  # noqa: BLE001 - 探测炸了也要给界面一句话
            self.log.exception("RTSP 探测异常")
            res.rtsp_detail = f"RTSP 探测异常：{type(exc).__name__}: {exc}"
        if not str(vv.camera_ip or "").strip():
            res.onvif_detail = "未填摄像头 IP，跳过 ONVIF 检查"
        else:
            try:
                cli = self._client(vv)
                info = cli.get_device_information()
                res.onvif_ok = True
                res.auth_mode = cli.auth_mode
                tags = [str(info.get(k) or "").strip() for k in ("manufacturer", "model", "serial")]
                res.onvif_detail = "ONVIF 正常：" + " / ".join(t for t in tags if t)
            except OnvifError as exc:
                res.onvif_detail = f"ONVIF 不可用：{exc}"
            except Exception as exc:  # noqa: BLE001 - 同上
                self.log.exception("ONVIF 探测异常")
                res.onvif_detail = f"ONVIF 探测异常：{type(exc).__name__}: {exc}"
        res.ok = res.rtsp_ok
        if not res.ok:
            res.error = res.rtsp_detail
        self.log.info("连接自检：rtsp=%s onvif=%s", res.rtsp_ok, res.onvif_ok)
        return res

    def apply_connection(self, snapshot: VideoCfg, *, reconnect: bool = True) -> dict[str, Any]:
        """把连接参数写回 ``video`` 段并落盘，随后让新地址立刻生效。

        只覆盖白名单字段：``buffer_ms`` 这类渲染参数不归这个对话框管。
        生效必须走 ``video.set_stream``——取流源的地址在建立时就读死了，
        光 ``restart()`` 仍旧按旧 IP / 旧路径拉流。
        """
        v = self.cfg.video
        ip = str(snapshot.camera_ip or "").strip()
        manual = str(snapshot.url or "").strip()
        if not ip and not manual:
            return {"ok": False, "detail": "摄像头 IP 和取流地址都是空的：至少填一项"}
        for name in _CONN_FIELDS:
            if not hasattr(snapshot, name):
                continue
            value = getattr(snapshot, name)
            setattr(v, name, list(value) if isinstance(value, list) else value)
        v.stream_index = min(max(int(v.stream_index), 0), 1)
        saved = ""
        try:
            saved = str(self.cfg.save())
        except OSError as exc:
            self.log.warning("连接参数已应用到内存，但写盘失败：%s", exc)
        out: dict[str, Any] = {
            "ok": True,
            "stream_index": int(v.stream_index),
            "url": v.resolved_url(),
            "detail": "已写回 %s" % saved if saved else "写盘失败，本次仅内存生效",
        }
        if reconnect and self.video is not None:
            try:
                r = self.video.set_stream(int(v.stream_index), persist=False)
            except Exception as exc:  # noqa: BLE001 - 切流失败不回滚配置
                self.log.exception("按新地址重连异常")
                r = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
            out["reconnected"] = bool(r.get("ok"))
            out["detail"] += (
                "，取流已按新地址重连"
                if r.get("ok") and getattr(self.video, "running", False)
                else "，下次启动取流按新地址生效"
            )
        self.log.info("摄像头连接参数已保存：%s", out["url"])
        return out

    # ------------------------------------------------------------------ 异步入口
    def _spawn(self, fn, *args: Any, topic: str = Events.CAMERA_CFG) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._thread = threading.Thread(
                target=self._run, args=(fn, args, topic), daemon=True,
                name="camera-config",
            )
            self._thread.start()
            return True

    def _run(self, fn, args: tuple[Any, ...], topic: str = Events.CAMERA_CFG) -> None:
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
        self.bus.publish(topic, result)

    def load_async(self, stream_index: int) -> bool:
        """工作线程里跑 ``load()``，结果发总线；忙时返回 False。"""
        return self._spawn(self.load, stream_index)

    def apply_async(self, st: EncoderSettings) -> bool:
        """工作线程里跑 ``apply()``，结果发总线；忙时返回 False。"""
        return self._spawn(self.apply, st)

    def probe_async(self, v: VideoCfg | None = None, stream_index: int | None = None) -> bool:
        """工作线程里跑 ``probe()``，结果发 ``Events.CAMERA_PROBE``；忙时返回 False。"""
        return self._spawn(self.probe, v, stream_index, topic=Events.CAMERA_PROBE)

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
