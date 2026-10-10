"""传输层与仿真电台的离线自检（不需要任何硬件）。"""

from __future__ import annotations

import time

import pytest
import serial

from hmi.core.models import FrameMeta, TransportState
from hmi.mock import MockByteTransport, MockRadioModule
from hmi.transport.base import ByteTransport, FrameChannel


class EchoTransport(ByteTransport):
    """最小可用实现：写什么就立刻收到什么，用来验证基类队列语义。"""

    def open(self) -> None:
        self._set_state(TransportState.OPEN, "echo")

    def close(self) -> None:
        self._set_state(TransportState.CLOSED, "")

    def write(self, data: bytes) -> int:
        self._note_write(data)
        self._emit_bytes(bytes(data))
        return len(data)


def test_frame_channel_releases_in_arrival_order():
    ch = FrameChannel()
    meta = FrameMeta(width=4, height=2)
    assert ch.publish(bytearray(b"a" * 24), meta) is False
    assert ch.publish(bytearray(b"b" * 24), meta) is False, "队列未满不算丢帧"
    image, _meta, dwell = ch.pull_next()
    assert bytes(image) == b"a" * 24, "一个节拍放一帧，先到先放"
    assert dwell >= 0.0
    image, _meta, _dwell = ch.pull_next()
    assert bytes(image) == b"b" * 24
    assert ch.pull_next() is None, "队列放完即无帧"
    peeked = ch.peek()
    assert peeked is not None and bytes(peeked[0]) == b"b" * 24, "最新帧槽留给抓拍"


def test_read_once_consumes_emitted_bytes():
    t = EchoTransport(name="echo")
    t.open()
    assert t.read_once(timeout_ms=50) == b""
    t.write(b"hello")
    assert t.read_once(timeout_ms=200) == b"hello"
    assert t.bytes_out == 5 and t.bytes_in == 5
    t.close()


def test_read_once_respects_max_bytes():
    t = EchoTransport(name="echo2")
    t.open()
    t.write(b"0123456789")
    first = t.read_once(max_bytes=4, timeout_ms=100)
    assert first == b"0123"
    assert t.clear_pending() == 6


def test_mock_at_query_and_set():
    mod = MockRadioModule(name="local", seed=7)
    assert mod.handle("AT+MODE?") == "+MODE:ap\r\nOK\r\n"
    assert mod.handle("AT+MODE=sta") == "OK\r\n"
    assert mod.get("MODE") == "sta"
    assert mod.handle("AT+MODE=bogus") == "ERROR\r\n"
    assert mod.handle("AT+UNKNOWN?") == "ERROR\r\n"
    assert mod.handle("AT") == "OK\r\n"


def test_mock_reads_telemetry_with_index_or_mac():
    """手册 6.3.9/6.3.12：只读参数既能用 "?"，也能带 <id|mac> 定向读从机。"""
    mod = MockRadioModule(name="local", seed=7)
    assert mod.handle("AT+RSSI?") == "+RSSI:-72.0\r\nOK\r\n"
    assert mod.handle("AT+RSSI=1") == "+RSSI:-78.0\r\nOK\r\n"
    assert mod.handle("AT+RSSI=00:11:22:33:44:55") == "+RSSI:-78.0\r\nOK\r\n"
    assert mod.handle("AT+RSSI=9") == "sta:9 is not exist\r\n"
    assert mod.handle("AT+EVM=1") == "+EVM:-22.0\r\nOK\r\n"
    assert mod.handle("AT+STA_INFO=1").startswith("+STA_INFO:1,00:11:22:33:44:55,-78.0,")
    assert mod.handle("AT+STA_INFO=1").endswith("\r\nOK\r\n")


