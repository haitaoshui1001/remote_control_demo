"""AT 客户端的链路测试：用仿真电台驱动真实的"唯一读者 + 请求窗口"通路。

这一层要守的纪律比解析多，而且每条都对应现场会遇到的坑：
1. **校验类失败不占通道**——注册表拦下来的值根本不该发出去，UI 要能区分
   "是软件拦的"和"模块没答"（``rejected`` 与 ``error`` 分开计数）；
2. **没有 OK 的响应形态**——要么"值行即成功"（``Q_BARE``），要么"末行之后
   静默够久"才算对话结束，否则多行响应会被截断成一行；
3. **超时是唯一可重试的错误**——模块明确回了 ERROR 还重发，只会污染总线并
   把现场排查时间拖长；
4. **自发上报不能被当成应答**——``+PAIR_SUCCESS <mac>`` 这类行只在空闲期计数。
"""

from __future__ import annotations

import time
from typing import Any, NamedTuple

import pytest

from hmi.core.config import AtCfg
from hmi.core.events import EventBus, Events
from hmi.core.models import AtChannel, AtExchange, Endpoint, RadioParams
from hmi.mock.radio import MockByteTransport, MockRadioModule
from hmi.protocol import param_registry as registry
from hmi.protocol.at_client import AtClient, AtError, failure_line, response_value
from hmi.protocol.channel import ChannelDemux

#: 测试里把"静默即结束"的窗口压到 120ms，既够仿真线程排空响应，又不拖慢用例
_GRACE_MS = 120.0


class Harness(NamedTuple):
    client: AtClient
    demux: ChannelDemux
    transport: MockByteTransport
    module: MockRadioModule
    bus: EventBus
    events: list[AtExchange]


class SilentTransport:
    """只收不答的通道：专门用来验证超时/重试，不必伪造整个 AT 解释器。"""

    name = "silent485"

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def is_open(self) -> bool:
        return True

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def read_once(self, max_chunk: int, timeout_ms: int) -> bytes:
        time.sleep(min(max(0, int(timeout_ms)), 20) / 1000.0)
        return b""

    def add_state_listener(self, callback: Any) -> None:
        pass

    def remove_state_listener(self, callback: Any) -> None:
        pass


def _wait_for(predicate, timeout_s: float = 2.0) -> bool:
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _harness(module: MockRadioModule, **cfg_overrides: Any) -> Harness:
    transport = MockByteTransport(
        module, name="mock485", latency_ms=0, telemetry_interval_s=3600.0
    )
    transport.open()
    demux = ChannelDemux("at-test", idle_gap_ms=20.0, read_timeout_ms=5)
    demux.attach(transport)
    bus = EventBus()
    events: list[AtExchange] = []
    bus.subscribe(Events.AT_EXCHANGE, lambda topic, payload: events.append(payload))
    cfg = AtCfg(timeout_ms=1500, retries=0, pre_silence_ms=0, wake_sequence="")
    for key, value in cfg_overrides.items():
        if not hasattr(cfg, key):
            raise AttributeError(f"AtCfg 没有字段 {key}")
        setattr(cfg, key, value)
    client = AtClient(
        cfg,
        demux,
        channel=AtChannel.SERIAL.value,
        endpoint=Endpoint.LOCAL.value,
        bus=bus,
        value_grace_ms=_GRACE_MS,
    )
    return Harness(client, demux, transport, module, bus, events)


def _teardown(h: Harness) -> None:
    h.demux.stop()
    h.transport.close()


@pytest.fixture
def harness():
    h = _harness(MockRadioModule(name="local", seed=7))
    try:
        yield h
    finally:
        _teardown(h)


# --------------------------------------------------------------------- 查询/设置
def test_query_returns_decoded_value_not_raw_text(harness: Harness) -> None:
    """``query_value`` 给的是注册表解码后的 Python 值，原样文本另算一份。"""
    ok, value, raw = harness.client.query_value("MODE")
    assert (ok, value, raw) == (True, "ap", "ap")
    assert harness.client.stats()["sent"] == 1


def test_channel_list_stays_in_wire_units(harness: Harness) -> None:
    """频点在注册表里**全程用线上单位**（MHz×10），换算只发生在信道遥测显示层。

    这条钉住一个容易改错的地方：如果 encode/decode 偷偷各乘除一次 10，
    UI 填的频点和模块实际跳的点就差一位数量级。
    """
    assert harness.client.set("CHAN_LIST", [8680, 8700]).ok is True
    assert harness.module.get("CHAN_LIST") == [8680, 8700]
    ok, value, raw = harness.client.query_value("CHAN_LIST")
    assert ok is True
    assert value == [8680, 8700]
    assert raw == "8680,8700"


