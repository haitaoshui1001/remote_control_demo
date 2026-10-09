"""视频服务：装配取流源、发布指标、热切主/子码流、抓拍。

依赖方向上这一层只做"装配 + 发布"，不碰解码细节：真实 ``RtspVideoSource``
与仿真 ``MockVideoSource`` 接口一致，配置里的 ``app.mock`` 决定用哪个，
上层（UI / 自检）拿到的都是同一个 ``VideoService``。

帧本体**不走事件总线**：25 fps 的事件会把总线和 UI 队列打满。帧一律经
``FrameChannel``（只留最新一帧）由界面定时器拉取，总线只发秒级的
``VIDEO_STATS`` 和状态变化 ``TRANSPORT_STATE``。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from ..core.config import AppConfig
from ..core.events import EventBus, Events, get_bus
from ..core.logger import get_logger
from ..core.models import AlarmEvent, AlarmLevel, FrameMeta, TransportState, VideoStats
from ..mock.video import MockVideoSource
from ..transport.base import FrameChannel
from ..transport.rtsp import RtspVideoSource
from .periodic import Ticker

StreamSource = RtspVideoSource | MockVideoSource


def as_rgb_array(image: Any) -> Any:
    """把一帧统一成 ``(h, w, 3)`` 的 uint8 数组，抓拍与录像共用这一份归一化。"""
    import numpy as np

    arr = np.asarray(image, dtype="uint8")
    if arr.ndim == 2:
        arr = np.repeat(arr[:, :, None], 3, axis=2)
    return arr


def save_frame(image: Any, path: Path) -> Path:
    """把一帧 RGB 数组存成图片；没有 Pillow 时退回避难所（自检仍能通过）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
    except ImportError:
        import numpy as np

        path = path.with_suffix(".npy")
        np.save(path, as_rgb_array(image))
        return path
    Image.fromarray(as_rgb_array(image)).save(path)
    return path