def test_mock_no_ok_response_forms():
    """手册 6.3.10/6.3.11：CONN_STATE 与 WNBCFG 用裸命令且响应没有 OK。"""
    mod = MockRadioModule(name="local", seed=7)
    assert mod.handle("AT+CONN_STATE") == "+CONNECTED\r\n"
    assert mod.handle("AT+WNBCFG") == "+WNBCFG:mode=ap,bw=8,chan=8680,8700,8720\r\n"
    mod.set("CONN_STATE", "disconnected")
    assert mod.handle("AT+CONN_STATE") == "+DISCONNECT\r\n"


def test_mock_action_commands():
    """写-only 动作指令：PAIR / UNPAIR / SYSDBG / LOADDEF / SCAN_AP / JOINGROUP。"""
    mod = MockRadioModule(name="local", seed=7)
    assert mod.handle("AT+PAIR=1") == "OK\r\n"
    assert mod.pairing is True
    assert mod.handle("AT+PAIR=2") == "ERROR\r\n"
    assert mod.handle("AT+UNPAIR=00:11:22:33:44:55") == "unpair sta:00:11:22:33:44:55\r\n"
    assert mod.handle("AT+UNPAIR=00:11:22:33:44:55") == "sta:00:11:22:33:44:55 is not exist\r\n"
    assert mod.handle("AT+SYSDBG=LMAC,1") == "OK\r\n"
    assert mod.sysdbg["LMAC"] == 1
    assert mod.handle("AT+SYSDBG=LMAC") == "ERROR\r\n"
    assert mod.handle("AT+SCAN_AP=3") == "OK\r\n"
    assert "hgic_ah_test" in mod.handle("AT+BSSLIST=2")
    assert mod.handle("AT+JOINGROUP=00:11:22:33:44:55,1") == "ERROR\r\n"
    mod.handle("AT+MODE=group")
    assert mod.handle("AT+JOINGROUP=00:11:22:33:44:55,1") == "OK\r\n"
    assert mod.group == ("00:11:22:33:44:55", 1)
    assert mod.handle("AT+LOADDEF=1") == "OK\r\n"
    assert mod.get("MODE") == "ap" and mod.sysdbg["LMAC"] == 0


def test_mock_at_txpower_range_validation():
    mod = MockRadioModule(name="local", seed=7)
    assert mod.handle("AT+TXPOWER=99") == "ERROR\r\n"
    assert mod.handle("AT+TXPOWER=27") == "OK\r\n"
    assert mod.get("TXPOWER") == 27


def test_mock_channel_list_and_psk():
    mod = MockRadioModule(name="local", seed=7)
    assert mod.handle("AT+CHAN_LIST=8680,8700,8720") == "OK\r\n"
    assert mod.get("CHAN_LIST") == [8680, 8700, 8720]
    assert mod.handle("AT+CHAN_LIST?") == "+CHAN_LIST:8680,8700,8720\r\nOK\r\n"
    assert mod.handle("AT+PSK=" + "ab" * 32) == "OK\r\n"
    assert mod.handle("AT+PSK=1234") == "ERROR\r\n"


def test_mock_transport_at_roundtrip():
    mod = MockRadioModule(name="local", seed=3)
    t = MockByteTransport(mod, name="mock485", latency_ms=5, telemetry_interval_s=30.0)
    t.open()
    try:
        t.write(b"AT+MODE?\r\n")
        raw = t.read_once(timeout_ms=500)
        assert b"+MODE:ap" in raw and b"OK" in raw
    finally:
        t.close()
    assert t.state is TransportState.CLOSED


def test_mock_transport_passthrough_reply():
    mod = MockRadioModule(name="local", seed=4)
    t = MockByteTransport(mod, name="mockdev", latency_ms=5, telemetry_interval_s=30.0)
    seen = []
    t.add_bytes_listener(lambda data: seen.append(data))
    t.open()
    try:
        t.write(bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x02, 0xC4, 0x0B]))
        deadline = time.monotonic() + 1.0
        while not seen and time.monotonic() < deadline:
            time.sleep(0.01)
        assert seen and seen[0][0] == 0x01 and seen[0][1] == 0x03
    finally:
        t.close()