def test_set_then_query_roundtrip(harness: Harness) -> None:
    assert harness.client.set("TXPOWER", 14).ok is True
    ok, value, _raw = harness.client.query_value("TXPOWER")
    assert (ok, value) == (True, 14)
    assert harness.module.get("TXPOWER") == 14


def test_zero_value_survives_the_whole_path(harness: Harness) -> None:
    """``ACKTMO=0`` 是合法值，不能被 ``or ""`` 之类的写法吞成空串。"""
    assert harness.client.set("ACKTMO", 0).ok is True
    ok, value, raw = harness.client.query_value("ACKTMO")
    assert ok is True
    assert value == 0
    assert raw == "0"


# ------------------------------------------------------------------ 校验类失败
def test_validation_failure_never_touches_channel(harness: Harness) -> None:
    """校验不通过时 sent 必须还是 0：现场要能分清"软件拦的"和"模块没答"。"""
    low = harness.client.set("TXPOWER", 3)
    ro = harness.client.set("RSSI", -50)
    unknown = harness.client.query("NOT_A_PARAM")
    assert low.ok is False and ro.ok is False and unknown.ok is False
    assert "只读" in ro.error
    assert "未知参数" in unknown.error
    st = harness.client.stats()
    assert st["sent"] == 0
    assert st["rejected"] == 3
    assert st["error"] == 0
    assert [e.kind for e in harness.events] == ["set", "set", "query"]
    assert [e.ok for e in harness.events] == [False, False, False]


def test_range_override_relaxes_selftest(harness: Harness) -> None:
    """自检可以临时放宽量程，但必须能还原，且还原后重新拦住。"""
    assert harness.client.set("TXPOWER", 3).ok is False
    registry.set_range_override("TXPOWER", 0, 30)
    try:
        assert harness.client.set("TXPOWER", 3).ok is True
        assert harness.module.get("TXPOWER") == 3
    finally:
        registry.clear_range_overrides()
    assert harness.client.set("TXPOWER", 3).ok is False


# ------------------------------------------------------------- 没有 OK 的响应形态
def test_bare_query_accepts_value_line_without_ok(harness: Harness) -> None:
    """手册上 ``AT+CONN_STATE`` 回 ``+CONNECTED`` 且没有 OK，值行就该判成功。"""
    ex = harness.client.query("CONN_STATE")
    assert ex.ok is True
    assert ex.line == "AT+CONN_STATE"
    assert ex.response == ["+CONNECTED"]
    ok, value, raw = harness.client.query_value("CONN_STATE")
    assert (ok, value, raw) == (True, True, "CONNECTED")


def test_bare_query_falls_back_to_question_mark_dialect(harness: Harness) -> None:
    """个别固件只认 ``AT+NAME?``；被直接回 ERROR 时补发一次带问号的写法。"""
    harness.module.force_error = "ERROR"
    ex = harness.client.query("WNBCFG")
    assert ex.ok is True
    assert ex.line == "AT+WNBCFG?"
    assert harness.client.stats()["sent"] == 2
    assert "mode=ap" in response_value(ex, "WNBCFG")


def test_silence_after_last_line_ends_the_dialog(harness: Harness) -> None:
    """既没有 OK 也不是 ``Q_BARE``：靠"末行之后静默够久"收尾，而不是收到首行就走。"""
    ex = harness.client.request("AT+WNBCFG", name="WNBCFG", kind="query")
    assert ex.ok is True
    assert ex.response == ["+WNBCFG:mode=ap,bw=8,chan=8680,8700,8720"]
    assert ex.rtt_ms >= _GRACE_MS * 0.6


# ---------------------------------------------------------------- 失败与重试
def test_error_response_is_not_retried() -> None:
    """模块明确回了 ERROR 就不该重发，retries 只给"根本没答"用。"""
    h = _harness(MockRadioModule(name="local", seed=3), retries=2)
    try:
        h.module.force_error = "ERROR"
        ex = h.client.query("MODE")
        assert ex.ok is False
        assert ex.error == "ERROR"
        st = h.client.stats()
        assert st["sent"] == 1
        assert st["error"] == 1
        assert st["retry"] == 0
    finally:
        _teardown(h)


def test_timeout_is_the_only_retriable_error() -> None:
    transport = SilentTransport()
    demux = ChannelDemux("silent-test", idle_gap_ms=20.0, read_timeout_ms=5)
    demux.attach(transport)
    client = AtClient(
        AtCfg(timeout_ms=120, retries=1, pre_silence_ms=0, wake_sequence=""),
        demux,
        value_grace_ms=_GRACE_MS,
    )
    try:
        ex = client.query("MODE")
        assert ex.ok is False
        assert ex.error == "超时未响应"
        st = client.stats()
        assert st["sent"] == 2
        assert st["timeout"] == 2
        assert st["retry"] == 1
        assert len(transport.writes) == 2
    finally:
        demux.stop()


