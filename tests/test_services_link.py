"""链路服务 LinkService 的单元测试（仿真模式为主，不碰真硬件）。

验证的都是"装配正确性"契约：仿真模式下 serial/udp 必须换成假电台且通道名跟着改、
打不开时返回 False 并发告警而不是抛异常、transport/state 只在状态真的变化时
发布、运行时换通道会重建传输层。全部用独立的 EventBus，避免全局总线串场景。
"""

from __future__ import annotations

import pytest

from hmi.core.config import _validate, load_config
from hmi.core.events import EventBus, Events
from hmi.core.models import AlarmEvent, LinkSnapshot, TransportState
from hmi.mock.radio import MockByteTransport
from hmi.services.link import LinkService
from hmi.transport.serial_transport import SerialTransport
from hmi.transport.udp_at import UdpAtTransport


def _cfg(**kw):
    base = {"app.mock": True}
    base.update(kw)
    return load_config(None, **base)


@pytest.fixture()
def bus():
    return EventBus()


@pytest.fixture()
def link(bus):
    svc = LinkService(_cfg(), bus=bus, endpoint="local")
    yield svc
    svc.close()


def _alarms(bus):
    out: list[AlarmEvent] = []
    bus.subscribe(Events.ALARM, lambda _t, p: out.append(p))
    return out


# ------------------------------------------------------------------ 通道装配
def test_real_channels_swapped_to_mock_in_sim(bus):
    svc = LinkService(_cfg(), bus=bus, endpoint="local")
    try:
        assert svc.channel == "mock", "默认 AT 通道已是 udp，仿真下必须落地成 mock"
        assert isinstance(svc.transport, MockByteTransport)
        assert svc.mock_module is not None
    finally:
        svc.close()


def test_unknown_channel_falls_back_to_udp(bus):
    svc = LinkService(_cfg(**{"app.mock": False, "at.channel": "rs485"}), bus=bus)
    try:
        assert svc.channel == "udp"
        assert isinstance(svc.transport, UdpAtTransport)
    finally:
        svc.close()


def test_channel_case_insensitive(bus):
    svc = LinkService(_cfg(**{"app.mock": False, "at.channel": "UDP"}), bus=bus)
    try:
        assert svc.channel == "udp"
        assert isinstance(svc.transport, UdpAtTransport)
    finally:
        svc.close()


def test_build_transport_returns_unopened(bus):
    svc = LinkService(_cfg(**{"app.mock": False}), bus=bus)
    try:
        t = svc.build_transport("udp")
        assert isinstance(t, UdpAtTransport)
        assert t not in (svc.transport,)
        assert not t.is_open()
    finally:
        svc.close()


# ------------------------------------------------------------------ 起停
def test_open_close_mock(link):
    assert link.is_open is False
    assert link.open() is True
    assert link.is_open and link.state is TransportState.OPEN
    link.close()
    assert link.is_open is False


def test_open_failure_alarms_not_raises(bus):
    cfg = _cfg(**{"app.mock": False, "at.channel": "serial", "serial.port": "COM254"})
    svc = LinkService(cfg, bus=bus, endpoint="local")
    try:
        got = _alarms(bus)
        assert svc.open() is False
        assert svc._open_error
        assert any(a.code == "AT_OPEN_FAILED" for a in got)
    finally:
        svc.close()


def test_open_without_transport_alarms(bus):
    svc = LinkService(_cfg(), bus=bus)
    svc._transport = None
    got = _alarms(bus)
    assert svc.open() is False
    assert any(a.code == "AT_NO_TRANSPORT" for a in got)
    svc.close()


def test_udp_open_failure_falls_back_to_serial(bus, monkeypatch):
    cfg = _cfg(
        **{"app.mock": False, "at.channel": "udp", "at.fallback_channel": "serial"}
    )
    svc = LinkService(cfg, bus=bus, endpoint="local")
    monkeypatch.setattr(
        UdpAtTransport,
        "open",
        lambda self: (_ for _ in ()).throw(RuntimeError("广播探测不到模块")),
    )
    monkeypatch.setattr(SerialTransport, "open", lambda self: None)
    try:
        got = _alarms(bus)
        assert svc.open() is True, "显式开启串口回退后，失败时应重试回退通道"
        assert svc.channel == "serial"
        assert isinstance(svc.transport, SerialTransport)
        assert any(a.code == "AT_CHANNEL_FALLBACK" for a in got)
    finally:
        svc.close()