def test_mock_transport_reports_state_changes():
    mod = MockRadioModule(name="local", seed=5)
    t = MockByteTransport(mod, name="mockstate", telemetry_interval_s=30.0)
    states = []
    t.add_state_listener(lambda st, detail: states.append(st))
    t.open()
    t.close()
    assert states[0] is TransportState.OPENING
    assert states[-1] is TransportState.CLOSED


def test_serial_transport_errors_without_hardware():
    """真实串口在无硬件时抛出可读错误，而不是静默失败。"""
    from hmi.core.config import SerialCfg
    from hmi.transport import SerialTransport

    t = SerialTransport(SerialCfg(port="COM_NOT_EXIST", auto_detect=False), name="serial485")
    with pytest.raises((RuntimeError, serial.SerialException)):
        t.open()
    assert t.state is TransportState.ERROR


def test_serial_transport_forgets_guessed_port_on_failure(monkeypatch):
    """自动推断的端口打不开要立刻忘掉，否则拔线换口后永远抱着死口重开失败。"""
    from hmi.core.config import SerialCfg
    from hmi.transport import SerialTransport
    from hmi.transport import serial_transport as st

    monkeypatch.setattr(st, "guess_port", lambda cfg: "COM_FAKE")

    def _boom(*_a, **_k):
        raise serial.SerialException("设备已拔出")

    monkeypatch.setattr(serial, "Serial", _boom)
    t = SerialTransport(SerialCfg(port="", auto_detect=True), name="serial485")
    with pytest.raises(serial.SerialException):
        t.open()
    assert t.port == ""
    assert t.state is TransportState.ERROR
    assert "COM_FAKE" in t.state_detail


UDP_HOST = "02-11-22-33-44-55"
UDP_MOD = "96-a1-3e-7d-83-88"
UDP_OTHER = "02-de-ad-be-ef-00"


class _FakeUdpSock:
    """假套接字：记录发出的帧；replies 耗尽后按 end_on_empty 决定退线程还是空转。"""

    def __init__(self, replies=(), end_on_empty: bool = False):
        self.sent: list[bytes] = []
        self.sent_addrs: list[tuple[str, int]] = []
        self._replies = list(replies)
        self.end_on_empty = end_on_empty

    def sendto(self, data, addr):
        self.sent.append(data)
        self.sent_addrs.append(addr)

    def recvfrom(self, _n):
        if self._replies:
            return self._replies.pop(0)
        if self.end_on_empty:
            raise OSError("end")
        raise TimeoutError

    def getsockname(self):
        return ("0.0.0.0", 56789)

    def close(self):
        pass


def test_frame_codec_round_trip_and_rejects():
    from hmi.transport.udp_at import (
        OP_AT,
        OP_AT_ACK,
        build_frame,
        format_mac,
        new_host_id,
        parse_frame,
        parse_mac,
    )

    host, mod = parse_mac(UDP_HOST), parse_mac(UDP_MOD)
    assert parse_mac("96:a1:3e:7d:83:88") == mod
    assert parse_mac("96A13E7D8388") == mod
    assert format_mac(mod) == UDP_MOD
    frame = build_frame(OP_AT, mod, host, b"AT+TXPOWER?")
    assert frame[:3] == bytes([OP_AT, 0, 11])
    assert frame[3:9] == mod and frame[9:15] == host, "帧头：op|00|len|dst6|src6"
    assert parse_frame(frame) == (OP_AT, mod, host, b"AT+TXPOWER?")
    assert parse_frame(b"AT+MODE?\r\n") is None, "裸 ASCII 不是本协议帧"
    assert parse_frame(build_frame(0x05, mod, host)) is None, "未知操作码要拒绝"
    assert parse_frame(build_frame(OP_AT_ACK, host, mod, b"OK")[:16]) is None, "截断帧要拒绝"
    with pytest.raises(ValueError):
        build_frame(OP_AT, mod, host, b"x" * 256)
    rid = new_host_id()
    assert len(rid) == 6 and rid[0] & 0x02 and not rid[0] & 0x01, "本机 ID 需本地管理位、非组播"