def test_wake_mode_off_fails_every_command(harness: Harness) -> None:
    harness.module.wake_mode = False
    try:
        ex = harness.client.query("SSID")
        assert ex.ok is False
        assert harness.client.stats()["timeout"] == 0
    finally:
        harness.module.wake_mode = True


def test_closed_channel_raises_at_error(harness: Harness) -> None:
    harness.transport.close()
    with pytest.raises(AtError, match="未打开"):
        harness.client.query("MODE")


def test_unsent_attempt_still_leaves_a_trace(harness: Harness) -> None:
    """通道没打开时点了也必须留痕：日志页空白只可能是"没点"。"""
    harness.transport.close()
    before = len(harness.events)
    with pytest.raises(AtError, match="未打开"):
        harness.client.query("MODE")
    assert len(harness.events) == before + 1
    ex = harness.events[-1]
    assert ex.ok is False and ex.line == "AT+MODE?" and ex.name == "MODE"
    assert "未打开" in ex.error
    assert harness.client.stats()["error"] >= 1


def test_closed_reason_carries_transport_detail() -> None:
    """传输层留有失败原因时，未上线提示要把它带上，用户才知为什么打不开。"""

    class _ClosedWithDetail:
        name = "serial485"
        state_detail = "打不开：Access denied（被其他程序占用）"

        def is_open(self) -> bool:
            return False

    bus = EventBus()
    events: list[AtExchange] = []
    bus.subscribe(Events.AT_EXCHANGE, lambda _t, p: events.append(p))
    cfg = AtCfg(timeout_ms=200, retries=0, pre_silence_ms=0, wake_sequence="")
    demux = ChannelDemux("detail-test", idle_gap_ms=20.0, read_timeout_ms=5)
    demux.attach(_ClosedWithDetail(), start=False)
    client = AtClient(cfg, demux, channel=AtChannel.SERIAL.value, bus=bus)
    try:
        with pytest.raises(AtError, match="Access denied"):
            client.query("MODE")
        assert "Access denied" in events[-1].error
    finally:
        demux.stop()


def test_unbound_and_broken_write_leave_traces() -> None:
    """没绑定传输层、写串口抛异常，两条静默路径同样要记进 AT 日志。"""
    bus = EventBus()
    events: list[AtExchange] = []
    bus.subscribe(Events.AT_EXCHANGE, lambda _t, p: events.append(p))
    cfg = AtCfg(timeout_ms=200, retries=0, pre_silence_ms=0, wake_sequence="")

    bare = ChannelDemux("unbound", idle_gap_ms=20.0, read_timeout_ms=5)
    client = AtClient(cfg, bare, channel=AtChannel.SERIAL.value, bus=bus)
    with pytest.raises(AtError, match="未绑定传输层"):
        client.query("MODE")

    class _BrokenWrite(SilentTransport):
        def write(self, data: bytes) -> int:
            raise OSError("设备被拔掉")

    demux = ChannelDemux("broken", idle_gap_ms=20.0, read_timeout_ms=5)
    demux.attach(_BrokenWrite())
    client2 = AtClient(cfg, demux, channel=AtChannel.SERIAL.value, bus=bus)
    with pytest.raises(AtError, match="发送失败"):
        client2.request("AT+MODE=?", name="MODE", kind="query")
    demux.stop()

    assert [e.ok for e in events] == [False, False]
    assert "未绑定传输层" in events[0].error
    assert "设备被拔掉" in events[1].error
    assert events[1].line == "AT+MODE=?"


def test_empty_command_raises(harness: Harness) -> None:
    with pytest.raises(AtError):
        harness.client.request("   ")


# --------------------------------------------------------------------- 动作指令
def test_action_pair_unpair_and_unknown_sta(harness: Harness) -> None:
    assert harness.client.action("PAIR", 1).ok is True
    assert harness.module.pairing is True
    assert harness.client.action("PAIR", 0).ok is True
    assert harness.module.pairing is False

    ex = harness.client.action("UNPAIR", "00:11:22:33:44:55")
    assert ex.ok is True
    assert harness.module.sta_table == []
    # "sta:xx is not exist" 既不是 + 行也不在 err_tokens 里，靠失败词表兜住
    miss = harness.client.action("UNPAIR", "ff:ff:ff:ff:ff:ff")
    assert miss.ok is False
    assert "not exist" in miss.error