def test_default_udp_failure_does_not_touch_serial(bus, monkeypatch):
    cfg = _cfg(**{"app.mock": False, "at.channel": "udp"})
    calls: list[str] = []
    monkeypatch.setattr(
        UdpAtTransport,
        "open",
        lambda self: (_ for _ in ()).throw(RuntimeError("广播探测不到模块")),
    )
    monkeypatch.setattr(SerialTransport, "open", lambda self: calls.append("serial"))
    svc = LinkService(cfg, bus=bus, endpoint="local")
    try:
        assert svc.open() is False, "串口是纯透传通道，默认绝不回退，免得 AT 被透传到远端"
        assert svc.channel == "udp"
        assert not calls, "默认配置不得占用透传串口"
    finally:
        svc.close()


def test_serial_at_usage_warns_passthrough_pollution():
    cfg = _cfg(**{"app.mock": False, "at.channel": "serial", "serial.port": "COM254"})
    _validate(cfg)
    assert any(
        "透传" in w and "远端" in w for w in cfg.meta.warnings
    ), "走串口发 AT 必须警告会被透传到远端（手册第六章：AT 只走网口）"


def test_fallback_disabled_keeps_error(bus, monkeypatch):
    cfg = _cfg(**{"app.mock": False, "at.channel": "udp", "at.fallback_channel": ""})
    svc = LinkService(cfg, bus=bus, endpoint="local")
    monkeypatch.setattr(
        UdpAtTransport,
        "open",
        lambda self: (_ for _ in ()).throw(RuntimeError("广播探测不到模块")),
    )
    try:
        got = _alarms(bus)
        assert svc.open() is False
        assert svc.channel == "udp"
        assert any(a.code == "AT_OPEN_FAILED" for a in got)
        assert not any(a.code == "AT_CHANNEL_FALLBACK" for a in got)
    finally:
        svc.close()


def test_fallback_to_same_channel_does_not_retry(bus):
    cfg = _cfg(**{"app.mock": False, "at.channel": "serial", "serial.port": "COM254"})
    svc = LinkService(cfg, bus=bus, endpoint="local")
    try:
        got = _alarms(bus)
        assert svc.open() is False
        assert svc.channel == "serial"
        assert any(a.code == "AT_OPEN_FAILED" for a in got)
    finally:
        svc.close()


def test_sim_never_falls_back_to_real_channel(bus, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        SerialTransport,
        "open",
        lambda self: calls.append("serial"),
        raising=True,
    )
    svc = LinkService(
        _cfg(**{"at.channel": "udp", "at.fallback_channel": "serial"}),
        bus=bus,
        endpoint="local",
    )
    try:
        assert svc.open() is True
        assert svc.channel == "mock"
        assert not calls, "仿真模式绝不碰真串口"
    finally:
        svc.close()


# ---------------------------------------------------------- 透传独立通道（分线）
def _split_cfg(**kw):
    base = {"app.mock": False, "at.channel": "udp", "passthrough.channel": "serial"}
    base.update(kw)
    return load_config(None, **base)


def test_split_mode_builds_pt_channel_without_serial_open(bus, monkeypatch):
    monkeypatch.setattr(UdpAtTransport, "open", lambda self: None)
    calls: list[str] = []
    monkeypatch.setattr(SerialTransport, "open", lambda self: calls.append("serial"))
    svc = LinkService(_split_cfg(), bus=bus, endpoint="local")
    try:
        assert svc.passthrough_split is True
        assert svc.pt_demux is not None
        assert svc.open() is True
        assert not calls, "AT 打开不得连带打开透传串口"
        assert svc.pt_is_open is False
        st = svc.stats()
        assert st["passthrough_split"] is True and st["pt_open"] is False
    finally:
        svc.close()


def test_open_passthrough_failure_alarms_but_keeps_at(bus, monkeypatch):
    monkeypatch.setattr(UdpAtTransport, "open", lambda self: None)
    monkeypatch.setattr(UdpAtTransport, "is_open", lambda self: True)

    def boom(self):
        raise OSError("COM254 不存在")

    monkeypatch.setattr(SerialTransport, "open", boom)
    svc = LinkService(_split_cfg(), bus=bus, endpoint="local")
    try:
        got = _alarms(bus)
        assert svc.open() is True
        assert svc.open_passthrough() is False
        assert any(a.code == "PT_OPEN_FAILED" for a in got)
        assert svc.is_open is True, "透传线打不开不能拖累 AT 线"
    finally:
        svc.close()


