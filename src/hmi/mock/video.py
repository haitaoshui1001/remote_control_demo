"""仿真视频源：产出合成测试画面，接口与 RtspVideoSource 完全一致。

这样 services/ui 层在 mock 与真实之间不需要任何分支判断。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from ..core.config import VideoCfg
from ..core.logger import get_logger
from ..core.models import FrameMeta, TransportState, VideoStats
from ..core.utils import RateMeter
from ..transport.base import FrameChannel

StateListener = Callable[[TransportState, str], None]

try:
    from PIL import Image, ImageDraw

    HAS_PIL = True
except Exception:
    Image = None
    ImageDraw = None
    HAS_PIL = False

SIZES = {0: (1920, 1080), 1: (704, 576)}


class MockVideoSource:
    """按 fps 合成画面：滚动网格 + 扫描线 + 实时字标，用于无硬件自检。"""

    def __init__(self, cfg: VideoCfg, *, name: str = "mockcam", fps: float = 25.0) -> None:
        self.name = name
        self.cfg = cfg
        self.url = cfg.url or f"mock://{cfg.camera_ip}/stream={cfg.stream_index}"
        self.frames = FrameChannel(
            max_q=max(0, int(cfg.pacing_frames)),
            max_dwell_ms=max(0.0, float(cfg.max_dwell_ms)),
        )
        self._fps = max(1.0, float(fps))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = TransportState.CLOSED
        self._detail = ""
        self._state_ls: list[StateListener] = []
        self._lock = threading.Lock()
        self._rate = RateMeter(3.0)
        self._s = VideoStats(url=self.url, stream_index=cfg.stream_index, connected=False)
        self._seq = 0
        self._log = get_logger(f"mock.{name}")

    # ------------------------------------------------------------------ 公共
    def add_state_listener(self, fn: StateListener) -> None:
        self._state_ls.append(fn)

    @property
    def state(self) -> TransportState:
        return self._state

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name=f"hmi-{self.name}-vrx", daemon=True
            )
        self._set_state(TransportState.OPEN, f"仿真 {self._size()[0]}x{self._size()[1]}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
        self._thread = None
        with self._lock:
            self._s.connected = False
        self._set_state(TransportState.CLOSED, "已停止")

    def switch_stream(self, stream_index: int, url: str) -> None:
        with self._lock:
            self._s.stream_index = int(stream_index)
            self._s.url = url or self.url
            self.url = self._s.url

    def stats(self) -> VideoStats:
        with self._lock:
            s = self._s
            s.fps_decode = round(self._rate.rate(), 2)
            s.frames_skipped = self.frames.skipped
            s.bitrate_kbps = round(
                self._rate.rate() * float(np.prod(self._size())) * 3.0 * 8.0 / 1000.0, 1
            )
            if s.last_frame_ts:
                s.last_frame_age_ms = round((time.time() - s.last_frame_ts) * 1000.0, 1)
            return s

    def info(self) -> dict[str, Any]:
        s = self.stats()
        base = {
            "name": self.name,
            "state": self._state.value,
            "detail": self._detail,
            "url": self.url,
            "mock": True,
        }
        base.update({k: v for k, v in vars(s).items() if k != "last_frame_ts"})
        return base

    # ------------------------------------------------------------------ 内部
    def _size(self) -> tuple:
        idx = int(self.cfg.stream_index or 0)
        return SIZES.get(idx, (704, 576))

    def _loop(self) -> None:
        period = 1.0 / self._fps
        with self._lock:
            self._s.connected = True
        while not self._stop.is_set():
            t0 = time.perf_counter()
            self._seq += 1
            image, meta = self._render(self._seq)
            if self.frames.publish(image, meta):
                with self._lock:
                    self._s.frames_dropped += 1
            self._rate.add(1)
            with self._lock:
                self._s.frames_decoded += 1
                self._s.resolution = f"{meta.width}x{meta.height}"
                self._s.codec = "mock(h264)"
                self._s.decode_ms = round((time.perf_counter() - t0) * 1000.0, 2)
                self._s.last_frame_ts = time.time()
                self._s.last_frame_age_ms = 0.0
            took = time.perf_counter() - t0
            self._stop.wait(max(0.0, period - took))
        with self._lock:
            self._s.connected = False

    def _render(self, seq: int) -> tuple:
        w, h = self._size()
        grid = ((np.indices((h, w)).sum(axis=0) // 16 + seq) % 2).astype(np.uint8)
        img = np.zeros((h, w, 3), dtype=np.uint8)
        img[..., 0] = 40 + grid * 18
        img[..., 1] = (60 + (np.arange(h)[:, None] // 8 % 2) * 14).astype(np.uint8)
        img[..., 2] = 90 + grid * 10
        bar = int((seq * (w // 3)) % (w - max(1, w // 6)))
        img[:, bar : bar + max(1, w // 30)] = (230, 230, 230)
        meta = FrameMeta(
            width=w,
            height=h,
            codec="mock(h264)",
            pix_fmt="rgb24",
            pts=seq,
            pts_time=seq / self._fps,
            key_frame=(seq % 50 == 0),
        )
        if HAS_PIL:
            self._overlay(img, seq)
        return img, meta

    def _overlay(self, img: np.ndarray, seq: int) -> None:
        pil = Image.fromarray(img)
        draw = ImageDraw.Draw(pil)
        label = (
            f"MOCK CAM {self.cfg.camera_ip} stream="
            f"{self.cfg.stream_index}  #{seq}  "
            f"{time.strftime('%H:%M:%S')}"
        )
        draw.rectangle((0, 0, min(img.shape[1], 12 + 7 * len(label)), 18), fill=(0, 0, 0))
        draw.text((4, 3), label, fill=(240, 240, 240))
        img[...] = np.asarray(pil)

    def _set_state(self, state: TransportState, detail: str = "") -> None:
        self._state, self._detail = state, detail
        for fn in list(self._state_ls):
            try:
                fn(state, detail)
            except Exception:
                self._log.exception("仿真视频状态回调异常")
