"""485 透传引擎测试：编码用纯函数断言，配对用假通道驱动，最后一例跑真仿真链路。

这里要守住三条纪律：
1. **顺序**——CRC 必须在结束符之前，否则 Modbus 校验值算错；
2. **半双工**——AT 窗口开着时透传必须拒绝发送，并且拒绝要可计数；
3. **配对窗口先于发送**——从机答得快也不能漏配。
"""

from __future__ import annotations

import threading
import time

import pytest

from hmi.core.config import PassthroughCfg
from hmi.core.events import EventBus, Events
from hmi.core.models import Direction
from hmi.core.utils import verify_crc16
from hmi.mock.radio import MockByteTransport, MockRadioModule
from hmi.protocol.channel import ChannelDemux
from hmi.protocol.passthrough import PassthroughEngine, build_frame, reply_matches

_REPLY = bytes([0x01, 0x03, 0x04, 0x01, 0x00, 0x01, 0x02])


class FakeChannel:
    """最小通道替身：记下写出的字节，可选立刻回一帧（模拟快从机）。"""

    def __init__(self, reply: bytes | None = None, *, error: Exception | None = None) -> None:
        self.name = "fake485"
        self.reply = reply
        self.error = error
        self.writes: list[bytes] = []
        self.sinks: list = []
        self.requester = ""
        self.sent = threading.Event()
        self._at_inject = False

    def subscribe_passthrough(self, sink) -> None:
        self.sinks.append(sink)

    def unsubscribe(self, sink) -> None:
        if sink in self.sinks:
            self.sinks.remove(sink)

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        self.sent.set()
        if self.error is not None:
            raise self.error
        if self.reply is not None:
            for sink in list(self.sinks):
                sink(self.reply, time.time())
        return len(data)

    def inject(self, frame: bytes) -> None:
        """外部（仿真读线程）送一帧进来。"""
        for sink in list(self.sinks):
            sink(frame, time.time())

    def stats(self) -> dict:
        return {"requester": self.requester, "at": self._at_inject}

    def is_attached(self) -> bool:
        return True


def _by_direction(eng: PassthroughEngine, direction: str) -> list:
    return [r for r in eng.all_records() if r.direction == direction]


def _engine(channel: FakeChannel, **kw) -> PassthroughEngine:
    cfg = kw.pop("cfg", None) or PassthroughCfg()
    eng = PassthroughEngine(channel, bus=EventBus(), cfg=cfg, endpoint="local", name="485", **kw)
    eng.start()
    return eng


# --------------------------------------------------------------------- 编码
def test_build_frame_appends_crc_before_suffix():
    frame = build_frame("010300000001", mode="hex", suffix="crlf", crc16_append=True)
    assert frame.endswith(b"\r\n")
    assert len(frame) == 6 + 2 + 2
    assert verify_crc16(frame[:-2])


def test_build_frame_modes_and_suffixes():
    assert build_frame("01 03", mode="hex") == bytes([0x01, 0x03])
    assert build_frame("A1B2", mode="hex") == bytes([0xA1, 0xB2])
    assert build_frame("RUN\\r", mode="ascii", suffix="none") == b"RUN\r"
    assert build_frame("0103", suffix="at") == bytes([0x01, 0x03]) + b"AT"
    with pytest.raises(ValueError):
        build_frame("0103", suffix="brackets")


def test_reply_matches_prefix_and_min_length():
    assert reply_matches(_REPLY, prefix="0103", prefix_mode="hex")
    assert not reply_matches(_REPLY, prefix="0104", prefix_mode="hex")
    assert reply_matches(_REPLY, min_bytes=7)
    assert not reply_matches(_REPLY, min_bytes=8)
    assert reply_matches(_REPLY, prefix="", min_bytes=0)
    assert reply_matches(b"OK done", prefix="OK", prefix_mode="ascii")
    # 前缀写法非法时判不匹配，而不是抛异常炸掉收包线程
    assert not reply_matches(_REPLY, prefix="zz", prefix_mode="hex")


# --------------------------------------------------------------------- 发送
def test_send_accounts_bytes_and_publishes_record():
    ch = FakeChannel()
    bus = EventBus()
    seen: list = []
    bus.subscribe(Events.PT_BYTES, lambda topic, payload: seen.append(payload))
    eng = PassthroughEngine(ch, bus=bus, cfg=PassthroughCfg(), endpoint="local", name="485")
    eng.start()

    rec = eng.send("010300000001", note="读线圈")
    assert rec.direction == Direction.TX.value
    assert rec.data == bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x01])
    assert rec.note == "读线圈"
    assert rec.kind == "manual"
    assert eng.tx_frames == 1 and eng.tx_bytes == 6
    assert eng.all_records() and eng.all_records()[0] is rec
    assert seen and seen[0] is rec

    eng.configure(echo_tx=False)
    before = len(eng.all_records())
    eng.send("0103")
    assert eng.tx_frames == 2
    assert len(eng.all_records()) == before