def test_scan_ap_fills_the_list(harness: Harness) -> None:
    assert harness.client.action("SCAN_AP", 3).ok is True
    ok, value, _raw = harness.client.query_value("BSSLIST")
    assert ok is True
    assert value


# ------------------------------------------------------------------ 批量下发
def test_apply_pushes_only_touched_fields(harness: Harness) -> None:
    exs = harness.client.apply(RadioParams(mode="sta", ssid="hgic_field", txpower=12))
    assert len(exs) == 3
    assert [e.ok for e in exs] == [True, True, True]
    assert harness.client.stats()["sent"] == 3
    assert harness.module.get("MODE") == "sta"
    assert harness.module.get("SSID") == "hgic_field"
    assert harness.module.get("TXPOWER") == 12


def test_apply_plan_rejects_before_touching_channel(harness: Harness) -> None:
    exs = harness.client.apply(RadioParams(ssid="x" * 40))
    assert len(exs) == 1
    assert exs[0].ok is False
    assert harness.client.stats()["sent"] == 0


def test_apply_stop_on_error_halts_the_sequence(harness: Harness) -> None:
    harness.module.wake_mode = False
    try:
        halted = harness.client.apply(
            RadioParams(mode="sta", txpower=12), stop_on_error=True
        )
        assert len(halted) == 1
        assert halted[0].ok is False
        before = harness.client.stats()["sent"]
        full = harness.client.apply(RadioParams(mode="sta", txpower=12), stop_on_error=False)
        assert len(full) == 2
        assert harness.client.stats()["sent"] == before + 2
    finally:
        harness.module.wake_mode = True


def test_read_many_isolates_failures(harness: Harness) -> None:
    out = harness.client.read_many(["MODE", "RSSI", "NOT_A_PARAM"])
    assert out["MODE"][0] is True and out["MODE"][1] == "ap"
    assert out["RSSI"][0] is True
    assert out["NOT_A_PARAM"][0] is False
    assert "未知参数" in out["NOT_A_PARAM"][2]
    assert harness.client.stats()["sent"] == 2


# ----------------------------------------------------------------- 通道与状态
def test_request_window_is_released_after_dialog(harness: Harness) -> None:
    """对话结束必须交还通道，否则透传会被永久拒发。"""
    assert not harness.demux.stats()["requester"]
    harness.client.query("VERSION")
    st = harness.demux.stats()
    assert not st["requester"]
    assert harness.client.busy() is False
    assert st["bytes_at"] > 0


def test_echo_suppression_keeps_command_out_of_response(harness: Harness) -> None:
    """仿真电台回显命令行；关掉抑制开关后响应里就该看到那一行。"""
    ex = harness.client.query("SSID")
    assert ex.response == ["+SSID:hgic_ah_test", "OK"]
    harness.client.configure(echo_suppress=False)
    raw = harness.client.query("SSID")
    assert "AT+SSID?" in raw.response


def test_wake_sequence_and_channel_switch_flags() -> None:
    """唤醒序列只在第一次对话前发一次；换通道/改配置后要重新唤醒。"""
    h = _harness(MockRadioModule(name="local", seed=1), wake_sequence="AT")
    try:
        assert h.client.stats()["awake"] is False
        ex = h.client.query("VERSION")
        assert ex.ok is True
        assert h.client.stats()["awake"] is True
        h.client.mark_channel(AtChannel.UDP.value, Endpoint.REMOTE.value)
        st = h.client.stats()
        assert st["awake"] is False
        assert st["channel"] == AtChannel.UDP.value
        assert st["endpoint"] == Endpoint.REMOTE.value
    finally:
        _teardown(h)


def test_unsolicited_pair_success_keeps_name(harness: Harness) -> None:
    """空闲期自发上报只计数，而且 ``+PAIR_SUCCESS <mac>`` 的名字不能被截断。"""
    harness.client.attach_unsolicited()
    harness.module.push("+PAIR_SUCCESS 00:11:22:33:44:55\r\n")
    assert _wait_for(lambda: harness.client.stats()["unsolicited"] == 1)
    pair = [e for e in harness.events if e.kind == "unsolicited"][-1]
    assert pair.name == "PAIR_SUCCESS"
    assert pair.response == ["+PAIR_SUCCESS 00:11:22:33:44:55"]
    assert response_value(pair) == "00:11:22:33:44:55"
    assert harness.client.stats()["sent"] == 0