def test_udp_at_requires_target_mac_when_scan_off():
    from hmi.core.config import AtCfg, UdpAtCfg
    from hmi.transport import UdpAtTransport

    t = UdpAtTransport(
        AtCfg(), UdpAtCfg(broadcast_scan=False, target_mac=""), name="udp_at"
    )
    with pytest.raises(RuntimeError, match="target_mac"):
        t.open()
    assert not t.is_open()


def test_udp_at_open_starts_discovery_keepalive(monkeypatch):
    """open()：绑 56789 起收线程，并按 discovery_interval_ms 节拍广播 op1 保活。"""
    import time as _time

    from hmi.core.config import AtCfg, UdpAtCfg
    from hmi.transport import UdpAtTransport
    from hmi.transport.udp_at import BROADCAST_MAC, OP_DISCOVER, build_frame, parse_mac

    t = UdpAtTransport(
        AtCfg(),
        UdpAtCfg(host_id=UDP_HOST, discovery_interval_ms=50),
        name="udp_at",
    )
    fake = _FakeUdpSock()
    monkeypatch.setattr(t, "_make_socket", lambda: fake)
    t.open()
    try:
        assert t.is_open()
        assert UDP_HOST in t.state_detail, "打开状态里要能看见本机 ID"
        deadline = _time.monotonic() + 2.0
        while len(fake.sent) < 2 and _time.monotonic() < deadline:
            _time.sleep(0.01)
        assert len(fake.sent) >= 2, "保活线程必须按节拍重发 op1"
        assert build_frame(OP_DISCOVER, BROADCAST_MAC, parse_mac(UDP_HOST)) in fake.sent
        assert fake.sent_addrs[0] == ("255.255.255.255", 56789)
        assert t.discovery_sent >= 2
    finally:
        t.close()


def test_udp_at_write_frames_op3_to_target_mac():
    from hmi.core.config import AtCfg, UdpAtCfg
    from hmi.transport import UdpAtTransport
    from hmi.transport.udp_at import HEADER_LEN, OP_AT, build_frame, parse_mac

    t = UdpAtTransport(
        AtCfg(), UdpAtCfg(host_id=UDP_HOST, target_mac=UDP_MOD), name="udp_at"
    )
    fake = _FakeUdpSock()
    t.sock = fake
    n = t.write(b"AT+TXPOWER?\r\n")
    assert n == len(b"AT+TXPOWER?"), "返回 ASCII 字节数，不计帧头"
    assert fake.sent == [
        build_frame(OP_AT, parse_mac(UDP_MOD), parse_mac(UDP_HOST), b"AT+TXPOWER?")
    ], "op3 必须定向模块 MAC 且剥掉 CRLF"
    assert fake.sent_addrs == [("255.255.255.255", 56789)]
    assert len(fake.sent[0]) == HEADER_LEN + 11


def test_udp_at_write_splits_lines_and_strips_endings():
    from hmi.core.config import AtCfg, UdpAtCfg
    from hmi.transport import UdpAtTransport
    from hmi.transport.udp_at import OP_AT, parse_frame

    t = UdpAtTransport(
        AtCfg(), UdpAtCfg(host_id=UDP_HOST, target_mac=UDP_MOD), name="udp_at"
    )
    fake = _FakeUdpSock()
    t.sock = fake
    n = t.write(b"AT+ECHO=0\r\nAT+MODE?\r\n")
    parsed = [parse_frame(f) for f in fake.sent]
    assert all(p is not None and p[0] == OP_AT for p in parsed)
    assert [p[3] for p in parsed] == [b"AT+ECHO=0", b"AT+MODE?"], "按行拆帧、行尾剥净"
    assert n == 9 + 8