def test_pt_transport_state_events_are_kind_pt(bus, monkeypatch):
    monkeypatch.setattr(SerialTransport, "open", lambda self: None)
    svc = LinkService(_split_cfg(), bus=bus, endpoint="local")
    try:
        assert svc.open_passthrough() is True
        got: list[dict] = []
        bus.subscribe(Events.TRANSPORT_STATE, lambda _t, p: got.append(p))
        svc._on_pt_transport_state(TransportState.ERROR, "打开失败")
        assert got and got[-1]["kind"] == "pt"
        assert got[-1]["state"] == "error" and got[-1]["detail"] == "打开失败"
        assert got[-1]["channel"] == "serial"
    finally:
        svc.close()


def test_apply_passthrough_serial_rebuilds_pt_line(bus, monkeypatch):
    opens: list[str] = []
    monkeypatch.setattr(SerialTransport, "open", lambda self: opens.append("open"))
    svc = LinkService(_split_cfg(), bus=bus, endpoint="local")
    try:
        assert svc.open_passthrough() is True
        first = svc._pt_transport
        out = svc.apply_passthrough_serial(port="COM7", baudrate=57600)
        assert out["ok"] is True and opens == ["open", "open"]
        assert "COM7" in out["detail"] and "57600" in out["detail"]
        assert svc._pt_transport is not first
        assert svc.cfg.serial.port == "COM7" and svc.cfg.serial.baudrate == 57600
    finally:
        svc.close()


def test_collinear_passthrough_shares_at_line(bus):
    svc = LinkService(_split_cfg(**{"passthrough.channel": "udp"}), bus=bus, endpoint="local")
    try:
        assert svc.passthrough_split is False and svc.pt_demux is None
        assert svc.open_passthrough() is False, "共线时透传状态就是 AT 线状态"
        out = svc.apply_passthrough_serial(port="COM9", baudrate=57600)
        assert out["ok"] is True and "已保存" in out["detail"]
        assert svc.cfg.serial.port == "COM9"
    finally:
        svc.close()


def test_passthrough_same_follows_at_channel(bus):
    svc = LinkService(_split_cfg(**{"passthrough.channel": "same"}), bus=bus, endpoint="local")
    try:
        assert svc.passthrough_channel == "udp"
        assert svc.passthrough_split is False
    finally:
        svc.close()


# ------------------------------------------------------------------ 状态事件
def test_transport_state_published_on_change(link, bus):
    got: list[dict] = []
    bus.subscribe(Events.TRANSPORT_STATE, lambda _t, p: got.append(p))
    link.open()
    assert got, "打开通道必须广播 transport/state"
    assert got[-1]["state"] == "open"
    assert got[-1]["kind"] == "at"
    assert got[-1]["endpoint"] == "local"
    before = len(got)
    link._on_transport_state(TransportState.OPEN)  # 状态未变化不广播
    assert len(got) == before
    link._on_transport_state(TransportState.ERROR, "手动触发")
    assert len(got) == before + 1
    assert got[-1]["detail"] == "手动触发"


def test_state_listener_forwarding_swallows_errors(link):
    good: list[object] = []

    def bad(state, detail=""):
        raise RuntimeError("转发器炸了")

    link.add_state_listener(bad)
    link.add_state_listener(lambda s, d="": good.append(s))
    assert link.open() is True
    assert TransportState.OPEN in good


# ------------------------------------------------------------------ 换通道
def test_set_channel_rebuilds_transport(bus):
    svc = LinkService(_cfg(**{"app.mock": False}), bus=bus, endpoint="local")
    try:
        res = svc.set_channel("serial", reopen=False)
        assert res["ok"] is True
        assert res["channel"] == "serial"
        assert isinstance(svc.transport, SerialTransport)
        assert svc.channel == "serial"
        res2 = svc.set_channel("serial", reopen=False)
        assert res2["detail"] == "通道未变化"
    finally:
        svc.close()


def test_set_channel_keeps_mock_name_in_sim(link):
    res = link.set_channel("serial", reopen=False)
    assert res["channel"] == "mock", "仿真模式下 serial 必须落地成 mock 通道"
    assert link.mock_module is not None
    res2 = link.set_channel("udp", reopen=False)
    assert res2["channel"] == "mock", "仿真模式下 udp 也必须落地成 mock，不能真去广播探测"