# ------------------------------------------------------------------- 纯函数契约
def test_response_value_matches_name_then_first_plus_line() -> None:
    ex = AtExchange(line="AT+RSSI?", kind="query", name="RSSI", response=["+RSSI:-72.0", "OK"])
    assert response_value(ex, "RSSI") == "-72.0"
    assert response_value(ex) == "-72.0"
    # 响应名和指令名对不上时退回第一个 + 行正文
    state = AtExchange(line="AT+CONN_STATE", response=["+CONNECTED"])
    assert response_value(state, "CONN_STATE") == "CONNECTED"
    # 正文里的冒号（MAC）不能把名字切成 PAIR_SUCCESS 00
    pair = AtExchange(line="AT", response=["+PAIR_SUCCESS 00:11:22:33:44:55"])
    assert response_value(pair, "PAIR_SUCCESS") == "00:11:22:33:44:55"


def test_response_value_keeps_intentionally_empty_body() -> None:
    """``+R_PSK:`` 是"有冒号、正文为空"，值就是空串，不能回退成行名。

    这是配置页的数据来源：密码留白、还没加入上级网络时模块回的就是空正文。一旦把
    ``R_PSK:`` 当成值存下来，用户点一次"下发"就把上级网络名写成了字面量 ``R_SSID:``。
    """
    psk = AtExchange(line="AT+R_PSK?", kind="query", name="R_PSK", response=["+R_PSK:", "OK"])
    assert response_value(psk, "R_PSK") == ""
    assert response_value(psk) == ""
    ssid = AtExchange(line="AT+R_SSID?", kind="query", name="R_SSID", response=["+R_SSID:"])
    assert response_value(ssid, "R_SSID") == ""


def test_response_value_first_plus_line_wins_even_if_empty() -> None:
    """退回"第一个 + 行"时，空正文也算取到过，不能被后面一行的正文顶掉。"""
    ex = AtExchange(line="AT+R_SSID?", response=["+R_SSID:", "+WNBCFG:mode=ap"])
    assert response_value(ex, "R_SSID") == ""
    assert response_value(ex) == ""


def test_failure_line_recognizes_custom_errors() -> None:
    assert failure_line("sta:ff:ff:ff:ff:ff:ff is not exist")
    assert failure_line("Pairing FAIL") == "Pairing FAIL"
    assert failure_line("+MODE:ap") == ""
    assert failure_line("OK") == ""


# --------------------------------------------------------------------- 多模块定向
class TargetCapTransport:
    """带 supports_target 的通道替身：记录 write 是否收到 target_mac 关键字。"""

    name = "cap485"
    supports_target = True

    def __init__(self) -> None:
        self.calls: list[tuple[bytes, object]] = []

    def is_open(self) -> bool:
        return True

    def write(self, data: bytes, target_mac=None) -> int:
        self.calls.append((bytes(data), target_mac))
        return len(data)


def test_set_target_mac_cleans_input_and_resets_awake(harness: Harness) -> None:
    mac6 = bytes.fromhex("96a13e7d8388")
    h = harness
    assert h.client.set_target_mac("96-a1-3e-7d-83-88") == mac6
    assert h.client.target_mac == mac6
    assert h.client.set_target_mac(b"\x01\x02") == b"", "长度不是 6 字节的 bytes 视为清空"
    assert h.client.set_target_mac("not-a-mac") == b"", "非法文本回空并告警"
    assert h.client.target_mac == b""
    h.client.set_target_mac("96:a1:3e:7d:83:88")
    assert h.client.target_mac == mac6, "冒号形式同样接受"


def test_write_raw_routes_target_only_when_supported(harness: Harness) -> None:
    h = harness
    cap = TargetCapTransport()
    mac6 = h.client.set_target_mac("96-a1-3e-7d-83-88")
    h.client._write_raw(cap, b"AT+RSSI?\r\n")
    assert cap.calls == [(b"AT+RSSI?\r\n", mac6)], "支持定向的通道必须收到 target_mac 关键字"
    plain = SilentTransport()
    h.client._write_raw(plain, b"AT+RSSI?\r\n")
    assert plain.writes == [b"AT+RSSI?\r\n"], "不支持定向的通道保持单参签名"
    h.client.set_target_mac("")
    h.client._write_raw(cap, b"AT+MODE?\r\n")
    assert cap.calls[-1] == (b"AT+MODE?\r\n", None), "清空目标后不再传参"


def test_query_still_works_when_transport_lacks_supports_target(
    harness: Harness,
) -> None:
    """仿真通道不支持定向：即使设了 target_mac，请求通路也照常（走裸 write）。"""
    h = harness
    h.client.set_target_mac("96-a1-3e-7d-83-88")
    ok, value, _raw = h.client.query_value("MODE")
    assert (ok, value) == (True, "ap")
