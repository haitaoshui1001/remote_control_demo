"""at_codec 与 ChannelDemux 的离线测试：分流判据是整个方案里最容易踩坑的一环。"""

from __future__ import annotations

import time

from hmi.protocol.at_codec import (
    CHUNK_AT,
    CHUNK_PASS,
    LineSplitter,
    classify,
    classify_chunk,
    format_at,
    normalize_name,
    parse_bool_state,
    parse_float,
    parse_int,
    parse_int_list,
    parse_kv_pairs,
    split_command,
    split_value_line,
)
from hmi.protocol.channel import ChannelDemux


def test_classify_line_kinds():
    assert classify("OK").is_ok
    assert classify("ERROR").is_error
    # 默认失败词表是"行首匹配"，自定义失败文本要靠 err_tokens 显式登记
    free_text = classify("sta:9 is not exist")
    assert free_text.kind == "other" and not free_text.is_error
    assert classify("sta:9 is not exist", err_tokens=("ERROR", "STA:")).is_error
    value = classify("+RSSI:-72.0")
    assert value.kind == "value" and value.name == "RSSI" and value.value == "-72.0"
    bare = classify("+CONNECTED")
    assert bare.kind == "value" and bare.name == "CONNECTED" and bare.payload() == "CONNECTED"
    assert classify("AT+MODE?", command="AT+MODE?").kind == "echo"
    assert classify("@DEV rssi=-71").kind == "other"
    assert classify("").kind == "other"


def test_value_line_with_colon_in_body_keeps_name_intact():
    """``+PAIR_SUCCESS <mac>``：正文里的冒号不能把参数名切成 ``PAIR_SUCCESS 00``。

    链路监控靠这个名字判断"配对完成"，名字被截断就再也认不出这是配对结果。
    """
    pair = classify("+PAIR_SUCCESS 00:11:22:33:44:55")
    assert pair.kind == "value"
    assert pair.name == "PAIR_SUCCESS"
    assert pair.value == "00:11:22:33:44:55"
    assert pair.payload() == "00:11:22:33:44:55"
    # 收口的拆行函数与 classify 同口径，response_value 也走它
    assert split_value_line("+PAIR_SUCCESS 00:11:22:33:44:55") == (
        "PAIR_SUCCESS",
        "00:11:22:33:44:55",
    )
    assert split_value_line("+CONNECTED") == ("CONNECTED", "")
    assert split_value_line("+WNBCFG:mode=ap, bw=8") == ("WNBCFG", "mode=ap, bw=8")
    # 带空格后缀的只读响应仍按冒号切，正文完整保留
    sta = classify("+STA_INFO:1,00:11:22:33:44:55,-78.0,-22.0")
    assert sta.name == "STA_INFO" and sta.value == "1,00:11:22:33:44:55,-78.0,-22.0"


def test_normalize_and_split_command():
    assert normalize_name("+AT+CHAN_LIST:9080,9160") == "CHAN_LIST"
    assert normalize_name("AT+MODE") == "MODE"
    assert normalize_name("+WNBCFG:mode=ap") == "WNBCFG"
    assert split_command("AT+CHAN_LIST=8680,8700") == ("CHAN_LIST", "8680,8700", False)
    assert split_command("AT+RSSI?") == ("RSSI", None, True)
    assert split_command("+RSSI:-72")[0] == ""


def test_format_at_forms():
    assert format_at("MODE", query=True) == "AT+MODE?"
    assert format_at("MODE", "ap") == "AT+MODE=ap"
    assert format_at("CONN_STATE") == "AT+CONN_STATE"
    assert format_at("SYSDBG", "LMAC,1") == "AT+SYSDBG=LMAC,1"


def test_parsers_tolerate_units_and_shapes():
    assert parse_int("20dbm") == 20
    assert parse_float("-72.4 dBm") == -72.4
    assert parse_int("-") is None
    assert parse_int_list("8680,8700;8720") == [8680, 8700, 8720]
    assert parse_int_list("863500-928000") == [863500, 928000]
    assert parse_int_list("nope") == []
    # kv 以逗号/分号/空格为分隔，所以"一个值里带逗号"会被截断——解析层如实反映
    assert parse_kv_pairs("mode=ap,bw=8,chan=8680") == {"mode": "ap", "bw": "8", "chan": "8680"}
    assert parse_kv_pairs("+WNBCFG: mode=group bw=4 chan=8700")["mode"] == "group"
    assert parse_kv_pairs("chan=8680,8700")["chan"] == "8680"
    assert parse_int_list(parse_kv_pairs("chan=8680,8700")["chan"] + ",8700") == [8680, 8700]
    assert parse_kv_pairs("没有等号的整行") == {}
    assert parse_bool_state("connected") is True
    assert parse_bool_state("disconnect") is False
    assert parse_bool_state("0") is False
    assert parse_bool_state("3") is True
    assert parse_bool_state("") is None