class VideoService:
    """一个摄像头对应一个视频服务；起停、换码流、抓拍都在这里收口。"""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        bus: EventBus | None = None,
        source: StreamSource | None = None,
        log: logging.Logger | None = None,
        stats_interval_s: float = 1.0,
        stall_alarm_ms: float = 5000.0,
    ) -> None:
        self.cfg = cfg
        self.bus = bus if bus is not None else get_bus()
        self.log = log or get_logger("hmi.services.video")
        self.stats_interval_s = max(0.2, float(stats_interval_s))
        self.stall_alarm_ms = max(500.0, float(stall_alarm_ms))
        if source is None:
            self.mock = bool(cfg.app.mock)
        else:
            self.mock = not isinstance(source, RtspVideoSource)
        self._source: StreamSource = source or self._build_source()
        self._lock = threading.Lock()
        self._stall_reported = False
        self._start_error = ""
        self._snapshots = 0
        self._ticker = Ticker(
            f"{self.name}-stats", self.publish_stats, self.stats_interval_s, log=self.log
        )
        self._source.add_state_listener(self._on_state)

    # ------------------------------------------------------------------ 装配
    def _build_source(self) -> StreamSource:
        if self.mock:
            return MockVideoSource(self.cfg.video, name="mockcam")
        return RtspVideoSource(self.cfg.video, name="rtsp")

    @property
    def source(self) -> StreamSource:
        return self._source

    @property
    def frames(self) -> FrameChannel:
        return self._source.frames

    @property
    def name(self) -> str:
        return str(getattr(self._source, "name", "video"))

    # ------------------------------------------------------------------ 起停
    def start(self) -> bool:
        """开始取流；失败发告警并返回 False（按钮不该弹异常框）。"""
        try:
            self._source.start()
        except (RuntimeError, OSError, ValueError) as exc:
            self._start_error = str(exc)
            self.log.error("视频启动失败：%s", exc)
            self._alarm("VIDEO_START_FAILED", str(exc))
            return False
        self._start_error = ""
        self._ticker.start()
        return True

    def stop(self) -> None:
        self._ticker.stop(timeout=max(1.0, self.stats_interval_s + 0.5))
        try:
            self._source.stop()
        except Exception as exc:
            self.log.debug("视频停止异常（忽略）：%s", exc)
        with self._lock:
            self._stall_reported = False

    def restart(self) -> bool:
        self.stop()
        time.sleep(0.05)
        return self.start()

    @property
    def running(self) -> bool:
        return self.state is TransportState.OPEN

    @property
    def connected(self) -> bool:
        return self.stats().connected

    @property
    def state(self) -> TransportState:
        return TransportState(getattr(self._source, "state", TransportState.CLOSED))

    # ------------------------------------------------------------------ 指标
    def stats(self) -> VideoStats:
        return self._source.stats()

    def info(self) -> dict[str, Any]:
        data = dict(self._source.info())
        data["mock"] = self.mock
        data["start_error"] = self._start_error
        data["snapshots"] = self._snapshots
        return data

    def publish_stats(self) -> VideoStats:
        """发一次 ``VIDEO_STATS``，并检查"连着但没画面"的卡流情形。"""
        st = self.stats()
        if self.bus is not None:
            self.bus.publish(Events.VIDEO_STATS, st)
        self.check_stall(st)
        return st

    def check_stall(self, st: VideoStats | None = None) -> bool:
        """链路显示已连接却长时间无新帧：只报一次，恢复后再允许报下一轮。"""
        s = st if st is not None else self.stats()
        stalled = bool(
            s.connected and s.last_frame_ts and s.last_frame_age_ms >= self.stall_alarm_ms
        )
        with self._lock:
            first = stalled and not self._stall_reported
            if stalled:
                self._stall_reported = True
            else:
                self._stall_reported = False
        if first:
            age_s = s.last_frame_age_ms / 1000.0
            self._alarm(
                "VIDEO_STALL",
                f"视频已连接但 {age_s:.1f}s 无新帧（{s.resolution or '未知分辨率'}）",
            )
        return stalled

    # ------------------------------------------------------------------ 码流
    @property
    def stream_index(self) -> int:
        return int(self.cfg.video.stream_index)

    def url_for(self, stream_index: int) -> str:
        return self.cfg.rtsp_url(stream_index)

    def set_stream(self, stream_index: int, *, persist: bool = True) -> dict[str, Any]:
        """热切主/子码流。0 = 主码流，1 = 子码流（默认子码流，带宽优先）。"""
        idx = int(stream_index)
        if idx not in (0, 1):
            return {
                "ok": False,
                "stream_index": self.stream_index,
                "detail": "码流序号只能是 0 或 1",
            }
        url = self.url_for(idx)
        if persist:
            self.cfg.video.stream_index = idx
        try:
            self._source.switch_stream(idx, url)
        except Exception as exc:
            self.log.error("切换码流失败：%s", exc)
            return {"ok": False, "stream_index": self.stream_index, "detail": str(exc)}
        self.log.info("切换到%s码流：%s", "主" if idx == 0 else "子", url)
        return {"ok": True, "stream_index": idx, "url": url, "detail": "已切换"}

    def toggle_stream(self) -> dict[str, Any]:
        return self.set_stream(0 if self.stream_index == 1 else 1)

    # ------------------------------------------------------------------ 传输层
    @property
    def transport(self) -> str:
        return str(self.cfg.video.transport or "tcp")

    def set_transport(self, mode: str, *, persist: bool = True) -> dict[str, Any]:
        """切换 RTSP 取流传输：tcp 稳妥，udp 延迟更低但图传丢包时可能花屏。

        源每次起流现读 cfg，取流中就重启一次让新参数立刻生效；persist 再把
        结果写回 app.yaml，重启软件后仍是这个选择。
        """
        t = str(mode).strip().lower()
        if t not in ("tcp", "udp"):
            return {
                "ok": False,
                "transport": self.transport,
                "detail": "取流传输只能是 tcp 或 udp",
            }
        old = self.transport
        changed = t != old
        was_running = self.running
        self.cfg.video.transport = t
        restarted = False
        if changed and was_running:
            restarted = self.restart()
        saved = ""
        if persist:
            try:
                saved = str(self.cfg.save())
            except OSError as exc:
                self.log.warning("写回配置失败（本次切换仅内存生效）：%s", exc)
        detail = f"取流传输已切到 {t.upper()}" if changed else f"取流传输保持 {t.upper()}"
        if changed and was_running:
            detail += (
                "，取流已重启生效"
                if restarted
                else f"，但取流重启失败：{self._start_error or '未知原因'}"
            )
        elif changed:
            detail += "，下次启动取流生效"
        if saved:
            detail += "，已写回配置文件"
        if t == "udp":
            detail += "；UDP 怕丢包，画面碎裂时切回 TCP"
        if changed:
            self.log.info("视频取流传输切换：%s -> %s", old, t)
        return {
            "ok": True,
            "transport": t,
            "restarted": restarted,
            "saved": saved,
            "detail": detail,
        }

    # ------------------------------------------------------------------ 帧操作
    def wait_for_frame(self, timeout: float = 2.0) -> bool:
        """等待出现新帧，供无 UI 的自检/脚本使用。"""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if self.frames.pull() is not None:
                return True
            time.sleep(0.02)
        return False

    def latest_meta(self) -> FrameMeta | None:
        return self.frames.latest_meta()

    def snapshot(self, path: str | Path | None = None) -> dict[str, Any]:
        """抓拍当前画面（不消费帧），返回 {ok, path, meta}。"""
        hit = self.frames.peek()
        if hit is None:
            return {"ok": False, "detail": "还没有收到任何一帧", "path": ""}
        image, meta, dwell = hit
        target = Path(path) if path else self._default_snapshot_path()
        try:
            saved = save_frame(image, target)
        except Exception as exc:
            self.log.error("抓拍保存失败：%s", exc)
            return {"ok": False, "detail": str(exc), "path": str(target)}
        self._snapshots += 1
        return {
            "ok": True,
            "path": str(saved),
            "dwell_ms": round(dwell, 1),
            "meta": meta,
        }

    def _default_snapshot_path(self) -> Path:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        return self.cfg.abs_dir("snapshot_dir") / f"frame_{stamp}.png"

    # ------------------------------------------------------------------ 事件
    def _on_state(self, state: object, detail: str = "") -> None:
        value = TransportState(state) if not isinstance(state, TransportState) else state
        if self.bus is None:
            return
        self.bus.publish(
            Events.TRANSPORT_STATE,
            {
                "ts": time.time(),
                "kind": "video",
                "name": self.name,
                "state": value.value,
                "detail": str(detail),
                "url": str(getattr(self._source, "url", "")),
            },
        )

    def _alarm(self, code: str, message: str, level: str = AlarmLevel.WARN.value) -> None:
        if self.bus is None:
            return
        event = AlarmEvent(
            ts=time.time(),
            level=level,
            source=f"video/{self.name}",
            code=code,
            message=message,
        )
        self.bus.publish(Events.ALARM, event)

    # ------------------------------------------------------------------ 汇总
    def summary(self) -> dict[str, Any]:
        st = self.stats()
        return {
            "name": self.name,
            "mock": self.mock,
            "state": self.state.value,
            "connected": st.connected,
            "url": st.url,
            "resolution": st.resolution,
            "codec": st.codec,
            "fps": st.fps_decode,
            "bitrate_kbps": st.bitrate_kbps,
            "stream_index": st.stream_index if st.stream_index is not None else self.stream_index,
            "reconnects": st.reconnects,
            "errors": st.errors,
            "snapshots": self._snapshots,
        }

    def __repr__(self) -> str:
        return f"<VideoService {self.name} state={self.state.value} mock={self.mock}>"
