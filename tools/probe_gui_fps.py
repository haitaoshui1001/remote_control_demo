"""GUI 帧率验证：修复后（放帧队列 + 一 tick 一帧）同一条解码线程的各档吞吐。

用法::

    python tools/probe_gui_fps.py            # 每档 6 秒
    python tools/probe_gui_fps.py 8          # 每档 8 秒

档位（按对主线程的压力从低到高）：
- idle  不拉帧（watching=False，模拟切页）——解码线程吞吐上限
- next  25Hz 只 pull_next 放帧，不做任何 Qt 转换
- ui    video_page 全量复刻：pull_next + fromImage + SmoothTransformation

判读（N 段修复后语义）：_drain 不再墙钟限速，fps 列 ≈ 帧的真实到达率，
各档应当基本一致；「显示帧/秒」只在 ui 档有意义，它才是用户眼睛里的帧率。
若 fps 列仍远低于相机声明帧率，剩余瓶颈在链路/相机侧（突发交付、码率上限），
与显示节奏无关。旧版对照（限速器时代）：idle/pull ≈4.8fps + 合并 ≈7/s，
arrival 12.1/s——限速器吞帧的定罪数据。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
from PyQt5.QtCore import Qt, QTimer  # noqa: E402
from PyQt5.QtGui import QImage, QPixmap  # noqa: E402
from PyQt5.QtWidgets import QApplication, QLabel  # noqa: E402

from hmi.core.config import load_config  # noqa: E402
from hmi.services.video import VideoService, as_rgb_array  # noqa: E402

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


class _Meter:
    def __init__(self) -> None:
        self.n = 0
        self.total_ms = 0.0
        self.max_ms = 0.0
        self.shown = 0

    def one(self, dt_ms: float, drew: bool) -> None:
        self.n += 1
        self.total_ms += dt_ms
        self.max_ms = max(self.max_ms, dt_ms)
        if drew:
            self.shown += 1


def run_variant(app: QApplication, svc: VideoService, label: QLabel, mode: str, seconds: float):
    frames = svc.frames
    meter = _Meter()

    def on_tick() -> None:
        t0 = time.perf_counter()
        hit = frames.pull_next()
        drew = False
        if hit is not None and mode == "ui":
            arr = np.ascontiguousarray(as_rgb_array(hit[0]))
            h, w = int(arr.shape[0]), int(arr.shape[1])
            qimg = QImage(arr.data, w, h, w * 3, QImage.Format_RGB888)
            pm = QPixmap.fromImage(qimg)
            label.setPixmap(pm.scaled(label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            drew = True
        meter.one((time.perf_counter() - t0) * 1000.0, drew)

    timer = QTimer()
    timer.setInterval(40)
    timer.timeout.connect(on_tick)
    frames.set_watching(mode != "idle")
    label.clear()
    if not svc.start():
        print(f"[{mode:6s}] 启动失败: {svc.info().get('start_error')}")
        return None
    if mode != "idle":
        timer.start()
    before = svc.stats()
    d0 = before.frames_decoded
    t0 = time.monotonic()
    deadline = t0 + seconds
    try:
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.001)
    finally:
        timer.stop()
        svc.stop()
    elapsed = time.monotonic() - t0
    after = svc.stats()
    return {
        "mode": mode,
        "fps": (after.frames_decoded - d0) / elapsed,
        "dropped": after.frames_dropped,
        "tick_avg_ms": meter.total_ms / meter.n if meter.n else 0.0,
        "tick_max_ms": meter.max_ms,
        "ticks": meter.n,
        "shown": meter.shown,
        "bitrate": after.bitrate_kbps,
    }


def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
    cfg = load_config(str(ROOT / "config" / "app.yaml"))
    cfg.app.mock = False
    app = QApplication([])
    label = QLabel()
    label.resize(900, 506)
    label.show()
    svc = VideoService(cfg)
    print(f"URL: {svc.url_for(cfg.video.stream_index)[:60]}... | 每档 {seconds:.0f}s")
    print(
        f"{'档位':8s} {'fps':>6s} {'显示fps':>7s} "
        f"{'tick均值ms':>10s} {'tick最大ms':>10s} {'丢帧':>4s} {'码率kbps':>8s}"
    )
    for mode in ("idle", "next", "ui"):
        r = run_variant(app, svc, label, mode, seconds)
        if r is None:
            continue
        shown_fps = r["shown"] / seconds
        print(
            f"{r['mode']:8s} {r['fps']:6.1f} {shown_fps:7.1f} "
            f"{r['tick_avg_ms']:10.2f} {r['tick_max_ms']:10.2f} "
            f"{r['dropped']:4d} {r['bitrate']:8.0f}"
        )


if __name__ == "__main__":
    main()
