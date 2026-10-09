"""PassthroughService 单元测试：只替掉传输线（FakeDemux/FakeAt），引擎与模板库都跑真货。

这一层的存在意义是"失败也要有结论"：引擎只在成功路径发布 ``command/result``，
超时/被拒/写失败都由服务补发。测试重点就放在补发规则上——
引擎发过的绝不重复发，引擎没发的必须补一条。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from hmi.core.config import load_config
from hmi.core.events import EventBus, Events
from hmi.core.models import AlarmEvent, CommandResult, Direction
from hmi.protocol.telemetry_frames import encode_wheeltec_frame
from hmi.services.passthrough import PassthroughService

__all__ = []


class FakeDemux:
    """通道替身：记录写出的帧，可注入 OSError，可让写帧立刻回环成应答。"""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.sinks: list[Any] = []
        self.fail: Exception | None = None
        self.echo_reply = False

    def subscribe_passthrough(self, sink: Any) -> None:
        self.sinks.append(sink)

    def unsubscribe(self, sink: Any) -> None:
        if sink in self.sinks:
            self.sinks.remove(sink)

    def write(self, frame: bytes) -> None:
        if self.fail is not None:
            raise self.fail
        self.written.append(frame)
        if self.echo_reply:
            for sink in list(self.sinks):
                sink(bytes(frame), time.time())


class FakeAt:
    def __init__(self) -> None:
        self.busy_flag = False

    def busy(self) -> bool:
        return self.busy_flag


class FakeLink:
    def __init__(self) -> None:
        self.endpoint = "local"
        self.demux = FakeDemux()
        self.at = FakeAt()
        # 分线模式相关面：默认共线，与旧用例行为完全一致
        self.passthrough_split = False
        self.pt_demux: FakeDemux | None = None
        self.pt_open_calls = 0
        self.pt_close_calls = 0

    def open_passthrough(self) -> bool:
        self.pt_open_calls += 1
        return True

    def close_passthrough(self) -> None:
        self.pt_close_calls += 1


_TEMPLATES = """\
- id: t_open
  name: 开闸
  payload: "01 06 00 10 00 01"
  mode: hex
  crc16_append: true
  expect_reply: false
  group: 演示
- id: t_set
  name: 写寄存器
  payload: "01 06 {reg} 00 01"
  mode: hex
  crc16_append: true
  expect_reply: false
  group: 演示
