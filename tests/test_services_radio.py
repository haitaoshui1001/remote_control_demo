"""RadioService 单元测试：用桩替换 link.at，完全不碰真实传输层。

RadioService 只依赖 link 的四个东西：``at`` 客户端、``endpoint`` 字符串、
``poll_once()`` 转发和 ``monitor`` 快照接口。全部用最小桩实现，
这样每一条断言都在验证"编排逻辑"本身（计数、告警、缓存回填、失败降级）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hmi.core.config import load_config
from hmi.core.events import EventBus, Events
from hmi.core.models import AlarmLevel, AtExchange, LinkSnapshot, RadioParams
from hmi.protocol.at_client import AtError
from hmi.services.radio import RadioService

__all__ = []


class FakeAt:
    """AtClient 的最小替身：每个方法可以脚本化成功/失败/抛异常。"""

    def __init__(self) -> None:
        self.default_value = "ap"
        self.ok_default = True
        self.value_map: dict[str, object] = {}
        self.raise_names: set[str] = set()
        self.raise_on: str | None = None
        self.raise_text: str = ""
        self.set_result_ok = True
        self.set_error = ""
        self.apply_results: list[AtExchange] | None = None
        self.action_response = ["OK"]
        self.calls: list[tuple] = []

    def _maybe_raise(self, meth: str, name: str = "") -> None:
        if self.raise_on == meth or name in self.raise_names:
            raise AtError(self.raise_text or f"{name or meth} 超时")

    def query_value(self, name: str, *, timeout_ms: int | None = None):
        self.calls.append(("query_value", name))
        self._maybe_raise("query_value", name)
        if name in self.value_map:
            value = self.value_map[name]
            return True, value, f"+{name}: {value}"
        if not self.ok_default:
            return False, None, f"{name} 无响应"
        return True, self.default_value, f"+{name}: {self.default_value}"

    def query(self, name: str, *, timeout_ms: int | None = None) -> AtExchange:
        self.calls.append(("query", name))
        self._maybe_raise("query", name)
        return AtExchange(
            line=f"AT+{name}=?", name=name, ok=True, response=[f"+{name}: {self.default_value}"]
        )

    def set(self, name: str, value: object, *, timeout_ms: int | None = None) -> AtExchange:
        self.calls.append(("set", name, value))
        self._maybe_raise("set", name)
        return AtExchange(
            line=f"AT+{name}={value}",
            name=name,
            ok=self.set_result_ok,
            error=self.set_error,
            rtt_ms=1.5,
            response=["OK"],
        )

    def action(self, name: str, value: object = "", *, timeout_ms: int | None = None) -> AtExchange:
        self.calls.append(("action", name, value))
        self._maybe_raise("action", name)
        line = f"AT+{name}={value}" if value != "" else f"AT+{name}"
        return AtExchange(
            line=line, name=name, ok=True, response=list(self.action_response), rtt_ms=2.0
        )

    def apply(self, params: RadioParams, *, stop_on_error: bool = False) -> list[AtExchange]:
        self.calls.append(("apply", tuple(sorted(params.touched())), stop_on_error))
        self._maybe_raise("apply")
        if self.apply_results is not None:
            return list(self.apply_results)
        return [AtExchange(line=f"AT+{k}", name=k, ok=True) for k in params.touched()]

    def busy(self) -> bool:
        return False

    def stats(self) -> dict:
        return {"sent": 7}


class FakeLink:
    def __init__(self) -> None:
        self.at = FakeAt()
        self.endpoint = "local"
        self.polls = 0
        self._snap: LinkSnapshot | None = None
        self.monitor = SimpleNamespace(
            snapshot=lambda: self._snap,
            history=lambda: [self._snap] if self._snap is not None else [],
            is_stale=lambda: False,
        )

    def poll_once(self) -> LinkSnapshot | None:
        self.polls += 1
        return self._snap


def _cfg():
    return load_config(None, **{"app.mock": True})


@pytest.fixture()
def bus() -> EventBus:
    return EventBus()


@pytest.fixture()
def link() -> FakeLink:
    return FakeLink()


@pytest.fixture()
def radio(link, bus) -> RadioService:
    return RadioService(link, _cfg(), bus=bus)


def _alarms(bus: EventBus) -> list:
    got: list = []
    bus.subscribe(Events.ALARM, lambda _t, p: got.append(p))
    return got


# ------------------------------------------------------------------ 单个读取
def test_read_success_caches_value(radio, link):
    r = radio.read("MODE")
    assert r["ok"] is True
    assert r["value"] == "ap"
    assert radio.params.mode == "ap"
    assert radio.params.raw["MODE"] == "+MODE: ap"
    assert radio.reads == 1 and radio.failures == 0


def test_read_lowercase_name_normalized(radio, link):
    r = radio.read("mode")
    assert r["ok"] is True
    assert ("query_value", "MODE") in link.at.calls


def test_read_soft_failure_counts(radio, link):
    link.at.ok_default = False
    r = radio.read("MODE")
    assert r["ok"] is False and r["detail"] == "MODE 无响应"
    assert radio.failures == 1


def test_read_aterror_becomes_dict(radio, link):
    link.at.raise_names = {"MODE"}
    r = radio.read("MODE")
    assert r["ok"] is False and "超时" in r["detail"]
    assert radio.failures == 1


# ------------------------------------------------------------------ 批量读取
def test_read_many_counts_success_and_fail(radio, link):
    link.at.value_map = {"SSID": "TESTAP"}
    link.at.raise_names = {"SSID"}
    r = radio.read_many(["MODE", "SSID"])
    assert r["ok"] is True
    assert r["count"] == 1 and r["total"] == 2
    assert r["detail"] == "成功 1/2"
    assert r["values"]["MODE"]["ok"] is True
    assert r["values"]["SSID"]["ok"] is False


def test_read_many_all_failed_flips_ok(radio, link):
    link.at.ok_default = False
    r = radio.read_many(["MODE", "SSID"])
    assert r["ok"] is False and r["count"] == 0


def test_read_many_default_targets_writable(radio, link):
    link.at.ok_default = False
    r = radio.read_many()
    assert r["total"] > 0
    names = set(r["values"])
    assert "MODE" in names and "SSID" in names


def test_read_many_short_circuits_when_channel_down(radio, link):
    """通道根本发不出去时，第一条失败就短路，别把 29 条重复失败刷进日志。"""
    link.at.raise_on = "query_value"
    link.at.raise_text = "AT 通道未打开（serial）"
    r = radio.read_many(["MODE", "SSID", "TXPOWER"])
    assert r["ok"] is False and r["count"] == 0 and r["total"] == 3
    assert r["values"]["MODE"]["detail"] == "AT 通道未打开（serial）"
    assert r["values"]["SSID"]["detail"].startswith("通道不可用，已跳过其余读取：")
    assert r["values"]["TXPOWER"]["detail"] == r["values"]["SSID"]["detail"]
    assert [c for c in link.at.calls if c[0] == "query_value"] == [("query_value", "MODE")]


def test_read_many_keeps_going_on_module_errors(radio, link):
    """模块回了 ERROR 属单参数失败，后面的参数照样该读。"""
    link.at.raise_names = {"MODE"}
    link.at.ok_default = False
    r = radio.read_many(["MODE", "SSID"])
    assert r["values"]["SSID"]["detail"] == "SSID 无响应"
    assert [c[1] for c in link.at.calls if c[0] == "query_value"] == ["MODE", "SSID"]


def test_read_line_returns_exchange(radio, link):
    r = radio.read_line("MODE")
    assert r["ok"] is True
    assert r["line"] == "AT+MODE=?"
    assert "MODE: ap" in r["text"]


# ------------------------------------------------------------------ 写入
def test_write_success_backfills(radio, link):
    r = radio.write("MODE", "sta")
    assert r["ok"] is True and r["rtt_ms"] == 1.5
    assert radio.params.mode == "sta"
    assert "已下发" in r["detail"]
    assert radio.writes == 1


def test_write_unknown_param_skips_channel(radio, link):
    r = radio.write("NOTHING", "x")
    assert r["ok"] is False and radio.failures == 1
    assert not [c for c in link.at.calls if c[0] == "set"]


def test_write_aterror_raises_alarm(radio, bus, link):
    got = _alarms(bus)
    link.at.raise_on = "set"
    r = radio.write("MODE", "ap")
    assert r["ok"] is False
    assert any(a.code == "AT_WRITE_FAILED" for a in got)


def test_write_module_rejects(radio, link):
    link.at.set_result_ok = False
    link.at.set_error = "模块拒绝"
    r = radio.write("MODE", "ap")
    assert r["ok"] is False and r["detail"] == "模块拒绝"
    assert radio.failures == 1


# ------------------------------------------------------------------ 预览与批量下发
def test_plan_translates_touched_to_at_lines(radio):
    p = RadioParams(mode="ap", ssid="TEST")
    r = radio.plan(p)
    assert r["ok"] is True
    assert r["detail"] == "2 项待下发"
    assert any("MODE=ap" in ln for ln in r["lines"])
    assert any("SSID=TEST" in ln for ln in r["lines"])


def test_plan_invalid_value_returns_error(radio):
    r = radio.plan(RadioParams(mode="bogus"))
    assert r["ok"] is False
    assert r["lines"] == []
    assert radio.failures == 1


def test_apply_without_changes_is_noop(radio, link):
    r = radio.apply(RadioParams())
    assert r == {"ok": True, "detail": "没有任何改动", "results": [], "count": 0}
    assert not [c for c in link.at.calls if c[0] == "apply"]


def test_apply_partial_failure_alarms(radio, bus, link):
    got = _alarms(bus)
    link.at.apply_results = [
        AtExchange(name="MODE", line="AT+MODE=ap", ok=True),
        AtExchange(name="SSID", line="AT+SSID=T", ok=False, error="拒绝"),
    ]
    r = radio.apply(RadioParams(mode="ap", ssid="T"))
    assert r["ok"] is False
    assert r["detail"] == "1/2 项成功"
    assert r["failed"] == ["SSID"]
    assert radio.writes == 1 and radio.failures == 1
    assert any(a.code == "PARAM_PARTIAL" for a in got)


def test_apply_aterror_alarms(radio, bus, link):
    got = _alarms(bus)
    link.at.raise_on = "apply"
    r = radio.apply(RadioParams(mode="ap"))
    assert r["ok"] is False and r["count"] == 0
    assert any(a.code == "AT_APPLY_FAILED" for a in got)


# ------------------------------------------------------------------ 动作指令
def test_action_success(radio, link):
    r = radio.action("PAIR")
    assert r["ok"] is True and r["detail"] == "OK"
    assert radio.writes == 1


def test_action_with_value_backfills_cache(radio, link):
    r = radio.action("MODE", "group")
    assert r["ok"] is True
    assert radio.params.mode == "group"


def test_action_aterror_is_crit_alarm(radio, bus, link):
    got = _alarms(bus)
    link.at.raise_on = "action"
    r = radio.action("SCAN_AP")
    assert r["ok"] is False and r["response"] == []
    alarm = next(a for a in got if a.code == "AT_ACTION_FAILED")
    assert alarm.level == AlarmLevel.CRIT.value


# ------------------------------------------------------------------ 事件契约
def test_service_never_publishes_radio_params(radio, bus, link):
    got: list = []
    bus.subscribe(Events.RADIO_PARAMS, lambda _t, p: got.append(p))
    radio.read("MODE")
    radio.write("MODE", "sta")
    radio.apply(RadioParams(ssid="T"))
    link.at.apply_results = None
    assert got == [], "RadioService 刻意不发布 radio/params，周期数据以快照为准"


def test_load_defaults_clears_cache(radio, link):
    radio.read("MODE")
    r = radio.load_defaults()
    assert r["ok"] is True
    assert radio.params.touched() == {}
    assert radio.last_read_ts == 0.0


# ------------------------------------------------------------------ 目录与遥测
def test_catalog_rows_have_full_keys(radio):
    rows = radio.catalog()
    assert rows, "注册表必须有参数"
    expected = {
        "name", "at", "title", "group", "rw", "kind",
        "bound", "persist", "desc", "value", "options", "unit",
    }
    assert expected <= set(rows[0])
    assert any(rn["name"] == "MODE" for rn in rows)
    names_rw = [rn["name"] for rn in radio.catalog(writable_only=True)]
    assert "MODE" in names_rw


def test_spec_info_and_missing(radio):
    info = radio.spec_info("MODE")
    assert info is not None and info["writable"] is True
    assert radio.spec_info("NOTHING") is None


def test_poll_and_snapshot_forwarding(radio, link):
    assert radio.poll_once() is None
    assert link.polls == 1
    assert radio.snapshot() is None
    assert radio.link_summary() == {"available": False, "detail": "还没有成功轮询"}
    link._snap = LinkSnapshot(endpoint="local", connected=True, rssi_dbm=-70.5)
    s = radio.link_summary()
    assert s["available"] is True
    assert s["rssi_dbm"] == -70.5
    assert s["stale"] is False
    assert radio.history() == [link._snap]


def test_summary_keys_and_repr(radio, link):
    st = radio.summary()
    assert {
        "endpoint", "reads", "writes", "failures",
        "touched", "last_read_ts", "snapshot", "at",
    } == set(st)
    assert st["at"] == {"sent": 7}
    assert "RadioService" in repr(radio)


# ------------------------------------------------------------------ 设备选择与 per-MAC 缓存
def test_select_device_unsupported_link(radio):
    res = radio.select_device("96-a1-3e-7d-83-88")
    assert res["ok"] is False and "不支持" in res["detail"]


def test_per_device_param_caches(link, bus):
    picks: list[str] = []

    def picker(mac: str) -> dict:
        picks.append(mac)
        return {"ok": True, "mac": mac.lower(), "known": True, "detail": ""}

    link.select_device = picker
    svc = RadioService(link, _cfg(), bus=bus)
    assert svc.read("MODE")["ok"] is True
    assert svc.params.mode == "ap"
    assert svc.select_device("96-A1-3E-7D-83-88")["ok"] is True
    assert svc.device_key == "96-a1-3e-7d-83-88"
    assert svc.params.mode is None, "切到新设备应是空白缓存"
    link.at.value_map = {"MODE": "sta"}
    assert svc.read("MODE")["ok"] is True
    assert svc.params.mode == "sta"
    assert svc.select_device("")["ok"] is True
    assert svc.params.mode == "ap", "切回原设备恢复各自缓存"
    assert picks == ["96-A1-3E-7D-83-88", ""]


def test_select_device_failure_keeps_current_device(link, bus):
    link.select_device = lambda mac: {"ok": False, "mac": "", "detail": "通道不支持"}
    svc = RadioService(link, _cfg(), bus=bus)
    assert svc.read("MODE")["ok"] is True
    res = svc.select_device("96-a1-3e-7d-83-88")
    assert res["ok"] is False
    assert svc.device_key == "" and svc.params.mode == "ap", "失败切换不能动当前状态"


def test_load_defaults_overwrites_current_device_cache(link, bus):
    link.select_device = lambda mac: {
        "ok": True, "mac": mac.lower(), "known": True, "detail": ""
    }
    svc = RadioService(link, _cfg(), bus=bus)
    assert svc.read("MODE")["ok"] is True
    svc.load_defaults()
    assert svc.params.mode is None
    assert svc._caches[""] is svc.params, "出厂默认要写回当前设备的缓存槽"