def test_send_rejects_empty_and_channel_error():
    ch = FakeChannel()
    eng = _engine(ch)
    with pytest.raises(ValueError):
        eng.send("")
    assert eng.tx_frames == 0

    boom = FakeChannel(error=OSError("COM7 被占用"))
    eng2 = _engine(boom)
    with pytest.raises(OSError):
        eng2.send("0103")
    assert eng2.errors == 1


def test_write_failure_carries_frame_bytes_for_display():
    """写失败时组帧已经完成——原始字节必须挂在异常上，界面才能显示"本来发了什么"。

    引擎对 OSError 只挂帧不吞异常（收口是服务层职责），但 ``request()``
    清理配对窗口后 re-raise，两条路径都必须带上 ``frame_bytes``。
    """
    boom = FakeChannel(error=OSError("COM7 被占用"))
    eng = _engine(boom)
    with pytest.raises(OSError) as ei:
        eng.send("0103")
    assert ei.value.frame_bytes == bytes([0x01, 0x03])
    with pytest.raises(OSError) as ei2:
        eng.request("0103")
    assert ei2.value.frame_bytes == bytes([0x01, 0x03])


def test_busy_probe_refuses_send_unless_forced():
    ch = FakeChannel()
    ch.requester = "at-local"
    eng = _engine(ch)

    with pytest.raises(RuntimeError):
        eng.send("0103")
    assert eng.refusals == 1
    assert ch.writes == []

    rec = eng.send("0103", force=True)
    assert rec.data == bytes([0x01, 0x03])
    assert eng.refusals == 1

    # 请求级入口把拒绝转成可读结果，不抛给 UI；且带上被拒发的原始帧
    res = eng.request("0104")
    assert res.ok is False and res.sent_bytes == bytes([0x01, 0x04])
    assert "半双工" in res.error
    assert eng.stats().errors == eng.errors + eng.refusals


def test_busy_probe_survives_channel_without_stats():
    class Bare:
        def subscribe_passthrough(self, sink):
            pass

        def write(self, data):
            return len(data)

    eng = _engine(Bare())
    assert eng.send("0103").data == bytes([0x01, 0x03])


def test_configure_rejects_unknown_key_and_applies_defaults():
    eng = _engine(FakeChannel())
    with pytest.raises(AttributeError):
        eng.configure(mode="hex", nope=1)
    eng.configure(mode="ascii", suffix="crlf")
    assert eng.resolve("PING") == b"PING\r\n"
    eng.configure(max_records=4)
    assert len(eng.all_records()) == 0


# --------------------------------------------------------------------- 配对
def test_request_pairs_reply_even_when_slave_is_fast():
    """应答在 ``write()`` 返回前就到了也必须配上——窗口先于发送登记。"""
    ch = FakeChannel(reply=_REPLY)
    bus = EventBus()
    results: list = []
    bus.subscribe(Events.CMD_RESULT, lambda topic, payload: results.append(payload))
    eng = PassthroughEngine(
        ch, bus=bus, cfg=PassthroughCfg(reply_timeout_ms=500), endpoint="local", name="485"
    )
    eng.start()

    res = eng.request("010300000001", match_prefix="0103", name="读状态")
    assert res.ok is True and res.matched is True
    assert res.reply == _REPLY
    assert res.sent_bytes == bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x01])
    assert res.name == "读状态"
    assert eng.timeouts == 0
    assert eng.stats().pending == 0
    assert results and results[0] is res
    rx = _by_direction(eng, Direction.RX.value)
    assert rx and rx[-1].kind == "reply"
    # 假通道在 write() 里同步回帧，环形缓冲的插入顺序会 rx 先于 tx；
    # 真正有序的是时间戳（tx.ts 在 write 之前取样），UI 排序必须按 ts 而非入队顺序。
    assert _by_direction(eng, Direction.TX.value)[0].ts <= rx[0].ts


def test_request_timeout_when_no_reply():
    eng = _engine(FakeChannel())
    res = eng.request("010300000001", timeout_ms=40, name="读状态")
    assert res.ok is False and res.matched is False
    assert res.reply == b""
    assert "应答超时" in res.error and "40" in res.error
    assert eng.timeouts == 1
    assert _by_direction(eng, Direction.RX.value) == []
    assert len(_by_direction(eng, Direction.TX.value)) == 1


def test_request_ignores_non_matching_frames_then_matches():
    ch = FakeChannel()
    eng = _engine(ch, cfg=PassthroughCfg(reply_timeout_ms=800))
    ch.reply = bytes([0x02, 0x03])  # 地址不符，不该被认领

    box: dict = {}

    def worker():
        box["res"] = eng.request("010300000001", match_prefix="0103", min_reply_bytes=7)

    t = threading.Thread(target=worker)
    t.start()
    assert ch.sent.wait(1.0)
    ch.inject(_REPLY)
    t.join(2.0)
    assert box["res"].ok is True
    assert eng.timeouts == 0