"""


def _cfg():
    return load_config(None, **{"app.mock": True})


@pytest.fixture()
def bus() -> EventBus:
    return EventBus()


@pytest.fixture()
def link() -> FakeLink:
    return FakeLink()


@pytest.fixture()
def svc(bus, link, tmp_path: Path):
    tpl_file = tmp_path / "templates.yaml"
    tpl_file.write_text(_TEMPLATES, encoding="utf-8")
    s = PassthroughService(link, _cfg(), bus=bus, templates_path=tpl_file)
    assert s.start() is True
    yield s
    s.stop()


def _results(bus: EventBus) -> list[CommandResult]:
    got: list[CommandResult] = []
    bus.subscribe(Events.CMD_RESULT, lambda _t, p: got.append(p))
    return got


def _alarms(bus: EventBus) -> list[AlarmEvent]:
    got: list[AlarmEvent] = []
    bus.subscribe(Events.ALARM, lambda _t, p: got.append(p))
    return got


# ------------------------------------------------------------------ 只发不等
def test_send_success_returns_hex_and_counts(svc, link):
    got = _results(bus=svc.bus)
    r = svc.send("01 03 00 00", mode="hex", note="probe")
    assert r["ok"] is True
    assert "已发送" in r["detail"]
    assert r["hex"] and r["ascii"] is not None
    assert svc.sent == 1 and svc.failed == 0
    assert link.demux.written, "帧必须真正写到线上"
    assert got == [], "只发不等的成功没有结论事件"


def test_send_validation_error_backfills_result(svc, link):
    got = _results(svc.bus)
    link.at.busy_flag = True  # 半双工忙 → RuntimeError，也必须收口
    r = svc.send("01 03", mode="hex")
    assert r == {"ok": False, "detail": r["detail"], "hex": "01 03"}
    assert "忙" in r["detail"], "拒发也要显示原始字节，便于排查线上内容"
    assert svc.failed == 1
    assert len(got) == 1 and got[0].ok is False


def test_send_empty_payload_is_value_error(svc):
    got = _results(svc.bus)
    r = svc.send("", mode="ascii", suffix="none")
    assert r["ok"] is False and r["hex"] == ""
    assert len(got) == 1
    assert svc.failed == 1


def test_send_oserror_alarms_and_backfills(svc, link):
    got = _results(svc.bus)
    alarms = _alarms(svc.bus)
    link.demux.fail = OSError("串口掉了")
    r = svc.send("41", mode="ascii")
    assert r["ok"] is False
    assert r["detail"].startswith("写入失败：")
    assert any(a.code == "PT_WRITE_FAILED" for a in alarms)
    assert len(got) == 1 and got[0].ok is False
    assert svc.failed == 1


# ------------------------------------------------------------------ 分线模式
def _split_link() -> FakeLink:
    link = FakeLink()
    link.passthrough_split = True
    link.pt_demux = FakeDemux()
    return link


def _make_svc(bus: EventBus, link: FakeLink, tmp_path: Path) -> PassthroughService:
    tpl_file = tmp_path / "templates.yaml"
    tpl_file.write_text(_TEMPLATES, encoding="utf-8")
    cfg = load_config(None, **{"app.mock": False})
    return PassthroughService(link, cfg, bus=bus, templates_path=tpl_file)


def test_collinear_start_never_touches_pt_line(svc, link):
    """共线时透传状态就是 AT 线状态，服务不得去开独立串口。"""
    assert link.pt_open_calls == 0 and link.pt_close_calls == 0


def test_split_start_opens_pt_line_and_stop_closes(bus, tmp_path):
    link = _split_link()
    s = _make_svc(bus, link, tmp_path)
    assert s.split is True
    assert s.start() is True
    assert link.pt_open_calls == 1
    s.stop()
    assert link.pt_close_calls == 1


def test_split_send_bypasses_busy_guard_and_uses_pt_channel(bus, tmp_path):
    """分线后 AT 在网口上忙，485 独立线照发不误——这正是用户报的冲突根因。"""
    link = _split_link()
    s = _make_svc(bus, link, tmp_path)
    s.start()
    try:
        link.at.busy_flag = True
        r = s.send("01 03", mode="hex")
        assert r["ok"] is True, r["detail"]
        assert link.pt_demux.written == [bytes([0x01, 0x03])]
        assert link.demux.written == [], "分线模式帧不能写到 AT 共用的分流器上"
    finally:
        s.stop()


def test_split_write_failure_still_reports_frame(bus, tmp_path):
    link = _split_link()
    link.pt_demux.fail = OSError("串口未打开（COM254）")
    s = _make_svc(bus, link, tmp_path)
    s.start()
    try:
        alarms = _alarms(s.bus)
        r = s.send("01 03", mode="hex")
        assert r["ok"] is False
        assert r["hex"] == "01 03"
        assert any(a.code == "PT_WRITE_FAILED" for a in alarms)
    finally:
        s.stop()


# ------------------------------------------------------------------ 发并等
def test_request_success_publishes_once(svc):
    svc.link.demux.echo_reply = True
    got = _results(svc.bus)
    res = svc.request("01 03 00 00 00 01", mode="hex", crc16_append=True, timeout_ms=200)
    assert res.ok is True and res.matched is True
    assert svc.sent == 1
    assert len(got) == 1, "引擎发过的结论，服务不得重复发"


def test_request_timeout_backfills_result(svc):
    got = _results(svc.bus)
    res = svc.request("01 03 00 00 00 01", mode="hex", timeout_ms=30)
    assert res.ok is False
    assert "超时" in res.error
    assert svc.failed == 1
    assert len(got) == 1 and got[0] is res


def test_request_oserror_alarm_and_single_result(svc, link):
    got = _results(svc.bus)
    alarms = _alarms(svc.bus)
    link.demux.fail = OSError("写不进")
    res = svc.request("41", mode="ascii", name="raw")
    assert res.ok is False and res.error.startswith("写入失败：")
    assert any(a.code == "PT_WRITE_FAILED" for a in alarms)
    assert svc.failed == 1 and svc.sent == 0
    assert len(got) == 1, "失败计数与补发都只能记一次"


# ------------------------------------------------------------------ 模板链路
def test_run_template_no_reply_publishes_conclusion(svc):
    got = _results(svc.bus)
    res = svc.run_template("t_open")
    assert res.ok is True and res.matched is False
    assert svc.sent == 1
    assert len(got) == 1, "不等应答的模板引擎不发事件，服务必须补"


def test_run_template_missing_id_fails_cleanly(svc):
    got = _results(svc.bus)
    res = svc.run_template("不存在的模板")
    assert res.ok is False and "模板不存在" in res.error
    assert svc.failed == 1
    assert len(got) == 1


def test_run_many_breaks_on_first_failure(svc):
    out = svc.run_many(
        [("t_open", None), ("不存在", None), ("t_open", None)],
        gap_s=0.0,
    )
    assert len(out) == 2
    assert out[0].ok is True and out[1].ok is False


# ------------------------------------------------------------------ 预览收口
def test_preview_ok_and_missing_values(svc):
    ok = svc.preview("t_open")
    assert ok["ok"] is True and ok["hex"]
    bad = svc.preview("t_set")
    assert bad["ok"] is False and "reg" in bad["detail"]
    none = svc.preview("没有这个id")
    assert none == {"ok": False, "detail": "模板不存在", "hex": ""}


def test_preview_manual_valid_invalid_empty(svc):
    good = svc.preview_manual("01 FF", mode="hex")
    assert good["ok"] is True
    assert "01FF" in good["hex"].replace(" ", "").upper()
    bad = svc.preview_manual("ZZGG", mode="hex")
    assert bad["ok"] is False and bad["hex"] == ""
    empty = svc.preview_manual("", mode="ascii", suffix="none")
    assert empty["ok"] is False and empty["detail"] == "内容为空"


# ------------------------------------------------------------------ 统计与转发
def test_publish_stats_emits_pt_stats(svc):
    got: list = []
    svc.bus.subscribe(Events.PT_STATS, lambda _t, p: got.append(p))
    st = svc.publish_stats()
    assert len(got) == 1
    assert got[0] is st
    assert st == svc.stats(), "stats() 每次现算，值应一致"


def test_query_forwards_and_records_flow(svc):
    data = svc.resolve("01 02", )
    assert isinstance(data, bytes) and data
    svc.send("01 02", mode="hex")
    recs = svc.all_records()
    if svc.cfg.passthrough.echo_tx:
        assert recs, "开了回显必须能查到存档"
    batch, cursor = svc.records(0)
    assert isinstance(batch, list) and isinstance(cursor, int)
    cleared = svc.clear()
    assert cleared >= 0


def test_configure_rejects_unknown_key(svc):
    with pytest.raises(AttributeError):
        svc.configure(nonexistent_field=1)


def test_summary_keys_and_repr(svc):
    st = svc.summary()
    assert {
        "endpoint", "subscribed", "connected", "tx_frames", "rx_frames",
        "tx_bytes", "rx_bytes", "timeouts", "errors", "records",
        "templates", "sent", "failed", "ticker",
    } == set(st)
    assert st["subscribed"] is True
    assert st["templates"] == 2
    assert "PassthroughService" in repr(svc)


def test_stop_unsubscribes(svc):
    assert svc.subscribed is True
    svc.stop()
    assert svc.subscribed is False


# ------------------------------------------------------------------ 收帧备注翻译成人话
def test_rx_wheeltec_frame_gets_human_readable_note(svc, link):
    frame = encode_wheeltec_frame(vx=0.16, wz=-0.03, voltage=23.4)
    for sink in list(link.demux.sinks):
        sink(frame, time.time())
    rx = [r for r in svc.all_records() if r.direction == Direction.RX.value]
    assert len(rx) == 1
    assert "底盘反馈" in rx[0].note
    assert "前向速度 0.160 m/s" in rx[0].note
    assert "角速度 -0.030 rad/s" in rx[0].note


def test_rx_non_wheeltec_frame_keeps_note_empty(svc, link):
    for sink in list(link.demux.sinks):
        sink(b"\x01\x03\x00\x00", time.time())
    rx = [r for r in svc.all_records() if r.direction == Direction.RX.value]
    assert len(rx) == 1 and rx[0].note == ""