def test_udp_at_write_targets_discovered_module():
    from hmi.core.config import AtCfg, UdpAtCfg
    from hmi.transport import UdpAtTransport
    from hmi.transport.udp_at import OP_AT, parse_frame, parse_mac

    t = UdpAtTransport(AtCfg(), UdpAtCfg(host_id=UDP_HOST), name="udp_at")
    fake = _FakeUdpSock()
    t.sock = fake
    t._remember_module(parse_mac(UDP_MOD), "0.0.0.0")
    t.write(b"AT+RSSI?\r\n")
    op, dst, _src, text = parse_frame(fake.sent[0])
    assert (op, dst, text) == (OP_AT, parse_mac(UDP_MOD), b"AT+RSSI?"), "定向最近发现的模块"


def test_udp_at_write_without_any_module_falls_back_to_all_f():
    from hmi.core.config import AtCfg, UdpAtCfg
    from hmi.transport import UdpAtTransport
    from hmi.transport.udp_at import BROADCAST_MAC, parse_frame

    t = UdpAtTransport(
        AtCfg(), UdpAtCfg(host_id=UDP_HOST, broadcast_scan=False), name="udp_at"
    )
    fake = _FakeUdpSock()
    t.sock = fake
    t.write(b"AT+MODE?\r\n")
    assert parse_frame(fake.sent[0])[1] == BROADCAST_MAC, "拿不到 MAC 时兜底全 F 并告警"


def test_udp_at_discover_counts_op2_modules(monkeypatch):
    from hmi.core.config import AtCfg, UdpAtCfg
    from hmi.transport import UdpAtTransport
    from hmi.transport.udp_at import (
        BROADCAST_MAC,
        OP_DISCOVER,
        OP_DISCOVER_ACK,
        build_frame,
        parse_frame,
        parse_mac,
    )

    t = UdpAtTransport(
        AtCfg(), UdpAtCfg(host_id=UDP_HOST, probe_timeout_ms=80), name="udp_at"
    )
    mod, host, other = parse_mac(UDP_MOD), parse_mac(UDP_HOST), parse_mac(UDP_OTHER)
    replies = [
        (build_frame(OP_DISCOVER, BROADCAST_MAC, mod), ("192.168.1.9", 56789)),
        (b"raw noise without frame header", ("192.168.1.9", 56789)),
        (build_frame(OP_DISCOVER_ACK, other, mod), ("192.168.1.9", 56789)),
        (build_frame(OP_DISCOVER_ACK, host, mod), ("0.0.0.0", 56789)),
    ]
    fake = _FakeUdpSock(replies, end_on_empty=True)
    monkeypatch.setattr(t, "_make_socket", lambda: fake)
    assert t.discover() == 1, "只有 dst=本机 ID 的 op2 才计数"
    assert t.discovered_macs() == [UDP_MOD]
    assert t.last_source == "0.0.0.0", "模块响应源 IP 恒为 0.0.0.0，也要记下来"
    assert fake.sent and parse_frame(fake.sent[0])[0] == OP_DISCOVER
    assert all(a == ("255.255.255.255", 56789) for a in fake.sent_addrs)


