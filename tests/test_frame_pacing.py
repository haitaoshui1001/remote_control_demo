"""放帧队列与丢帧拆账：解出的帧必须全发布，只有队列溢出才记丢帧。

回归锁定（N 段实测定罪）：无线图传是突发交付，旧的「墙钟限速 + 最新帧槽」
节奏模型把实测 20fps 的源压成 7fps——限速器以"合并"名义吞掉约六成到达帧，
UI 每个节拍又只看最新一眼。对照实验：关限速后同进程到达 12.1 帧/s，而限速档
只发布 4.8 帧/s + 合并 7 帧/s，账目恰好对上。现在的模型：
- _drain 不做任何墙钟限速，每解出一帧立即发布（合并账退役，恒为 0）；
- FrameChannel 有界放帧队列抹平突发，满了顶掉最旧一帧才记丢帧；
- UI 一个节拍 pull_next() 放一帧，按到达顺序播；
- 队首真实滞留超过停留预算（max_dwell_ms）时跳掉积压直接放最新帧，
  这部分过期帧单独记"跳帧"账，与溢出丢帧分账。
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from hmi.core.config import AppConfig, VideoCfg, _validate
from hmi.core.models import FrameMeta
from hmi.transport.base import FrameChannel
from hmi.transport.rtsp import RtspVideoSource


# --------------------------------------------------------------------- 通道层
def test_publish_queues_and_pull_next_is_fifo():
    ch = FrameChannel(max_q=3)
    meta = FrameMeta()
    for label in ("a", "b", "c"):
        assert ch.publish(label, meta) is False, "队列未满不算丢帧"
    got = []
    while True:
        hit = ch.pull_next()
        if hit is None:
            break
        got.append(hit[0])
    assert got == ["a", "b", "c"], "按到达顺序一帧一帧放"


def test_overflow_evicts_oldest_and_flags_drop():
    ch = FrameChannel(max_q=2)
    meta = FrameMeta()
    ch.publish("a", meta)
    ch.publish("b", meta)
    assert ch.publish("c", meta) is True, "队列满顶掉最旧一帧，这才是丢帧"
    assert ch.pull_next()[0] == "b"
    assert ch.pull_next()[0] == "c"
    peeked = ch.peek()
    assert peeked is not None and peeked[0] == "c", "最新帧槽不受队列影响，抓拍照常"


def test_pull_clears_backlog_and_returns_latest():
    ch = FrameChannel()
    meta = FrameMeta()
    for label in ("a", "b", "c"):
        ch.publish(label, meta)
    pulled = ch.pull()
    assert pulled is not None and pulled[0] == "c", "旧式消费者只看现况"
    assert ch.pull_next() is None, "积压随取最新一并作废"


def test_zero_queue_falls_back_to_latest_slot():
    ch = FrameChannel(max_q=0)
    meta = FrameMeta()
    assert ch.publish("a", meta) is False, "首帧没有上一帧，不算覆盖"
    assert ch.publish("b", meta) is True, "关队列时退回最新帧槽：未拉走即覆盖"
    assert ch.pull() is not None
    assert ch.publish("c", meta) is False, "上一帧已被消费，不算丢帧"


def test_unwatched_publish_is_not_dropped_and_peek_still_works():
    ch = FrameChannel()
    meta = FrameMeta()
    ch.set_watching(False)
    for i in range(50):
        assert ch.publish(f"f{i}", meta) is False, "无人观看时的覆盖不计丢帧"
    peeked = ch.peek()
    assert peeked is not None and peeked[0] == "f49", "抓拍/录像仍能取到最新帧"
    ch.set_watching(True)
    assert ch.publish("next", meta) is False, "恢复观看后第一帧不得虚计丢帧"
    pulled = ch.pull()
    assert pulled is not None and pulled[0] == "next"


# ------------------------------------------------------------------ _drain 层
class _PixFmt:
    name = "yuv420p"


class _FakeFrame:
    def __init__(self, label: int, converted: list[int]) -> None:
        self.label = label
        self._converted = converted
        self.width = 8
        self.height = 4
        self.format = _PixFmt()
        self.pts = label
        self.key_frame = False

    def to_ndarray(self, format: str = "") -> np.ndarray:  # noqa: A002 - 签名对齐 av
        self._converted.append(self.label)
        return np.zeros((4, 8, 3), dtype="uint8")


class _FakePacket:
    def __init__(self, frames) -> None:
        self.size = 100
        self._frames = frames

    def decode(self):
        return self._frames


class _FakeContainer:
    def __init__(self, packets) -> None:
        self._packets = packets

    def demux(self, stream: object):
        return iter(self._packets)


class _CodecCtx:
    name = "hevc"


class _FakeStream:
    codec_context = _CodecCtx()


def _new_src(**overrides) -> RtspVideoSource:
    cfg_kwargs = {"url": "rtsp://x/y"}
    cfg_kwargs.update(overrides)
    return RtspVideoSource(VideoCfg(**cfg_kwargs), name="test")


def _drain_burst(src: RtspVideoSource, converted: list[int], n: int, between=None):
    """连发 n 帧；between(i) 在第 i 帧入队前回调，用来模拟 UI 节拍消费。"""

    def gen():
        for i in range(1, n + 1):
            if between is not None:
                between(i)
            yield _FakeFrame(i, converted)

    src._drain(_FakeContainer([_FakePacket(gen())]), _FakeStream())


def test_burst_all_published_and_overflow_is_honest():
    # 突发 8 帧、没人消费：默认队列深 5，最旧 3 帧被顶掉并如实记账，
    # 剩下 5 帧按序等 UI 放——再不存在"限速器名义合并、实际吞帧"。
    src = _new_src(render_fps_limit=25.0)
    converted: list[int] = []
    _drain_burst(src, converted, 8)
    st = src.stats()
    assert converted == list(range(1, 9)), "每帧都做像素转换并发布，不再被限速掐掉"
    assert st.frames_merged == 0, "合并账目退役"
    assert st.frames_dropped == 3, "只有队列溢出顶掉的才算丢帧"
    assert st.frames_decoded == 8
    order = []
    while True:
        hit = src.frames.pull_next()
        if hit is None:
            break
        order.append(hit[1].pts)
    assert order == [4, 5, 6, 7, 8], "溢出不死锁：最新几帧仍按到达顺序可播"


def test_ui_cadence_consumes_every_frame():
    # UI 一个节拍放一帧：哪怕帧连排到达，只要节拍跟得上就不掉一帧。
    src = _new_src(render_fps_limit=25.0)
    converted: list[int] = []
    shown: list[int] = []

    def tick(_i: int) -> None:
        hit = src.frames.pull_next()
        if hit is not None:
            shown.append(hit[1].pts)

    _drain_burst(src, converted, 8, between=tick)
    while True:
        hit = src.frames.pull_next()
        if hit is None:
            break
        shown.append(hit[1].pts)
    st = src.stats()
    assert converted == list(range(1, 9))
    assert shown == list(range(1, 9)), "8 帧按序播完，一帧不丢"
    assert st.frames_dropped == 0


def test_unwatched_stream_inflates_nothing():
    # 切页后的真实处境：无人 pull 也无人声明要看——丢帧账必须保持干净，
    # 但帧照解照发布，最新帧槽留给抓拍/录像。
    src = _new_src()
    src.frames.set_watching(False)
    converted: list[int] = []
    _drain_burst(src, converted, 50)
    st = src.stats()
    assert st.frames_dropped == 0, "无人观看的覆盖不是丢帧（旧版 1095 虚账的病根）"
    assert st.frames_decoded == 50 and converted == list(range(1, 51))
    peeked = src.frames.peek()
    assert peeked is not None and peeked[1].pts == 50


def test_render_fps_limit_no_longer_throttles_publish():
    # 结构性护栏：限速配置只留给 UI 节拍用，解码线程里不得再有墙钟限速。
    import inspect

    from hmi.transport import rtsp as rtsp_mod

    src_code = inspect.getsource(rtsp_mod.RtspVideoSource._drain)
    assert "render_fps_limit" not in src_code, "解码线程里不得再有墙钟限速"


# ------------------------------------------------------------- 积压跳帧（延迟预算）
def test_backlog_beyond_dwell_budget_skips_to_latest():
    ch = FrameChannel(max_q=5, max_dwell_ms=5.0)
    meta = FrameMeta()
    for label in ("a", "b", "c", "d", "e"):
        ch.publish(label, meta)
    time.sleep(0.02)
    hit = ch.pull_next()
    assert hit is not None and hit[0] == "e", "超预算：跳掉积压直接播最新帧"
    assert ch.skipped == 4, "中间 4 帧过期被跳过并记账"
    assert ch.pull_next() is None, "跳帧后积压作废，不重复播"


def test_within_dwell_budget_keeps_fifo_order():
    ch = FrameChannel(max_q=5, max_dwell_ms=1000.0)
    meta = FrameMeta()
    for label in ("a", "b", "c"):
        ch.publish(label, meta)
    got = []
    while True:
        hit = ch.pull_next()
        if hit is None:
            break
        got.append(hit[0])
    assert got == ["a", "b", "c"], "预算内一切照旧：纯 FIFO 按到达顺序"
    assert ch.skipped == 0


def test_single_queued_frame_is_never_skipped():
    ch = FrameChannel(max_q=5, max_dwell_ms=1.0)
    meta = FrameMeta()
    ch.publish("a", meta)
    time.sleep(0.02)
    hit = ch.pull_next()
    assert hit is not None and hit[0] == "a", "队列只有一帧，没有积压可跳"
    assert ch.skipped == 0


def test_budget_zero_keeps_legacy_pure_fifo():
    ch = FrameChannel(max_q=5)
    meta = FrameMeta()
    for label in ("a", "b", "c", "d"):
        ch.publish(label, meta)
    time.sleep(0.02)
    hit = ch.pull_next()
    assert hit is not None and hit[0] == "a", "预算为 0=关闭：再久滞留也按序放队首"
    assert ch.skipped == 0


def test_source_wires_pacing_cfg_and_reports_skips():
    src = _new_src(pacing_frames=4, max_dwell_ms=5.0)
    converted: list[int] = []
    _drain_burst(src, converted, 4)
    time.sleep(0.02)
    hit = src.frames.pull_next()
    assert hit is not None and hit[1].pts == 4, "源按配置装配预算：积压超支后播最新帧"
    st = src.stats()
    assert st.frames_skipped == 3, "跳帧进 stats 账，与溢出丢帧分账"
    assert st.frames_dropped == 0, "队列没溢出，丢帧账必须保持干净"


def test_pacing_cfg_validation_clamps_out_of_range():
    cfg = AppConfig()
    cfg.video.pacing_frames = 99
    cfg.video.max_dwell_ms = -5.0
    _validate(cfg)
    assert cfg.video.pacing_frames == 15
    assert cfg.video.max_dwell_ms == 0.0
    assert any("放帧队列深度" in w for w in cfg.meta.warnings)
    assert any("停留预算" in w for w in cfg.meta.warnings)


def test_video_latency_tuning_defaults():
    cfg = VideoCfg()
    assert cfg.pacing_frames == 5, "默认队列深度维持放帧节拍回归钉住的 5"
    assert cfg.max_dwell_ms == pytest.approx(120.0), "默认给 120ms 积压预算"


def test_transport_validation_normalizes_and_falls_back():
    cfg = AppConfig()
    cfg.video.transport = "UDP "
    _validate(cfg)
    assert cfg.video.transport == "udp", "大小写/首尾空白做规范化，不算非法"
    assert not cfg.meta.warnings

    cfg = AppConfig()
    cfg.video.transport = "http"
    _validate(cfg)
    assert cfg.video.transport == "tcp", "FFmpeg 只认 tcp/udp，非法值必须回退"
    assert any("视频传输" in w for w in cfg.meta.warnings)


if __name__ == "__main__":  # pragma: no cover - 手工入口
    raise SystemExit(pytest.main([__file__, "-q"]))
