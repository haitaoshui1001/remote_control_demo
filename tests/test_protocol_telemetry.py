"""信道遥测测试：解析函数用纯数据断言，轮询器用仿真链路端到端跑一次。

阈值判定是 UI 告警的唯一来源，所以这里把"多少 dBm 算差"逐条钉住——以后改
LinkCfg 默认值会立刻在这里暴露，而不是等现场看到一条不合常理的红字。
"""

from __future__ import annotations

from typing import Any

from hmi.core.config import AtCfg, LinkCfg
from hmi.core.events import EventBus, Events
from hmi.core.models import LinkSnapshot, StaInfo
from hmi.mock.radio import MockByteTransport, MockRadioModule
from hmi.protocol.at_client import AtClient
from hmi.protocol.channel import ChannelDemux
from hmi.protocol.telemetry import (
    LinkMonitor,
    build_snapshot,
    evaluate,
    loss_percent,
    params_view,
    parse_sta_rows,
)

_STA_CSV = "1,00:11:22:33:44:55,-78.0,-22.0,7,7,15234,18990,12"

#: 端到端用例的轮询清单（AP 侧完整一轮，共 9 项）
_POLL_NAMES = [
    "MODE",
    "CONN_STATE",
    "RSSI",
    "EVM",
    "BSS_BW",
    "TXPOWER",
    "WNBCFG",
    "CHAN_LIST",
    "STA_INFO",
]


def test_parse_sta_rows_accepts_csv_and_kv_shapes():
    rows = parse_sta_rows(_STA_CSV)
    assert len(rows) == 1
    row = rows[0]
    assert row.index == 1
    assert row.mac == "00:11:22:33:44:55"
    assert row.rssi_dbm == -78.0 and row.evm_db == -22.0
    assert row.tx_mcs == 7 and row.rx_mcs == 7
    assert row.tx_packets == 15234 and row.rx_packets == 18990 and row.lost_packets == 12

    # 分号分行：序号按行序补齐，供 UI 表格显示
    two = parse_sta_rows("+STA_INFO:" + _STA_CSV + ";2,00:11:22:33:44:66,-80.0,-25.0,5,5,10,9,1")
    assert [r.index for r in two] == [1, 2]
    assert two[1].mac == "00:11:22:33:44:66"

    kv = parse_sta_rows("id=2,rssi=-80.5,evm=-24,tx=100,rx=90,lost=3")
    assert kv[0].index == 2 and kv[0].rssi_dbm == -80.5 and kv[0].lost_packets == 3

    # 认不出来的行只丢数据，不抛异常
    assert parse_sta_rows("没有数字也没有逗号的一行") == []
    assert parse_sta_rows("") == []


def test_loss_percent_denominator_and_unknown():
    # lost 是未确认帧，分母取 tx + lost
    assert loss_percent(1000, 0) == 0.0
    assert loss_percent(15234, 12) == 0.08
    assert loss_percent("15234", "12") == 0.08
    assert loss_percent(None, None) is None
    assert loss_percent(0, 0) is None
    assert loss_percent(100, None) == 0.0


def _results(**overrides):
    """按 ``query_value`` 的真实返回形态造数据：(ok, 解析值, 响应正文)。

    正文这里必须是**去掉 ``+NAME:`` 前缀**的那段——``AtClient.response_value`` 已经剥好了，
    ``WNBCFG``/``STA_INFO`` 这类多字段行正是拿正文解析的，前缀留在正文里会解析不到字段。
    """
    base = {
        "MODE": (True, "ap", "ap"),
        "BSS_BW": (True, 8, "8"),
        "TXPOWER": (True, 20, "20"),
        "FREQ_RANGE": (True, [8680, 9280], "8680,9280"),
        "CHAN_LIST": (True, [8680, 8700, 8720], "8680,8700,8720"),
        "RSSI": (True, -72.0, "-72.0"),
        "EVM": (True, -28.0, "-28.0"),
        "CONN_STATE": (True, True, "CONNECTED"),
        "WNBCFG": (True, "mode=group,bw=4,chan=8700,8720", "mode=group,bw=4,chan=8700,8720"),
        "STA_INFO": (True, _STA_CSV, _STA_CSV),
    }
    base.update(overrides)
    return base