def test_udp_at_loop_strips_headers_and_filters_frames(monkeypatch):
    """收线程：只把发给本机 ID 的 op4 剥头投递；op1 自回环/异主机/非协议帧分类计数。"""
    from hmi.core.config import AtCfg, UdpAtCfg
    from hmi.transport import UdpAtTransport
    from hmi.transport.udp_at import (
        BROADCAST_MAC,
        OP_AT_ACK,
        OP_DISCOVER,
        OP_DISCOVER_ACK,
        build_frame,
        parse_mac,
    )

    host, mod, other = parse_mac(UDP_HOST), parse_mac(UDP_MOD), parse_mac(UDP_OTHER)
    packets = [
        (build_frame(OP_AT_ACK, host, mod, b"+TXPOWER:6dbm\r\nOK\r\n"), ("0.0.0.0", 56789)),
        (build_frame(OP_DISCOVER, BROADCAST_MAC, host), ("0.0.0.0", 56789)),
        (build_frame(OP_AT_ACK, other, mod, b"+MODE:ap"), ("0.0.0.0", 56789)),
        (build_frame(OP_DISCOVER_ACK, host, mod, b""), ("0.0.0.0", 56789)),
        (b"AT+MODE?\r\n", ("0.0.0.0", 56789)),
    ]

    t = UdpAtTransport(AtCfg(), UdpAtCfg(host_id=UDP_HOST), name="udp_at")
    got: list[bytes] = []
    monkeypatch.setattr(t, "_emit_bytes", lambda data: got.append(data))
    t.sock = _FakeUdpSock(packets, end_on_empty=True)
    t._stop.clear()
    t._loop()
    assert got == [b"+TXPOWER:6dbm\r\nOK\r\n"], "只放行发给本机的 op4，且剥净二进制帧头"
    assert t.frames_foreign == 1, "发给其它主机的帧计入 foreign"
    assert t.frames_bad == 1, "裸 ASCII 包计入非协议"
    assert UDP_MOD in t.discovered_macs(), "收线程也要把 op2 记进模块表"


def test_udp_at_write_explicit_target_overrides_discovered():
    """显式 target_mac 优先于模块表里的其它模块，并记为期望应答源。"""
    from hmi.core.config import AtCfg, UdpAtCfg
    from hmi.transport import UdpAtTransport
    from hmi.transport.udp_at import OP_AT, parse_frame, parse_mac

    t = UdpAtTransport(AtCfg(), UdpAtCfg(host_id=UDP_HOST), name="udp_at")
    fake = _FakeUdpSock()
    t.sock = fake
    t._remember_module(parse_mac(UDP_OTHER), "0.0.0.0")
    t._remember_module(parse_mac(UDP_MOD), "0.0.0.0")
    t.write(b"AT+RSSI?\r\n", target_mac=UDP_MOD)
    op, dst, _src, text = parse_frame(fake.sent[0])
    assert (op, dst, text) == (OP_AT, parse_mac(UDP_MOD), b"AT+RSSI?")
    assert t._at_target == parse_mac(UDP_MOD), "写后要记住期望的应答源"


def test_udp_at_write_unknown_explicit_target_fails_after_handshake():
    """目标不在模块表：先补一次 op1 定向握手；握手等不到它就明确失败，
    绝不把 AT 帧静默定向到一台不应答的 MAC（那会顺带丢弃另一台的应答）。"""
    from hmi.core.config import AtCfg, UdpAtCfg
    from hmi.transport import UdpAtTransport
    from hmi.transport.udp_at import OP_AT, OP_DISCOVER, parse_frame, parse_mac

    t = UdpAtTransport(
        AtCfg(),
        UdpAtCfg(host_id=UDP_HOST, probe_timeout_ms=60),
        name="udp_at",
    )
    fake = _FakeUdpSock()
    t.sock = fake
    with pytest.raises(OSError, match="离线"):
        t.write(b"AT+RSSI?\r\n", target_mac="96:a1:3e:7d:83:88")
    ops = [parse_frame(f)[0] for f in fake.sent]
    assert ops and ops[0] == OP_DISCOVER, "先补发现"
    assert OP_AT not in ops, "确认离线就不该把 AT 帧放上线"
    assert t._at_target == b"", "失败路径不得污染期望应答源"


