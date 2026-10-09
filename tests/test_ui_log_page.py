"""日志页测试（offscreen）。

三条 STREAM 流水统一走 ``publish -> bridge._tick() -> on_batch`` 真实通路；
负载用 models 里的真对象（AtExchange / AlarmEvent）与 LOG 的 dict 形状。
过滤、计数、清空、录制路径各测各的，级别过滤只应影响告警与运行日志。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from PyQt5.QtWidgets import QPlainTextEdit

from hmi.core.events import Events
from hmi.core.models import AlarmEvent, AtExchange
from hmi.services.session import build_session
from hmi.ui.bridge import UiBridge
from hmi.ui.pages.log_page import LogPage


@pytest.fixture()
def session():
    return build_session(None, mock=True)


@pytest.fixture()
def bridge(session):
    b = UiBridge(session.bus, interval_ms=200)
    b.start()
    yield b
    b.stop()


@pytest.fixture()
def page(qapp, session, bridge):
    p = LogPage(session, bridge)
    yield p
    p.on_close()


def _text(page: LogPage) -> str:
    return page.view.toPlainText()


def _log_line(level: str = "INFO", text: str = "运行日志样本") -> dict:
    return {
        "ts": time.time(),
        "level": level,
        "logger": "hmi.test",
        "text": f"[{level}] hmi.test: {text}",
        "extra": {},
    }


# --------------------------------------------------------------------- 构建
def test_page_builds_widgets(page):
    assert page.view.isReadOnly()
    assert page.chk_at.isChecked()
    assert page.chk_alarm.isChecked()
    assert page.chk_log.isChecked()
    assert page.cmb_level.count() == 3
    assert page.view.maximumBlockCount() >= 1000
    assert page.view.lineWrapMode() == QPlainTextEdit.WidgetWidth
    assert "AT 0" in page.lbl_counts.text()


# --------------------------------------------------------------------- AT 流水
def test_at_exchange_appended(page, session, bridge):
    item = AtExchange(
        line="AT+TXPOWER=15",
        kind="set",
        name="TXPOWER",
        ts=time.time(),
        rtt_ms=12.3,
        ok=True,
        response=["OK"],
        channel="mock",
        endpoint="local",
    )
    session.bus.publish(Events.AT_EXCHANGE, item)
    bridge._tick()
    txt = _text(page)
    assert "TXPOWER" in txt
    assert "AT+TXPOWER=15" in txt
    assert "OK" in txt
    assert "[mock]" in txt
    assert "AT 1" in page.lbl_counts.text()


def test_at_exchange_failed_shows_error(page, session, bridge):
    item = AtExchange(
        line="AT+PAIR=1",
        kind="action",
        name="PAIR",
        ok=False,
        error="超时未响应",
        channel="serial",
    )
    session.bus.publish(Events.AT_EXCHANGE, item)
    bridge._tick()
    txt = _text(page)
    assert "FAIL" in txt
    assert "超时未响应" in txt


def test_at_batch_three_items(page, session, bridge):
    for i in range(3):
        session.bus.publish(Events.AT_EXCHANGE, AtExchange(name=f"P{i}", line=f"AT+i={i}"))
    bridge._tick()
    assert "AT 3" in page.lbl_counts.text()
    txt = _text(page)
    for i in range(3):
        assert f"AT+i={i}" in txt


def test_each_event_starts_its_own_line(page, session, bridge):
    """批量事件必须逐条各占一行（<br/> 不算新段落，曾把多条挤在同一行）。"""
    for i in range(3):
        session.bus.publish(Events.AT_EXCHANGE, AtExchange(name=f"P{i}", line=f"AT+i={i}"))
    session.bus.publish(Events.ALARM, AlarmEvent(level="warn", code="X", message="告警样本"))
    bridge._tick()
    lines = [ln for ln in _text(page).splitlines() if ln.strip()]
    assert len(lines) >= 4
    assert page.view.document().blockCount() >= 4
    assert all(sum(ln.count(f"AT+i={j}") for j in range(3)) <= 1 for ln in lines)


# --------------------------------------------------------------------- 告警流水
def test_alarm_appended(page, session, bridge):
    alarm = AlarmEvent(
        ts=time.time(),
        level="warn",
        source="link",
        code="RSSI_LOW",
        message="RSSI -88 dBm 低于阈值 -85",
    )
    session.bus.publish(Events.ALARM, alarm)
    bridge._tick()
    txt = _text(page)
    assert "RSSI_LOW" in txt
    assert "[WARN]" in txt
    assert "告警 1" in page.lbl_counts.text()


def test_alarm_non_object_survives(page, session, bridge):
    session.bus.publish(Events.ALARM, {"level": "crit", "message": "字典告警"})
    bridge._tick()
    assert "告警 1" in page.lbl_counts.text()
    assert "crit" in _text(page).lower() or "告警" in _text(page)


# --------------------------------------------------------------------- 运行日志
def test_log_dict_appended(page, session, bridge):
    session.bus.publish(Events.LOG, _log_line("INFO", "链路已建立"))
    bridge._tick()
    assert "链路已建立" in _text(page)
    assert "日志 1" in page.lbl_counts.text()


def test_log_non_dict_survives(page, session, bridge):
    session.bus.publish(Events.LOG, "纯字符串日志")
    bridge._tick()
    assert "纯字符串日志" in _text(page)


def test_log_html_special_chars_escaped(page, session, bridge):
    session.bus.publish(Events.LOG, _log_line("INFO", "<b>粗体&标签</b>"))
    bridge._tick()
    assert "<b>粗体&标签</b>" in _text(page)


# --------------------------------------------------------------------- 过滤
def test_source_filter_hides_but_counts(page, session, bridge):
    page.chk_at.setChecked(False)
    session.bus.publish(Events.AT_EXCHANGE, AtExchange(line="AT+CIRCUIT=123", name="CIRCUIT"))
    bridge._tick()
    assert "AT+CIRCUIT=123" not in _text(page)
    assert "AT 1" in page.lbl_counts.text()


def test_level_filter_warning_up(page, session, bridge):
    page.cmb_level.setCurrentIndex(1)
    session.bus.publish(Events.LOG, _log_line("INFO", "低级别消息"))
    session.bus.publish(Events.LOG, _log_line("WARNING", "高级别消息"))
    session.bus.publish(Events.ALARM, AlarmEvent(level="warn", code="EVM_HIGH", message="w"))
    session.bus.publish(Events.AT_EXCHANGE, AtExchange(line="AT+PING=1", name="PING"))
    bridge._tick()
    txt = _text(page)
    assert "低级别消息" not in txt
    assert "高级别消息" in txt
    assert "EVM_HIGH" in txt
    assert "AT+PING=1" in txt


def test_level_filter_error_only(page, session, bridge):
    page.cmb_level.setCurrentIndex(2)
    session.bus.publish(Events.LOG, _log_line("WARNING", "警告消息"))
    session.bus.publish(Events.LOG, _log_line("ERROR", "错误消息"))
    session.bus.publish(Events.ALARM, AlarmEvent(level="warn", code="RSSI_LOW", message="w"))
    session.bus.publish(Events.ALARM, AlarmEvent(level="crit", code="LINK_DOWN", message="c"))
    bridge._tick()
    txt = _text(page)
    assert "警告消息" not in txt
    assert "错误消息" in txt
    assert "RSSI_LOW" not in txt
    assert "LINK_DOWN" in txt


def test_unknown_topic_ignored(page):
    page.on_batch("some/other", [1, 2, 3])
    assert _text(page) == ""
    assert "AT 0" in page.lbl_counts.text()


# --------------------------------------------------------------------- 动作
def test_clear_resets_view_and_counts(page, session, bridge):
    session.bus.publish(Events.AT_EXCHANGE, AtExchange(line="AT+X=1", name="X"))
    session.bus.publish(Events.LOG, _log_line())
    bridge._tick()
    page._on_clear()
    assert _text(page) == ""
    assert page.lbl_counts.text() == "AT 0 · 告警 0 · 日志 0"


def test_paths_button_shows_dict(page, session, bridge, monkeypatch):
    said = []
    monkeypatch.setattr(page, "say", lambda text, *a, **k: said.append(text))
    paths = {"at_path": "D:/rec/at.log", "csv": "D:/rec/s.csv"}
    monkeypatch.setattr(session.record, "paths", lambda: paths)
    page._on_paths()
    assert "at_path" in page.lbl_paths.text()
    assert "D:/rec/at.log" in page.lbl_paths.text()
    assert not page.lbl_paths.isHidden()
    assert any("路径已显示" in s for s in said)


def test_paths_empty_says_hint(page, session, monkeypatch):
    said = []
    monkeypatch.setattr(page, "say", lambda text, *a, **k: said.append(text))
    monkeypatch.setattr(session.record, "paths", lambda: {})
    page._on_paths()
    assert any("还没生成文件" in s for s in said)


def test_paths_button_disabled_without_record(qapp, session, bridge):
    bare = SimpleNamespace(bus=session.bus, record=None)
    p = LogPage(bare, bridge)
    try:
        assert not p.btn_paths.isEnabled()
    finally:
        p.on_close()