# ------------------------------------------------------------------ 探测
def test_discover_requires_udp_channel(link):
    res = link.discover()
    assert res["ok"] is False
    assert "UDP" in res["detail"]


# ------------------------------------------------------------------ 遥测转发
def test_poll_once_publishes_snapshot(link, bus):
    got: list[LinkSnapshot] = []
    bus.subscribe(Events.LINK_SNAPSHOT, lambda _t, p: got.append(p))
    assert link.open() is True
    snap = link.poll_once()
    assert isinstance(snap, LinkSnapshot)
    assert snap.rssi_dbm is not None
    assert got and got[-1] is snap


def test_start_stop_polling(link):
    assert link.open() is True
    assert link.start_polling() is True
    assert link.polling is True
    link.stop_polling()
    assert link.polling is False


def test_set_poll_names_roundtrip(link):
    assert link.set_poll_names(["RSSI"]) == ["RSSI"]


# ------------------------------------------------------------------ 汇总
def test_stats_and_summary_shape(link):
    st = link.stats()
    assert set(st) >= {"endpoint", "channel", "state", "open", "demux", "at", "link", "ticker"}
    assert link.summary()["channel"] == st["channel"]
    assert "polls" in link.summary()


def test_repr(link):
    assert "local" in repr(link) and "channel=mock" in repr(link)


# ------------------------------------------------------------------ 多模块选择
def test_discover_on_non_udp_returns_empty_modules(link):
    res = link.discover()
    assert res["ok"] is False and res["modules"] == []


def test_at_modules_empty_on_non_udp(link):
    assert link.at_modules() == []


def test_select_device_rejected_and_target_cleared_on_mock(link):
    res = link.select_device("96-a1-3e-7d-83-88")
    assert res["ok"] is False and res["mac"] == ""
    assert link.at.target_mac == b"", "非 UDP 通道要把刚设上的目标清干净"
    assert "不支持" in res["detail"]


def test_select_device_empty_mac_is_auto(bus):
    svc = LinkService(_cfg(**{"app.mock": False, "at.channel": "udp"}), bus=bus)
    try:
        res = svc.select_device("")
        assert res["ok"] is True and res["mac"] == "" and res["known"] is True
    finally:
        svc.close()


def test_select_device_udp_known_and_unknown(bus, monkeypatch):
    svc = LinkService(_cfg(**{"app.mock": False, "at.channel": "udp"}), bus=bus)
    try:
        rows = [
            {
                "mac": "96-a1-3e-7d-83-88",
                "online": True,
                "seen_ago_s": 0.2,
                "source_ip": "0.0.0.0",
            }
        ]
        monkeypatch.setattr(svc.transport, "modules", lambda: rows)
        res = svc.select_device("96-A1-3E-7D-83-88")
        assert res["ok"] is True and res["known"] is True
        assert res["mac"] == "96-a1-3e-7d-83-88", "统一回小写连字符形式"
        assert svc.at.target_mac == bytes.fromhex("96a13e7d8388")
        res2 = svc.select_device("96-a1-3e-97-66-80")
        assert res2["ok"] is True and res2["known"] is False
        assert "补定向握手" in res2["detail"]
    finally:
        svc.close()


def test_discover_returns_module_table(bus, monkeypatch):
    svc = LinkService(_cfg(**{"app.mock": False, "at.channel": "udp"}), bus=bus)
    try:
        rows = [
            {
                "mac": "96-a1-3e-7d-83-88",
                "online": True,
                "seen_ago_s": 0.1,
                "source_ip": "0.0.0.0",
            },
            {
                "mac": "96-a1-3e-97-66-80",
                "online": False,
                "seen_ago_s": 12.0,
                "source_ip": "0.0.0.0",
            },
        ]
        monkeypatch.setattr(svc.transport, "discover", lambda timeout_ms=None: 1)
        monkeypatch.setattr(svc.transport, "modules", lambda: rows)
        res = svc.discover()
        assert res["ok"] is True and res["count"] == 1
        assert res["modules"] == rows
        assert "在线 1 个模块（共 2 个）" in res["detail"]
        assert svc.at_modules() == rows
    finally:
        svc.close()