def test_build_snapshot_maps_fields_and_prefers_wnbcfg_channel():
    snap = build_snapshot(_results(), endpoint="local", stats={"sent": 10})
    assert snap.endpoint == "local"
    assert snap.role == "ap"  # 已有 MODE 时不被 WNBCFG 的 mode=group 覆盖
    assert snap.bandwidth_mhz == 4  # WNBCFG 是当前生效配置，读数优先于参数表
    assert snap.freq_mhz == 870.0  # 线上传 MHz×10
    assert snap.chan_list == [8680, 8700, 8720]
    assert snap.freq_range == [8680, 9280]
    assert snap.txpower_dbm == 20.0
    assert snap.connected is True
    assert snap.rssi_dbm == -72.0 and snap.evm_db == -28.0
    assert snap.sta_count == 1 and snap.sta_list[0].mac == "00:11:22:33:44:55"
    assert snap.errors == [] and snap.degraded is False
    assert snap.stats["sent"] == 10


def test_build_snapshot_records_failures_without_crashing():
    snap = build_snapshot(
        _results(SSID=(False, None, "超时未响应"), RSSI=(False, None, "ERROR")),
        endpoint="remote",
    )
    assert len(snap.errors) == 2
    assert any(err.startswith("SSID:") for err in snap.errors)
    # CONN_STATE 没查到保持 None（未知），而不是当作断开
    assert snap.connected is True
    assert snap.sta_count == 1


def test_build_snapshot_falls_back_to_sta_rows_for_rf():
    """个别固件不提供整机 RSSI，用第一条 STA 的链路值兜底。"""
    snap = build_snapshot(_results(RSSI=(True, None, ""), EVM=(True, None, "")))
    assert snap.rssi_dbm == -78.0
    assert snap.evm_db == -22.0


def _snap(**kw) -> LinkSnapshot:
    base = {
        "endpoint": "local",
        "role": "ap",
        "connected": True,
        "rssi_dbm": -72.0,
        "evm_db": -28.0,
        "bandwidth_mhz": 8,
        "sta_list": [StaInfo(index=1, mac="00:11:22:33:44:55", tx_packets=15234, lost_packets=12)],
    }
    base.update(kw)
    return LinkSnapshot(**base)


def test_evaluate_thresholds():
    cfg = LinkCfg()
    assert evaluate(_snap(), cfg) == []

    crit = evaluate(_snap(rssi_dbm=-93.0), cfg)
    assert [a.code for a in crit] == ["RSSI_CRIT"]
    assert crit[0].level == "crit"

    low = evaluate(_snap(rssi_dbm=-86.0), cfg)
    assert [a.code for a in low] == ["RSSI_LOW"]

    evm = evaluate(_snap(evm_db=-15.0), cfg)
    assert [a.code for a in evm] == ["EVM_HIGH"]

    down = evaluate(_snap(connected=False), cfg)
    assert [a.code for a in down] == ["LINK_DOWN"]

    # 未知状态（None）不报"链路断开"，避免刚上电时误报
    assert not any(a.code == "LINK_DOWN" for a in evaluate(_snap(connected=None), cfg))

    leaky = StaInfo(index=1, mac="aa:bb:cc:dd:ee:ff", tx_packets=1000, lost_packets=60)
    loss = evaluate(_snap(sta_list=[leaky]), cfg)
    assert [a.code for a in loss] == ["PACKET_LOSS"]

    partial = evaluate(_snap(rssi_dbm=None, errors=["SSID: 超时未响应"]), cfg)
    assert [a.code for a in partial] == ["POLL_PARTIAL"]
    assert partial[0].level == "info"


def test_evaluate_sets_degraded_flag_inplace():
    good = _snap()
    assert evaluate(good, LinkCfg()) == [] and good.degraded is False
    bad = _snap(rssi_dbm=-95.0, evm_db=-10.0)
    assert len(evaluate(bad, LinkCfg())) == 2 and bad.degraded is True


def test_params_view_is_a_flat_form_dict():
    view = params_view(_snap(ssid="hgic_ah_test", tx_mcs=10))
    assert view["ssid"] == "hgic_ah_test" and view["tx_mcs"] == 10
    assert {"role", "connected", "bandwidth_mhz", "freq_range", "chan_list"} <= set(view)