def test_zero_values_are_not_swallowed():
    """``0`` 和 ``False`` 是合法响应值，不能被"空值兜底"写法吃掉。

    原先解析器普遍写 ``str(text or "")``，于是 ``AT+TXPOWER?`` 回到 0、
    ``AT+ACKTMO?=0``、``+DISCONNECT`` 之后再取一次数值，信道页会显示成空白，
    操作员误以为"模块没上报"。这里把口径钉死：只有 ``None`` 才算空。
    """
    assert parse_int(0) == 0
    assert parse_int("0") == 0
    assert parse_float(0) == 0.0
    assert parse_float("0.0") == 0.0
    assert parse_int_list("0") == [0]
    assert parse_bool_state(False) is False
    assert parse_bool_state(True) is True
    assert parse_bool_state("false") is False
    # 真正该返回 None 的仍然返回 None
    assert parse_int(None) is None
    assert parse_int("") is None
    assert parse_float("N/A") is None
    assert parse_bool_state(None) is None


def test_line_splitter_handles_partial_tail():
    sp = LineSplitter()
    assert sp.feed(b"AT+MODE=ap\r\n+M") == ["AT+MODE=ap"]
    assert sp.pending() == b"+M"
    assert sp.feed(b"ODE=ap\r\n") == ["+MODE=ap"]
    assert sp.pending() == b""
    sp.feed(b"+EVM:-2")
    assert sp.pending() == b"+EVM:-2"
    sp.clear()
    assert sp.pending() == b""


def test_classify_chunk_is_conservative():
    markers = ("+", "AT+")
    assert classify_chunk(b"+RSSI:-72\r\nOK\r\n", markers) == CHUNK_AT
    assert classify_chunk(b"OK\r\n", markers + ("OK",)) == CHUNK_AT
    assert classify_chunk(b"AT+MODE?\r\n", markers) == CHUNK_AT
    assert classify_chunk(b"@DEV rssi=-71 tx=1\r\n", markers) == CHUNK_PASS
    assert classify_chunk(bytes([1, 3, 4, 0, 1, 2, 0, 5]), markers) == CHUNK_PASS
    assert classify_chunk(b"\xff\x00plain text", markers) == CHUNK_PASS
    assert classify_chunk(b"", markers) == CHUNK_PASS


class _FakeTransport:
    """最小假通道：满足 ChannelDemux 需要的 read_once/write/状态监听接口。"""

    def __init__(self) -> None:
        self.queue: list[bytes] = []
        self.written: list[bytes] = []
        self.name = "fake"

    def add_state_listener(self, fn) -> None:
        pass

    def remove_state_listener(self, fn) -> bool:
        return True

    def read_once(self, max_bytes: int = 4096, timeout_ms: int = 50) -> bytes:
        if self.queue:
            return self.queue.pop(0)
        time.sleep(min(timeout_ms, 5) / 1000.0)
        return b""

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data)


def test_demux_splits_at_and_passthrough():
    tr = _FakeTransport()
    demux = ChannelDemux("t", idle_gap_ms=1.0, read_timeout_ms=1)
    demux.attach(tr, start=False)
    at_lines: list[str] = []
    frames: list[bytes] = []
    demux.subscribe_at(at_lines.append)
    demux.subscribe_passthrough(lambda d, ts: frames.append(d))

    demux._dispatch(b"+CONNECTED\r\n")
    demux.flush_now()
    demux._dispatch(bytes([1, 3, 4, 0, 1]))
    demux.flush_now()
    demux._dispatch(b"@DEV rssi=-71\r\n")
    demux.flush_now()

    assert at_lines == ["+CONNECTED"]
    assert frames == [bytes([1, 3, 4, 0, 1]), b"@DEV rssi=-71\r\n"]
    st = demux.stats()
    assert st["bytes_at"] == 12 and st["frames_pt"] == 2
    assert not st["requester"]


def test_demux_frames_passthrough_by_idle_gap():
    """连续到达的二进制块属于同一次发送，必须合成一帧，不能被 read 边界切碎。"""
    demux = ChannelDemux("t", idle_gap_ms=1000.0, read_timeout_ms=1)
    demux.attach(_FakeTransport(), start=False)
    frames: list[bytes] = []
    demux.subscribe_passthrough(lambda d, ts: frames.append(d))

    demux._dispatch(b"\x01\x03")
    demux._dispatch(b"\x04\x00\x01")
    assert frames == []
    demux.flush_now()
    assert frames == [b"\x01\x03\x04\x00\x01"]
    assert demux.stats()["frames_pt"] == 1


def test_demux_window_keeps_text_but_not_binary():
    """窗口内只收纯 ASCII 文本；二进制块归透传——旧"全吞"假设会造成透传假超时。"""
    tr = _FakeTransport()
    demux = ChannelDemux("t", idle_gap_ms=1.0, read_timeout_ms=1)
    demux.attach(tr, start=False)
    frames: list[bytes] = []
    demux.subscribe_passthrough(lambda d, ts: frames.append(d))

    demux.begin("tok")
    demux._dispatch(b"AT+MODE?\r\n")
    demux._dispatch(bytes([1, 3, 0, 1]))
    got = demux.collect("tok", 20)
    assert got == b"AT+MODE?\r\n"
    assert b"\x01\x03" not in got
    demux.flush_now()
    assert frames == [bytes([1, 3, 0, 1])]
    left = demux.release("tok")
    assert left == b""
    assert demux.stats()["bytes_pt"] == 4