def test_cancel_wakes_waiting_request():
    ch = FakeChannel()
    eng = _engine(ch, cfg=PassthroughCfg(reply_timeout_ms=5000))
    box: dict = {}
    t = threading.Thread(target=lambda: box.setdefault("res", eng.request("0103")))
    t.start()
    assert ch.sent.wait(1.0)
    assert eng.stats().pending == 1
    assert eng.cancel() is True
    assert eng.cancel() is False
    t.join(2.0)
    assert box["res"].ok is False
    assert eng.stats().pending == 0


def test_records_cursor_is_incremental():
    eng = _engine(FakeChannel())
    eng.send("0103")
    eng.send("0104")
    items, cursor = eng.records(0)
    assert len(items) == 2 and cursor > 0
    again, cursor2 = eng.records(cursor)
    assert again == [] and cursor2 == cursor
    assert eng.clear() == 2
    assert eng.all_records() == []


def test_stop_unsubscribes_and_abandons_pending():
    ch = FakeChannel()
    eng = _engine(ch)
    assert eng.subscribed is True
    assert eng.start() is True
    assert eng.stop() is True
    assert eng.subscribed is False
    assert eng.stop() is False
    assert ch.sinks == []
    ch.inject(_REPLY)
    assert eng.rx_frames == 0


# --------------------------------------------------------------------- 真链路
def test_end_to_end_over_mock_transport():
    """二进制帧走 ChannelDemux：被判为透传，并与仿真设备的 Modbus 风格应答配对。"""
    mod = MockRadioModule(name="local", seed=3)
    transport = MockByteTransport(mod, latency_ms=2, telemetry_interval_s=3600.0)
    transport.open()
    demux = ChannelDemux("pt-test", idle_gap_ms=15.0, read_timeout_ms=5)
    demux.attach(transport)
    eng = PassthroughEngine(
        demux,
        bus=EventBus(),
        cfg=PassthroughCfg(reply_timeout_ms=2000),
        endpoint="local",
        name="485",
    )
    eng.start()
    try:
        req = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x01])
        res = eng.request(req, match_prefix="0103", min_reply_bytes=7, name="读线圈")
        assert res.ok is True, res.error
        assert res.sent_bytes == req
        assert res.reply[:2] == bytes([0x01, 0x03])
        assert len(res.reply) == 7
        assert res.rtt_ms >= 0
        assert demux.stats()["frames_pt"] >= 1
        assert demux.stats()["requester"] == ""
        assert eng.summary()["rx_frames"] >= 1
    finally:
        eng.stop()
        demux.detach()
        transport.close()


def test_at_window_does_not_swallow_binary_reply():
    """回归：AT 遥询窗口开着时到达的从机 Modbus 应答必须仍走透传。

    旧实现假设"窗口内所有字节一律判给 AT"，模板下发的应答若恰在遥询窗口
    内被读线程带出，就会被整块吞进请求缓冲，造成偶发"应答超时（800 ms）"。
    """
    mod = MockRadioModule(name="local", seed=7)
    transport = MockByteTransport(mod, latency_ms=2, telemetry_interval_s=3600.0)
    transport.open()
    demux = ChannelDemux("window-test", idle_gap_ms=15.0, read_timeout_ms=5)
    demux.attach(transport)
    frames: list[bytes] = []
    demux.subscribe_passthrough(lambda f, _ts: frames.append(f))
    try:
        demux.begin("poller")
        try:
            demux.write(bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x01]))
            deadline = time.time() + 2.0
            while not frames and time.time() < deadline:
                time.sleep(0.01)
            assert frames, "AT 窗口内的二进制应答被吞进了请求缓冲"
            assert frames[0][:2] == bytes([0x01, 0x03])
            assert demux.collect("poller", 50) == b"", "二进制块不该进请求缓冲"

            demux.write(b"AT+MODE?\r\n")
            text = demux.collect("poller", 500)
            assert b"MODE" in text.upper(), "窗口内的 AT 文本应答仍应归 AT"
        finally:
            demux.release("poller")
        assert demux.stats()["requester"] == ""
    finally:
        demux.detach()
        transport.close()


# --------------------------------------------------------------------- 收帧备注回调
def test_annotate_hook_describes_rx_records():
    ch = FakeChannel()
    eng = _engine(ch, annotate=lambda data: "explained" if data == b"\x01\x02" else "")
    ch.inject(b"\x01\x02")
    ch.inject(b"\x09\x09")
    rx = _by_direction(eng, Direction.RX.value)
    assert rx[0].note == "explained"
    assert rx[1].note == "", "回调返回空串时备注保持为空"


def test_annotate_failure_leaves_note_empty():
    def boom(_data: bytes) -> str:
        raise RuntimeError("回调炸了")

    ch = FakeChannel()
    eng = _engine(ch, annotate=boom)
    ch.inject(b"\x7b\x00")
    rx = _by_direction(eng, Direction.RX.value)
    assert len(rx) == 1 and rx[0].note == ""
    assert eng.rx_frames == 1, "备注失败不许影响收包记账"