def test_link_monitor_polls_mock_radio_end_to_end(monkeypatch):
    """从仿真 485 线一路读到 LinkSnapshot：轮询清单、裸命令、多行响应都要成立。"""
    mod = MockRadioModule(name="local", seed=7)
    monkeypatch.setattr(mod, "tick", lambda: None)  # 冻结随机游走，断言才有确定值
    transport = MockByteTransport(mod, latency_ms=2, telemetry_interval_s=3600.0)
    transport.open()
    demux = ChannelDemux("tel-test", idle_gap_ms=20.0, read_timeout_ms=5)
    demux.attach(transport)
    at = AtClient(
        AtCfg(timeout_ms=1200, retries=0, pre_silence_ms=0),
        demux,
        endpoint="local",
        value_grace_ms=60.0,
    )
    mon = LinkMonitor(at, cfg=LinkCfg(poll_interval_s=0.5, auto_poll=False))
    mon.set_names(_POLL_NAMES)
    try:
        names = mon.default_names()
        assert "WNBCFG" in names and "STA_INFO" in names  # AP 侧才追加这两项
        assert names.count("RSSI") == 1
        assert mon.names[0] == "MODE"

        snap = mon.poll_once(publish=False)
        assert snap is not None
        assert snap.errors == []
        assert snap.role == "ap"
        assert snap.bandwidth_mhz == 8
        assert snap.freq_mhz == 868.0
        assert snap.txpower_dbm == 20.0
        assert snap.connected is True
        assert snap.rssi_dbm == -72.0 and snap.evm_db == -28.0
        assert snap.sta_count == 1
        assert snap.sta_list[0].mac == "00:11:22:33:44:55"
        assert snap.stats["endpoint"] == "local"

        assert mon.snapshot() is snap
        assert mon.polls == 1 and mon.skips == 0
        assert mon.is_stale() is False
        assert mon.check_stale() is False

        # 第二轮：静态参数不再重读，只读 volatile 项，但快照仍保留首轮的值
        rounds: list[list[str]] = []
        orig_read_many = at.read_many

        def spy(names):
            rounds.append(list(names))
            return orig_read_many(names)

        at.read_many = spy
        snap2 = mon.poll_once(publish=False)
        assert rounds[0] == [
            "CONN_STATE",
            "RSSI",
            "EVM",
            "WNBCFG",
            "STA_INFO",
        ]
        assert snap2.role == "ap" and snap2.txpower_dbm == 20.0
        assert snap2.freq_mhz == 868.0  # MODE/TXPOWER/CHAN_LIST 靠累积结果留存

        # AT 通道被占用时跳过本轮，返回上一快照并且计数
        at.busy = lambda: True
        assert mon.poll_once(publish=False) is snap2
        assert mon.skips == 1 and mon.polls == 2
        assert mon.stats()["names"] == len(_POLL_NAMES)
        assert mon.start() is False  # auto_poll 关闭时不起线程
    finally:
        demux.detach()
        transport.close()


def test_link_monitor_history_and_stale_alarm(monkeypatch):
    mod = MockRadioModule(name="local", seed=1)
    monkeypatch.setattr(mod, "tick", lambda: None)
    transport = MockByteTransport(mod, latency_ms=2, telemetry_interval_s=3600.0)
    transport.open()
    demux = ChannelDemux("tel-stale", idle_gap_ms=20.0, read_timeout_ms=5)
    demux.attach(transport)
    bus = EventBus()
    at = AtClient(AtCfg(timeout_ms=800, retries=0, pre_silence_ms=0), demux, bus=bus)
    mon = LinkMonitor(at, bus=bus, cfg=LinkCfg(stale_after_s=1.0, auto_poll=False))
    mon.set_names(["MODE"])
    try:
        alarms = []
        bus.subscribe(Events.ALARM, lambda topic, payload: alarms.append(payload))
        assert mon.poll_once() is not None
        assert len(mon.history()) == 1 and mon.polls == 1
        assert alarms == []

        monkeypatch.setattr(mon, "age_s", lambda: 5.0)
        assert mon.check_stale() is True
        assert [a.code for a in alarms] == ["TELEMETRY_STALE"]
        # 刚变过期只报一次
        assert mon.check_stale() is True and len(alarms) == 1
    finally:
        demux.detach()
        transport.close()


