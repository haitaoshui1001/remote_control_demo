"""电台配置页测试（offscreen）。

表单行来自真实的 ``RadioService.catalog()``（纯内存，无需打开通道），
读/写/动作一律 monkeypatch 服务实例方法做确定性断言。周期回填沿用合并类
三件套：``bus.publish(RADIO_PARAMS, dict) → bridge._tick() → 数格子``。
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from PyQt5.QtGui import QPainter, QPixmap
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QComboBox, QLineEdit, QPushButton, QStyle, QStyleOptionViewItem

from hmi.core.events import Events
from hmi.services.session import build_session
from hmi.ui.bridge import UiBridge
from hmi.ui.pages.radio_page import _C_CUR, _C_DESC, _C_EDIT, RadioPage, _SingleLineDelegate


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
    p = RadioPage(session, bridge)
    yield p
    p.on_close()


def _cur(page: RadioPage, name: str) -> str:
    item = page.table.item(page._row_of[name], _C_CUR)
    return item.text() if item is not None else ""


def _wait_read(page: RadioPage, timeout_ms: int = 3000) -> None:
    """等后台读取线程收尾：``finished`` 在 ``done`` 之后投递，退出即回填完成。"""
    deadline = time.perf_counter() + timeout_ms / 1000.0
    while page._read_worker is not None and time.perf_counter() < deadline:
        QTest.qWait(5)
    assert page._read_worker is None
    assert page.btn_read.text() == "读取全部"
    assert page.btn_read.isEnabled() is True


# --------------------------------------------------------------------- 表单生成
def test_catalog_rows_and_groups(page, session):
    rows = session.radio.catalog()
    groups = {str(r.get("group", "")) for r in rows}
    assert "TXPOWER" in page._row_of
    assert page.table.rowCount() == len(rows) + len(groups)


def test_enum_param_gets_combo(page):
    widget = page.table.cellWidget(page._row_of["MODE"], _C_EDIT)
    assert isinstance(widget, QComboBox)
    assert widget.itemText(1) == "ap"
    assert widget.currentText() == ""


def test_desc_delegate_paint_raises_nothing(page):
    """highlightedText()/windowText() 返回 QBrush，直接喂 setPen 会抛 TypeError；
    异常被全局兜底吞掉后说明列文字整列画不出来，这里直接驱动 paint 防回归。"""
    delegate = _SingleLineDelegate(page.table)
    index = page.table.model().index(page._row_of["TXPOWER"], _C_DESC)
    assert index.data()  # 说明格必须有文字，否则走不到画笔分支
    pixmap = QPixmap(240, 30)
    painter = QPainter(pixmap)
    try:
        for selected in (False, True):
            opt = QStyleOptionViewItem()
            opt.initFrom(page.table)
            opt.widget = page.table
            opt.state |= QStyle.State_Selected if selected else QStyle.State_Enabled
            delegate.paint(painter, opt, index)
    finally:
        painter.end()


def test_int_param_gets_line_edit(page):
    widget = page.table.cellWidget(page._row_of["TXPOWER"], _C_EDIT)
    assert isinstance(widget, QLineEdit)


def test_readonly_row_disabled(page):
    widget = page.table.cellWidget(page._row_of["RSSI"], _C_EDIT)
    assert widget.isEnabled() is False
    assert page._kind_of["RSSI"] == "ro"


def test_action_row_kind(page):
    assert page._kind_of["PAIR"] == "action"
    assert page._kind_of["TXPOWER"] == "write"


# --------------------------------------------------------------------- 值格式化
def test_display_scale_and_lists(page):
    assert page._display("FREQ_RANGE", [8680, 9280]) == "868 / 928"
    assert page._display("TXPOWER", 20.0) == "20"
    assert page._display("CONN_STATE", True) == "是"
    assert page._display("SSID", None) == ""


# --------------------------------------------------------------------- 编辑收集
def test_collect_edits_combo_and_blank(page):
    page.table.cellWidget(page._row_of["MODE"], _C_EDIT).setCurrentIndex(1)
    edits = dict(page._collect_edits())
    assert edits.get("MODE") == "ap"
    assert "TXPOWER" not in edits


def test_clear_edits(page):
    page.table.cellWidget(page._row_of["TXPOWER"], _C_EDIT).setText("12")
    page.btn_clear.click()
    assert page._collect_edits() == []


# --------------------------------------------------------------------- 预览
def test_preview_valid_line(page):
    page.table.cellWidget(page._row_of["TXPOWER"], _C_EDIT).setText("15")
    page.btn_preview.click()
    assert "AT+TXPOWER=15" in page.preview_view.toPlainText()


def test_preview_rejects_out_of_range(page):
    page.table.cellWidget(page._row_of["TXPOWER"], _C_EDIT).setText("99")
    page.btn_preview.click()
    assert "TXPOWER=99" in page.preview_view.toPlainText()
    assert "AT+TXPOWER=99" not in page.preview_view.toPlainText()


# --------------------------------------------------------------------- 下发
def test_apply_uses_write_and_updates_current(page, session, monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(page, "say", lambda *a, **k: None)

    def fake_write(name, value):
        calls.append((name, value))
        return {"ok": True, "detail": "已写入"}

    monkeypatch.setattr(session.radio, "write", fake_write)
    page.table.cellWidget(page._row_of["TXPOWER"], _C_EDIT).setText("12")
    page.btn_apply.click()
    assert calls == [("TXPOWER", "12")]
    assert _cur(page, "TXPOWER") == "12"
    assert "OK" in page.preview_view.toPlainText()


def test_action_row_excluded_from_batch_apply(page, session, monkeypatch):
    """动作行即使填了新值也不进"下发修改"：只给普通参数下发，并提示逐行执行。"""
    calls: list = []
    monkeypatch.setattr(page, "say", lambda text, *a, **k: calls.append(text))
    monkeypatch.setattr(
        session.radio,
        "action",
        lambda *a, **k: (calls.append("action"), {"ok": True})[1],
    )
    monkeypatch.setattr(
        session.radio, "write", lambda n, v: (calls.append(("write", n)), {"ok": True})[1]
    )
    field = page._editors["PAIR"]
    if isinstance(field, QComboBox):
        field.setCurrentIndex(1)
    else:
        field.setText("1")
    page.table.cellWidget(page._row_of["TXPOWER"], _C_EDIT).setText("12")
    page.btn_apply.click()
    assert calls.count("action") == 0, "批量下发绝不能执行动作指令"
    assert ("write", "PAIR") not in calls
    assert ("write", "TXPOWER") in calls
    assert any("执行" in str(c) for c in calls)


def test_dangerous_action_asks_confirmation_before_send(page, session, monkeypatch):
    """LOADDEF 标了 confirm：确认框取消就不下发，确认后才带 confirmed=True 走 action()。"""
    from PyQt5.QtWidgets import QMessageBox

    from hmi.ui.pages import radio_page as rp

    asked: list[str] = []
    executed: list[tuple] = []
    monkeypatch.setattr(page, "say", lambda *a, **k: None)

    def fake_action(name, value="", *, confirmed=False):
        executed.append((name, value, confirmed))
        return {"ok": True, "detail": "OK"}

    monkeypatch.setattr(session.radio, "action", fake_action)
    monkeypatch.setattr(
        rp.QMessageBox,
        "question",
        lambda *a, **k: asked.append(a[2]) or QMessageBox.No,
    )
    box = page.table.cellWidget(page._row_of["LOADDEF"], _C_EDIT)
    btn = box.findChild(QPushButton)
    assert btn is not None and btn.text() == "执行"
    field = page._editors["LOADDEF"]
    if isinstance(field, QComboBox):
        field.setCurrentIndex(1)
    else:
        field.setText("1")

    btn.click()
    assert executed == [], "取消确认框后绝不能下发"
    assert asked and "清空" in asked[0] and "目标模块" in asked[0]

    monkeypatch.setattr(rp.QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    btn.click()
    assert executed == [("LOADDEF", "1", True)]


def test_action_without_value_is_not_sent(page, session, monkeypatch):
    """动作行留空：点执行只提示，不占用 AT 通道。"""
    executed: list[tuple] = []
    said: list[str] = []
    monkeypatch.setattr(page, "say", lambda text, *a, **k: said.append(text))
    monkeypatch.setattr(
        session.radio, "action", lambda *a, **k: executed.append(a) or {"ok": True}
    )
    box = page.table.cellWidget(page._row_of["SCAN_AP"], _C_EDIT)
    box.findChild(QPushButton).click()
    assert executed == []
    assert any("填写" in s for s in said)


def test_apply_reports_failure(page, session, monkeypatch):
    said: list[str] = []
    monkeypatch.setattr(page, "say", lambda text, *a, **k: said.append(text))
    monkeypatch.setattr(
        session.radio, "write", lambda n, v: {"ok": False, "detail": "模块回 ERROR"}
    )
    page.table.cellWidget(page._row_of["TXPOWER"], _C_EDIT).setText("12")
    page.btn_apply.click()
    assert "FAIL" in page.preview_view.toPlainText()
    assert any("0/1" in s for s in said)


def test_apply_skips_when_nothing_edited(page, session, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(page, "say", lambda text, *a, **k: calls.append(text))
    monkeypatch.setattr(session.radio, "write", lambda n, v: calls.append("write") or {"ok": True})
    page.btn_apply.click()
    assert calls == ["没有待下发的修改（新值列留空即不发送）"]


# --------------------------------------------------------------------- 读取
def _ok_result():
    return {
        "ok": True,
        "detail": "成功 2/2",
        "values": {
            "TXPOWER": {"ok": True, "value": 17},
            "SSID": {"ok": True, "value": "net1"},
        },
    }


def test_read_many_fills_cells(page, session, monkeypatch):
    said: list[str] = []
    monkeypatch.setattr(page, "say", lambda text, *a, **k: said.append(text))
    monkeypatch.setattr(session.radio, "read_many", lambda names=None: _ok_result())
    page.btn_read.click()
    _wait_read(page)
    assert _cur(page, "TXPOWER") == "17"
    assert _cur(page, "SSID") == "net1"
    assert any("读取完成" in s for s in said)


def test_read_runs_off_ui_thread_and_blocks_reentry(page, session, monkeypatch):
    """读取必须在后台线程：模块不应答时同步跑会界面冻死分钟级。"""
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def slow_read_many(names=None):
        calls.append(len(calls))
        started.set()
        release.wait(3)
        return _ok_result()

    monkeypatch.setattr(session.radio, "read_many", slow_read_many)
    page.btn_read.click()
    assert started.wait(3) is True
    assert page.btn_read.text() == "读取中…"
    assert page.btn_read.isEnabled() is False
    page._on_read()
    assert len(calls) == 1
    release.set()
    _wait_read(page)
    assert _cur(page, "TXPOWER") == "17"


def test_read_partial_failure_still_fills_cells(page, session, monkeypatch):
    said: list[str] = []
    monkeypatch.setattr(page, "say", lambda text, *a, **k: said.append(text))

    def partial(names=None):
        return {
            "ok": True,
            "detail": "成功 1/2",
            "values": {
                "TXPOWER": {"ok": True, "value": 17},
                "SSID": {"ok": False, "detail": "SSID 无响应"},
            },
        }

    monkeypatch.setattr(session.radio, "read_many", partial)
    page.btn_read.click()
    _wait_read(page)
    assert _cur(page, "TXPOWER") == "17"
    assert _cur(page, "SSID") == ""
    assert any("成功 1/2" in s for s in said)


def _page_with_link(qapp, session, bridge, monkeypatch, is_open, result):
    said: list[str] = []
    link = SimpleNamespace(is_open=is_open)
    fake = SimpleNamespace(bus=session.bus, radio=session.radio, link=link)
    p = RadioPage(fake, bridge)
    monkeypatch.setattr(p, "say", lambda text, *a, **k: said.append(text))
    monkeypatch.setattr(session.radio, "read_many", lambda names=None: result)
    return p, said


def test_read_failure_guides_when_channel_closed(qapp, session, bridge, monkeypatch):
    """通道没打开：提示里要带自救路径——列出可见串口并引导点“重新连接”。"""
    result = {"ok": False, "detail": "成功 0/2", "values": {
        "TXPOWER": {"ok": False, "detail": "AT 通道未打开（serial）"},
        "SSID": {"ok": False, "detail": "通道不可用，已跳过其余读取：AT 通道未打开（serial）"},
    }}
    p, said = _page_with_link(qapp, session, bridge, monkeypatch, False, result)
    try:
        p.btn_read.click()
        _wait_read(p)
        assert any(
            "AT 通道未打开" in s and "重新连接" in s and "可见串口" in s for s in said
        )
    finally:
        p.on_close()


def test_read_failure_reports_first_reason(qapp, session, bridge, monkeypatch):
    """通道是开的、模块不应答：把第一条失败原因递到用户眼前。"""
    result = {"ok": False, "detail": "成功 0/2", "values": {
        "TXPOWER": {"ok": False, "detail": "超时未响应"},
        "SSID": {"ok": False, "detail": "超时未响应"},
    }}
    p, said = _page_with_link(qapp, session, bridge, monkeypatch, True, result)
    try:
        p.btn_read.click()
        _wait_read(p)
        assert any("读取失败" in s and "超时未响应" in s for s in said)
        assert not any("重启程序" in s for s in said)
    finally:
        p.on_close()


def test_on_close_waits_for_running_read(qapp, session, bridge, monkeypatch):
    release = threading.Event()

    def blocking_read_many(names=None):
        release.wait(3)
        return _ok_result()

    monkeypatch.setattr(session.radio, "read_many", blocking_read_many)
    p = RadioPage(session, bridge)
    p.btn_read.click()
    QTest.qWait(50)
    release.set()
    p.on_close()
    assert p._read_worker is None


def test_defaults_reloads_catalog(page, session, monkeypatch):
    monkeypatch.setattr(page, "say", lambda *a, **k: None)
    monkeypatch.setattr(session.radio, "load_defaults", lambda: {"ok": True, "detail": ""})
    page.btn_defaults.click()
    assert "TXPOWER" in page._row_of


# --------------------------------------------------------------------- 遥测回填
def test_params_view_fills_table(page, session, bridge):
    view = {
        "endpoint": "local",
        "ts": time.time(),
        "role": "ap",
        "connected": True,
        "ssid": "hgic_x",
        "keymgmt": "WPA-PSK",
        "bandwidth_mhz": 8,
        "freq_range": [8680, 9280],
        "chan_list": [8680],
        "txpower_dbm": 20.0,
        "tx_mcs": 10,
        "acktmo_us": 0,
        "heart_int_ms": 1000,
    }
    session.bus.publish(Events.RADIO_PARAMS, view)
    bridge._tick()
    assert _cur(page, "TXPOWER") == "20"
    assert _cur(page, "SSID") == "hgic_x"
    assert _cur(page, "FREQ_RANGE") == "868 / 928"
    assert _cur(page, "MODE") == "ap"
    assert _cur(page, "CONN_STATE") == "已连接"
    assert "已连接" in page.tel_label.text()


def test_slot_other_topic_ignored(page, session, bridge):
    session.bus.publish(Events.LINK_SNAPSHOT, object())
    bridge._tick()
    assert _cur(page, "TXPOWER") == ""


# --------------------------------------------------------------------- 降级
def test_degraded_without_radio(qapp, session, bridge):
    bare = SimpleNamespace(bus=session.bus, radio=None)
    p = RadioPage(bare, bridge)
    assert p.btn_read.isEnabled() is False
    assert p.btn_apply.isEnabled() is False
    assert "未启用" in p.tel_label.text()
    p.on_close()


# --------------------------------------------------------------------- 多模块设备选择
def test_device_row_disabled_on_non_udp_channel(page):
    assert page.combo_device.isEnabled() is False
    assert page.btn_discover.isEnabled() is False
    assert "不支持" in page.dev_label.text()


def test_discover_populates_and_switch_selects_device(qapp, session, bridge, monkeypatch):
    mod_a, mod_b = "96-a1-3e-7d-83-88", "96-a1-3e-97-66-80"
    rows = [
        {"mac": mod_a, "online": True, "seen_ago_s": 0.1, "source_ip": "0.0.0.0"},
        {"mac": mod_b, "online": False, "seen_ago_s": 9.0, "source_ip": "0.0.0.0"},
    ]
    switched: list[str] = []
    link = SimpleNamespace(
        is_open=True,
        channel="udp",
        at=SimpleNamespace(target_mac=b""),
        discover=lambda: {
            "ok": True,
            "count": 2,
            "detail": "在线 2 个模块（共 2 个）：96-a1-3e-7d-83-88、96-a1-3e-97-66-80",
            "modules": rows,
        },
        at_modules=lambda: [],
        select_device=lambda mac: {"ok": True, "mac": mac.lower(), "detail": ""},
    )
    fake = SimpleNamespace(bus=session.bus, radio=session.radio, link=link)
    p = RadioPage(fake, bridge)
    monkeypatch.setattr(p, "say", lambda text, *a, **k: None)
    monkeypatch.setattr(
        session.radio,
        "select_device",
        lambda mac: (
            switched.append(mac),
            {"ok": True, "mac": mac.lower(), "detail": ""},
        )[1],
    )
    try:
        assert p.combo_device.isEnabled() is True
        assert p.btn_discover.isEnabled() is True
        assert p.dev_label.text() == "未发现模块，点“发现/刷新”"
        p.btn_discover.click()
        got = [p.combo_device.itemData(i) for i in range(p.combo_device.count())]
        assert got == ["", mod_a, mod_b]
        assert "共 2 个，在线 1 个" in p.dev_label.text()
        p.combo_device.setCurrentIndex(p.combo_device.findData(mod_a))
        assert switched == [mod_a]
        p.combo_device.setCurrentIndex(0)
        assert switched == [mod_a, ""]
    finally:
        p.on_close()


def _two_online_page(qapp, session, bridge, monkeypatch, switched: list[str]):
    """造一台"两台都在线"的链路并建页：返回页面，发现已点过一次。"""
    mod_a, mod_b = "96-a1-3e-7d-83-88", "96-a1-3e-97-66-80"
    rows = [
        {"mac": mod_a, "online": True, "seen_ago_s": 0.1, "source_ip": "0.0.0.0"},
        {"mac": mod_b, "online": True, "seen_ago_s": 0.2, "source_ip": "0.0.0.0"},
    ]
    link = SimpleNamespace(
        is_open=True,
        channel="udp",
        at=SimpleNamespace(target_mac=b""),
        discover=lambda: {"ok": True, "count": 2, "detail": "", "modules": rows},
        at_modules=lambda: rows,
        select_device=lambda mac: {"ok": True, "mac": mac.lower(), "detail": ""},
    )
    monkeypatch.setattr(
        session.radio, "select_device",
        lambda mac: switched.append(mac) or {"ok": True, "mac": mac.lower(), "detail": ""},
    )
    fake = SimpleNamespace(bus=session.bus, radio=session.radio, link=link)
    p = RadioPage(fake, bridge)
    monkeypatch.setattr(p, "say", lambda text, *a, **k: None)
    p.btn_discover.click()
    return p


def test_two_online_modules_remove_auto_choice(qapp, session, bridge, monkeypatch):
    """两台都在线时不再提供"自动（首个在线模块）"，也不默认选中任何一台。"""
    switched: list[str] = []
    p = _two_online_page(qapp, session, bridge, monkeypatch, switched)
    try:
        got = [p.combo_device.itemData(i) for i in range(p.combo_device.count())]
        assert got == ["96-a1-3e-7d-83-88", "96-a1-3e-97-66-80"]
        assert p.combo_device.currentIndex() == -1
        assert "在线 2 个" in p.dev_label.text()
    finally:
        p.on_close()


def test_send_blocked_until_one_module_chosen(qapp, session, bridge, monkeypatch):
    """未选定目标：读取/下发/动作全部阻断，一次都不会碰到模块。"""
    switched: list[str] = []
    p = _two_online_page(qapp, session, bridge, monkeypatch, switched)
    hits: list = []
    monkeypatch.setattr(session.radio, "write",
                        lambda n, v, **k: hits.append(("write", n)) or {"ok": True})
    monkeypatch.setattr(session.radio, "action",
                        lambda n, v, **k: hits.append(("action", n)) or {"ok": True})
    monkeypatch.setattr(session.radio, "read_many",
                        lambda *a, **k: hits.append(("read",)) or {"ok": True, "values": {}})
    try:
        p.table.cellWidget(p._row_of["TXPOWER"], _C_EDIT).setText("12")
        p.btn_apply.click()
        p.btn_read.click()
        assert p._read_worker is None, "未选定目标时不该起读取线程"
        p._execute_action("PAIR")
        assert hits == []
        assert switched == []
    finally:
        p.on_close()


def test_chosen_module_unblocks_send(qapp, session, bridge, monkeypatch):
    """手选一台之后恢复正常下发，并在预览区写明这条指令发给了谁。"""
    switched: list[str] = []
    p = _two_online_page(qapp, session, bridge, monkeypatch, switched)
    hits: list = []
    monkeypatch.setattr(
        session.radio, "write",
        lambda n, v, **k: hits.append((n, v)) or {"ok": True, "detail": "已下发并回读确认为 12"},
    )
    try:
        p.combo_device.setCurrentIndex(p.combo_device.findData("96-a1-3e-97-66-80"))
        assert switched == ["96-a1-3e-97-66-80"]
        p.table.cellWidget(p._row_of["TXPOWER"], _C_EDIT).setText("12")
        p.btn_apply.click()
        assert hits == [("TXPOWER", "12")]
        assert "96-a1-3e-97-66-80" in p.preview_view.toPlainText()
    finally:
        p.on_close()


def test_apply_shows_silent_noop_readback(page, session, monkeypatch):
    """模块回 OK 但回读没变：当前值列改显真实读值，提示里说清"没生效"。"""
    said: list[str] = []
    monkeypatch.setattr(page, "say", lambda text, *a, **k: said.append(text))
    monkeypatch.setattr(
        session.radio, "write",
        lambda n, v, **k: {
            "ok": False, "verified": False, "readback": 6,
            "detail": "写入未生效：下发 8，回读 6（+TXPOWER: 6dbm）",
        },
    )
    page.table.cellWidget(page._row_of["TXPOWER"], _C_EDIT).setText("8")
    page.btn_apply.click()
    assert _cur(page, "TXPOWER") == "6"
    assert "写入未生效" in page.preview_view.toPlainText()
    assert any("没变" in s for s in said)


def test_dev_label_tracks_targeting(qapp, session, bridge, monkeypatch):
    """设备标签同时反映"模块表 + 实际定向"：未定向要说清遥测已暂停，选定后立刻改口。"""
    switched: list[str] = []
    p = _two_online_page(qapp, session, bridge, monkeypatch, switched)

    def picker(mac: str) -> dict:
        switched.append(mac)
        session.radio.at.target_mac = bytes.fromhex(mac.replace("-", "")) if mac else b""
        return {"ok": True, "mac": mac.lower(), "detail": ""}

    monkeypatch.setattr(session.radio, "select_device", picker)
    try:
        assert "在线 2 个" in p.dev_label.text()
        assert "遥测已暂停" in p.dev_label.text()
        p.combo_device.setCurrentIndex(p.combo_device.findData("96-a1-3e-97-66-80"))
        assert switched == ["96-a1-3e-97-66-80"]
        assert "当前定向 96-a1-3e-97-66-80" in p.dev_label.text()
        assert "遥测已暂停" not in p.dev_label.text()
    finally:
        p.on_close()

