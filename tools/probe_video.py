"""视频链路实测：分清"摄像头只发这么慢"还是"我们解码跟不上"。

分别对子码流/主码流各测一段时间，报告：
- SDP 声明帧率（camera.average_rate）——摄像头配置的真实出帧率；
- 包到达速率（packets/s）——网络上实际收到了多少帧；
- 解码+像素转换吞吐（frames/s）与各自平均耗时——本机处理能力。

用法：python tools/probe_video.py [时长秒=10] [码流=1,0]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
DEPS = ROOT / ".deps"
if DEPS.is_dir() and str(DEPS) not in sys.path:
    sys.path.append(str(DEPS))

import av  # noqa: E402

from hmi.core.config import load_config  # noqa: E402
from hmi.transport.rtsp import RtspVideoSource  # noqa: E402


def probe(url: str, opts: dict[str, str], seconds: float) -> None:
    print(f"\n=== 探测 {url}")
    t_open = time.perf_counter()
    container = av.open(url, format="rtsp", options=opts, timeout=10.0)
    stream = container.streams.video[0]
    stream.thread_type = "AUTO"
    print(
        f"打开耗时 {(time.perf_counter() - t_open) * 1000:.0f}ms | "
        f"{stream.width}x{stream.height} {stream.codec_context.name} | "
        f"声明帧率 base={stream.base_rate} average={stream.average_rate} "
        f"guessed={stream.guessed_rate}"
    )
    packets = frames = 0
    total_bytes = 0
    decode_s = convert_s = 0.0
    pts_list: list[float] = []
    deadline = time.perf_counter() + seconds
    for packet in container.demux(stream):
        if time.perf_counter() >= deadline:
            break
        if packet.size:
            packets += 1
            total_bytes += packet.size
        t0 = time.perf_counter()
        try:
            got = packet.decode()
        except Exception as exc:  # noqa: BLE001
            print(f"  解码异常（忽略）：{exc}")
            continue
        for frame in got:
            frames += 1
            decode_s += time.perf_counter() - t0
            t1 = time.perf_counter()
            frame.to_ndarray(format="rgb24")
            convert_s += time.perf_counter() - t1
            if frame.pts is not None and stream.time_base:
                pts_list.append(float(frame.pts * stream.time_base))
    container.close()
    if frames == 0:
        print("  窗口内没有解出任何帧")
        return
    gaps = [b - a for a, b in zip(pts_list, pts_list[1:], strict=False)]
    kbps = total_bytes * 8 / seconds / 1000
    print(
        f"  包 {packets} 个（{packets / seconds:.1f}/s，{kbps:.0f} kbps）| "
        f"帧 {frames} 个（{frames / seconds:.1f}/s）"
    )
    print(
        f"  单帧解码均值 {decode_s / frames * 1000:.1f}ms + 转RGB均值 "
        f"{convert_s / frames * 1000:.1f}ms = 合计 {(decode_s + convert_s) / frames * 1000:.1f}ms"
    )
    if gaps:
        print(
            f"  帧间隔(pts) 均值 {sum(gaps) / len(gaps) * 1000:.1f}ms "
            f"最小 {min(gaps) * 1000:.1f}ms 最大 {max(gaps) * 1000:.1f}ms "
            f"→ 源端实际约 {1.0 / (sum(gaps) / len(gaps)):.1f} fps"
        )


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    streams = [int(x) for x in (sys.argv[2].split(",") if len(sys.argv) > 2 else ["1"])]
    cfg = load_config(ROOT / "config" / "app.yaml")
    src = RtspVideoSource(cfg.video, name="probe")
    opts = src._options()
    for idx in streams:
        url = cfg.video.resolved_url(idx)
        try:
            probe(url, opts, seconds)
        except Exception as exc:  # noqa: BLE001
            print(f"\n=== 探测失败 {url}\n  {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