# ------------------------------------------------------------------ 首轮/易变轮
class _RecordingAt:
    """LinkMonitor 只用到 busy/read_many/stats/endpoint/cfg，用替身钉死每轮读什么。"""

    endpoint = "local"

    def __init__(self, results: dict[str, tuple[bool, Any, str]] | None = None) -> None:
        self.cfg = AtCfg()
        self.results = results or {}
        self.calls: list[list[str]] = []

    def busy(self) -> bool:
        return False

    def stats(self) -> dict[str, Any]:
        return {}

    def read_many(self, names: list[str]) -> dict[str, tuple[bool, Any, str]]:
        self.calls.append(list(names))
        return {n: self.results.get(n, (True, "", "")) for n in names}


def test_monitor_reads_full_first_then_volatile_only():
    at = _RecordingAt()
    mon = LinkMonitor(at, cfg=LinkCfg(auto_poll=False))
    mon.set_names(["MODE", "CONN_STATE", "RSSI", "TXPOWER", "WNBCFG"])
    assert mon.poll_once(publish=False) is not None
    assert at.calls[0] == ["MODE", "CONN_STATE", "RSSI", "TXPOWER", "WNBCFG"]
    mon.poll_once(publish=False)
    assert at.calls[1] == ["CONN_STATE", "RSSI", "WNBCFG"]
    assert mon.stats()["full_round_done"] is True


def test_failed_static_param_is_re_read_until_ok():
    """首轮失败的静态项每轮补读，读成功后不再出现——避免一次失败永久挂红。"""
    at = _RecordingAt(results={"RSSI": (True, -70.0, "-70.0"), "TXPOWER": (False, None, "ERROR")})
    mon = LinkMonitor(at, cfg=LinkCfg(auto_poll=False))
    mon.set_names(["RSSI", "TXPOWER", "BSS_BW"])
    mon.poll_once(publish=False)
    assert at.calls[0] == ["RSSI", "TXPOWER", "BSS_BW"]
    # TXPOWER 失败待补读；BSS_BW 首轮已成功，不再重读
    mon.poll_once(publish=False)
    assert at.calls[1] == ["RSSI", "TXPOWER"]
    at.results["TXPOWER"] = (True, 20, "20")
    snap3 = mon.poll_once(publish=False)
    assert snap3.txpower_dbm == 20.0 and snap3.errors == []
    mon.poll_once(publish=False)
    assert at.calls[3] == ["RSSI"]


def test_volatile_after_first_can_be_disabled():
    at = _RecordingAt()
    mon = LinkMonitor(at, cfg=LinkCfg(auto_poll=False, volatile_after_first=False))
    mon.set_names(["RSSI", "MODE"])
    mon.poll_once(publish=False)
    mon.poll_once(publish=False)
    assert at.calls[0] == at.calls[1] == ["RSSI", "MODE"]


def test_all_static_list_falls_back_to_full_round():
    """清单里没有任何易变项时退回全量，不然第二轮等于什么都不读。"""
    at = _RecordingAt()
    mon = LinkMonitor(at, cfg=LinkCfg(auto_poll=False))
    mon.set_names(["MODE", "SSID"])
    mon.poll_once(publish=False)
    mon.poll_once(publish=False)
    assert at.calls[1] == ["MODE", "SSID"]


def test_set_names_resets_accumulation():
    """换清单必须重回首轮语义：旧清单的参数不能残留在新快照里。"""
    at = _RecordingAt(
        results={"MODE": (True, "ap", "ap"), "RSSI": (True, -70.0, "-70.0")}
    )
    mon = LinkMonitor(at, cfg=LinkCfg(auto_poll=False))
    mon.set_names(["MODE", "RSSI"])
    mon.poll_once(publish=False)
    mon.set_names(["RSSI"])
    snap = mon.poll_once(publish=False)
    assert at.calls[1] == ["RSSI"]
    assert snap.role is None  # MODE 被移出清单，旧值不得残留
