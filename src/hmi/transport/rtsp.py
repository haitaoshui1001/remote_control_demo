"""RTSP 拉流与解码：独立线程产出 RGB 帧 + 运行指标，UI 只拉最新帧。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from ..core.config import VideoCfg
from ..core.logger import get_logger
from ..core.models import FrameMeta, TransportState, VideoStats
from ..core.utils import RateMeter
from .base import FrameChannel

try:
    import av

    HAS_AV = True
except Exception:
    av = None
    HAS_AV = False

StateListener = Callable[[TransportState, str], None]


def _frame_time(frame: Any, attr: str) -> float | None:
    """帧的展示时间（秒）：解码后的 VideoFrame 没有 time_base，直接读 pts_time
    会抛 AttributeError；退化成 pts×time_base，再拿不到就当没有。"""
    try:
        return getattr(frame, attr)
    except AttributeError:
        pass
    base = attr[:-5] if attr.endswith("_time") else attr
    val = getattr(frame, base, None)
    tb = getattr(frame, "time_base", None)
    if val is None or tb is None:
        return None
    try:
        return float(val) * float(tb)
    except (TypeError, ValueError):
        return None


class RtspVideoSource:
    """视频链路的传输端：断流自动重连、主/子码流热切换、指标自计。"""

    def __init__(self, cfg: VideoCfg, *, name: str = "rtsp") -> None:
        self.name = name
        self.cfg = cfg
        # url 留空不代表没地址：与 AppConfig.rtsp_url 同一解析——按 url_template 拼装
        self.url = cfg.resolved_url()
        self.frames = FrameChannel(
            max_q=max(0, int(cfg.pacing_frames)),
            max_dwell_ms=max(0.0, float(cfg.max_dwell_ms)),
        )
        self._stop = threading.Event()
        self._switch = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = TransportState.CLOSED
        self._detail = ""
        self._state_ls: list[StateListener] = []
        self._lock = threading.Lock()
        self._fps = RateMeter(3.0)
        self._bytes = RateMeter(5.0)
        self._s = VideoStats(url=self.url, stream_index=cfg.stream_index)
        self._container: Any = None
        self._log = get_logger(f"transport.{name}")

    def add_state_listener(self, fn: StateListener) -> None:
        self._state_ls.append(fn)

    @property
    def state(self) -> TransportState:
        return self._state

    def stats(self) -> VideoStats:
        with self._lock:
            s = self._s
            now = time.time()
            s.fps_decode = round(self._fps.rate(), 2)
            s.bitrate_kbps = round(self._bytes.rate() * 8.0 / 1000.0, 1)
            s.frames_skipped = self.frames.skipped
            if s.last_frame_ts:
                s.last_frame_age_ms = round((now - s.last_frame_ts) * 1000.0, 1)
            return s

    def info(self) -> dict[str, Any]:
        s = self.stats()
        base = {
            "name": self.name,
            "state": self._state.value,
            "detail": self._detail,
            "url": self.url,
            "pyav": HAS_AV,
        }
        base.update({k: v for k, v in vars(s).items() if k != "last_frame_age_ms"})
        base["last_frame_age_ms"] = s.last_frame_age_ms
        return base

    def start(self) -> None:
        if not HAS_AV:
            raise RuntimeError("缺少 PyAV（av），无法解码 RTSP 视频")
        if not self.url:
            raise RuntimeError(
                "视频地址为空：请填 config/app.yaml 的 video.camera_ip（模板拼装用），"
                "或直接填 video.url"
            )
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name=f"hmi-{self.name}-rx", daemon=True
            )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        self._set_state(TransportState.CLOSED, "已停止")

    def switch_stream(self, stream_index: int, url: str) -> None:
        """热切换主/子码流：置标志让读线程在本轮循环里干净地重开。"""
        with self._lock:
            self.url = url
            self._s.stream_index = int(stream_index)
            self._s.url = url
        self._switch.set()

    def _loop(self) -> None:
        backoff = max(0.5, float(self.cfg.reconnect_interval_s))
        while not self._stop.is_set():
            self._switch.clear()
            try:
                self._run_once()
                backoff = max(0.5, float(self.cfg.reconnect_interval_s))
            except Exception as exc:
                with self._lock:
                    self._s.errors += 1
                    self._s.last_error = str(exc)
                self._set_state(TransportState.ERROR, str(exc))
                self.log_line("warning", f"视频读取异常: {exc}")
            if self._stop.is_set():
                break
            if not self.cfg.auto_reconnect:
                break
            if int(self.cfg.max_reconnects) and self._s.reconnects >= int(self.cfg.max_reconnects):
                self.log_line("error", f"重连次数已达上限 {self.cfg.max_reconnects}")
                break
            with self._lock:
                self._s.reconnects += 1
            self._set_state(
                TransportState.OPENING, f"{backoff:.1f}s 后重连（第 {self._s.reconnects} 次）"
            )
            if self._stop.wait(backoff):
                break
        self._set_state(TransportState.CLOSED, "读线程退出")

    def _run_once(self) -> None:
        opts = self._options()
        timeout = max(1.0, self.cfg.open_timeout_us / 1_000_000.0)
        with self._lock:
            self._s.connected = False
            self._s.url = self.url
        container = av.open(self.url, format="rtsp", options=opts, timeout=timeout)
        self._container = container
        try:
            stream = self._pick_stream(container)
            if stream is None:
                raise RuntimeError("RTSP 流中没有视频轨")
            self._set_state(
                TransportState.OPEN,
                f"{stream.width or '?'}x{stream.height or '?'} {stream.codec_context.name}",
            )
            with self._lock:
                self._s.connected = True
                self._s.resolution = f"{stream.width}x{stream.height}"
                self._s.codec = stream.codec_context.name or ""
                self._s.pix_fmt = getattr(stream.codec_context, "pix_fmt", "") or ""
            self._drain(container, stream)
        finally:
            try:
                container.close()
            except Exception:
                pass
            self._container = None
            with self._lock:
                self._s.connected = False

    def _drain(self, container: Any, stream: Any) -> None:
        # 这里不做任何墙钟限速：图传是突发交付（一批连到好几帧），按节拍掐帧
        # 会把显示 fps 压成批次频率。每解出一帧都发布，节奏交给帧通道的放帧
        # 队列和 UI 节拍去抹平。
        for packet in container.demux(stream):
            if self._stop.is_set() or self._switch.is_set():
                break
            if packet.size:
                self._bytes.add(packet.size)
            try:
                frames = packet.decode()
            except Exception as exc:
                self.log_line("debug", f"解码失败（忽略该包）: {exc}")
                continue
            for frame in frames:
                if self._stop.is_set() or self._switch.is_set():
                    break
                t0 = time.perf_counter()
                try:
                    image = frame.to_ndarray(format="rgb24")
                except Exception as exc:
                    self.log_line("debug", f"像素转换失败: {exc}")
                    continue
                decode_ms = (time.perf_counter() - t0) * 1000.0
                meta = FrameMeta(
                    width=frame.width,
                    height=frame.height,
                    codec=stream.codec_context.name or "",
                    pix_fmt=frame.format.name if frame.format else "",
                    pts=frame.pts,
                    pts_time=_frame_time(frame, "pts_time"),
                    dts_time=_frame_time(frame, "dts_time"),
                    key_frame=bool(getattr(frame, "key_frame", False)),
                )
                if self.frames.publish(image, meta):
                    # 放帧队列满了，最旧的一帧被顶掉——这才是真丢帧
                    with self._lock:
                        self._s.frames_dropped += 1
                self._fps.add(1)
                with self._lock:
                    self._s.frames_decoded += 1
                    self._s.decode_ms = round(self._s.decode_ms * 0.7 + decode_ms * 0.3, 2)
                    self._s.resolution = f"{frame.width}x{frame.height}"
                    self._s.last_frame_age_ms = 0.0
                    self._s.last_frame_ts = time.time()

    def _pick_stream(self, container: Any) -> Any:
        videos = list(container.streams.video)
        if not videos:
            return None
        best = videos[0]
        for st in videos:
            try:
                if st.duration and best.duration and st.bit_rate > best.bit_rate:
                    best = st
            except Exception:
                continue
        try:
            best.thread_type = "AUTO"
            if best.codec_context is not None:
                best.codec_context.thread_count = max(1, int(self.cfg.decode_threads))
        except Exception:
            pass
        return best

    def _options(self) -> dict[str, str]:
        opts = {
            "rtsp_transport": self.cfg.transport or "tcp",
            "stimeout": str(max(500_000, int(self.cfg.open_timeout_us))),
            "timeout": str(max(500_000, int(self.cfg.open_timeout_us))),
            "max_delay": str(max(0, int(self.cfg.buffer_ms)) * 1000),
            "fflags": "nobuffer" if self.cfg.drop_late_frames else "discardcorrupt",
            "flags": "low_delay",
            "analyzeduration": "300000",
            "probesize": "300000",
        }
        if self.cfg.transport == "udp":
            opts["buffer_size"] = "4194304"
        return opts

    def _set_state(self, state: TransportState, detail: str = "") -> None:
        self._state, self._detail = state, detail
        for fn in list(self._state_ls):
            try:
                fn(state, detail)
            except Exception:
                self.log_line("exception", "视频状态回调异常")

    def log_line(self, level: str, msg: str) -> None:
        getattr(
            self._log,
            level if hasattr(self._log, level) else "info",
        )(msg)