def test_udp_at_write_target_surfaced_during_handshake_is_sent():
    """定向握手期间模块表里出现该 MAC：帧仍按目标定向发出并记为期望应答源。"""
    import threading

    from hmi.core.config import AtCfg, UdpAtCfg
    from hmi.transport import UdpAtTransport
    from hmi.transport.udp_at import OP_AT, parse_frame, parse_mac

    t = UdpAtTransport(
        AtCfg(),
        UdpAtCfg(host_id=UDP_HOST, probe_timeout_ms=800),
        name="udp_at",
    )
    fake = _FakeUdpSock()
    t.sock = fake
    want = parse_mac(UDP_MOD)
    threading.Timer(0.1, lambda: t._remember_module(want, "0.0.0.0")).start()
    t.write(b"AT+RSSI?\r\n", target_mac=UDP_MOD)
    at = [f for f in fake.sent if parse_frame(f)[0] == OP_AT]
    assert len(at) == 1 and parse_frame(at[0])[1] == want
    assert t._at_target == want


def test_udp_at_write_broadcast_target_clears_expected_src():
    from hmi.core.config import AtCfg, UdpAtCfg
    from hmi.transport import UdpAtTransport
    from hmi.transport.udp_at import BROADCAST_MAC, parse_frame, parse_mac

    t = UdpAtTransport(
        AtCfg(), UdpAtCfg(host_id=UDP_HOST, broadcast_scan=False), name="udp_at"
    )
    fake = _FakeUdpSock()
    t.sock = fake
    t._at_target = parse_mac(UDP_MOD)
    t.write(b"AT+MODE?\r\n", target_mac=BROADCAST_MAC)
    assert parse_frame(fake.sent[0])[1] == BROADCAST_MAC
    assert t._at_target == b"", "全 F 广播时收线程不该按源 MAC 过滤"


def test_udp_at_modules_reports_online_within_ttl():
    import time as _time

    from hmi.core.config import AtCfg, UdpAtCfg
    from hmi.transport import UdpAtTransport
    from hmi.transport.udp_at import parse_mac

    t = UdpAtTransport(AtCfg(), UdpAtCfg(host_id=UDP_HOST), name="udp_at")
    mod, other = parse_mac(UDP_MOD), parse_mac(UDP_OTHER)
    t._remember_module(mod, "0.0.0.0")
    t._remember_module(other, "192.168.1.5")
    rows = t.modules()
    assert [r["mac"] for r in rows] == [UDP_MOD, UDP_OTHER]
    assert all(r["online"] for r in rows)
    assert rows[1]["source_ip"] == "192.168.1.5"
    t._modules[other] = (_time.monotonic() - 999.0, "192.168.1.5")
    rows = t.modules()
    assert rows[0]["online"] is True and rows[1]["online"] is False, "超 TTL 记离线"
    assert t._has_module(mod) and not t._has_module(other)


def test_udp_at_loop_drops_op4_from_unselected_module(monkeypatch):
    """设了期望应答源后，其它模块串话的 op4 只计数不投递。"""
    from hmi.core.config import AtCfg, UdpAtCfg
    from hmi.transport import UdpAtTransport
    from hmi.transport.udp_at import OP_AT_ACK, build_frame, parse_mac

    host, mod, other = parse_mac(UDP_HOST), parse_mac(UDP_MOD), parse_mac(UDP_OTHER)
    packets = [
        (build_frame(OP_AT_ACK, host, other, b"+RSSI:-30"), ("0.0.0.0", 56789)),
        (build_frame(OP_AT_ACK, host, mod, b"+RSSI:-9\r\nOK\r\n"), ("0.0.0.0", 56789)),
    ]
    t = UdpAtTransport(AtCfg(), UdpAtCfg(host_id=UDP_HOST), name="udp_at")
    t._at_target = mod
    got: list[bytes] = []
    monkeypatch.setattr(t, "_emit_bytes", lambda data: got.append(data))
    t.sock = _FakeUdpSock(packets, end_on_empty=True)
    t._stop.clear()
    t._loop()
    assert got == [b"+RSSI:-9\r\nOK\r\n"]
    assert t.frames_other_src == 1
    assert "frames_other_src" in t.info()
