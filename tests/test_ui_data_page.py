"""数据流水页测试（offscreen）。

沿用控制台页的确定性三件套：mock 会话 + 手动 ``bridge._tick()`` 交付事件。
流水表只认 ``PT_BYTES`` 批次事件这一条进路，所以绝大多数用例都是
"bus.publish 几条 ByteRecord → tick → 数行数"；统计条单独验证合并类
``PT_STATS`` 走 ``on_slot`` + ``latest``。CSV 导出一律传路径绕开对话框。
"""

from __future__ import annotations

import csv
import time
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QHeaderView

from hmi.core.events import Events
from hmi.core.models import ByteRecord, PassthroughStats
from hmi.services.session import build_session
from hmi.ui.bridge import UiBridge
from hmi.ui.pages import data_page as dp
from hmi.ui.pages.data_page import _C_DIR, _C_HEX, _C_TS, DataPage


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
    p = DataPage(session, bridge)
    yield p
    p.on_close()


def _rec(direction: str = "rx", data: bytes = b"AB", kind: str = "", **kw) -> ByteRecord:
    return ByteRecord(
        ts=kw.pop("ts", time.time()),
        direction=direction,
        data=data,
        channel=kw.pop("channel", "usb"),
        endpoint=kw.pop("endpoint", "485-USB"),
        note=kw.pop("note", ""),
        kind=kind or direction,
        **kw,
    )


def _push(bus, *records: ByteRecord) -> None:
    for rec in records:
        bus.publish(Events.PT_BYTES, rec)


def _rows(page: DataPage) -> int:
    return page._proxy.rowCount()


# ------------------------------------------------------------------ 进表通路
def test_backfill_from_all_records(qapp, session, bridge, monkeypatch):
    monkeypatch.setattr(session.passthrough, "all_records", lambda: [_rec(), _rec(), _rec()])
    p = DataPage(session, bridge)
    try:
        assert _rows(p) == 3
        assert p._model.rowCount() == 3
    finally:
        p.on_close()


def test_batch_events_append_rows(page, session, bridge):
    _push(session.bus, _rec(data=b"AB"), _rec(direction="tx", data=b"CD"))
    bridge._tick()
    assert _rows(page) == 2
    hex0 = str(page._proxy.index(0, _C_HEX).data(Qt.DisplayRole))
    assert hex0 == "41 42"


def test_other_topic_does_not_enter_table(page, session, bridge):
    session.bus.publish(Events.AT_EXCHANGE, "not-a-record")
    bridge._tick()
    assert _rows(page) == 0


def test_direction_and_kind_labels(page, session, bridge):
    _push(session.bus, _rec(direction="tx", data=b"12", kind="command"))
    bridge._tick()
    texts = [str(page._proxy.index(0, c).data(Qt.DisplayRole)) for c in (_C_DIR, _C_HEX)]
    assert texts == ["发送", "31 32"]


def test_new_kind_added_to_combo(page, session, bridge):
    assert page.kind_combo.findData("ota") < 0
    _push(session.bus, _rec(kind="ota"))
    bridge._tick()
    assert page.kind_combo.findData("ota") >= 0


# ------------------------------------------------------------------ 过滤与排序
def test_direction_filter(page, session, bridge):
    _push(session.bus, _rec(), _rec(direction="tx"))
    bridge._tick()
    page.dir_combo.setCurrentIndex(page.dir_combo.findData("tx"))
    page._on_filter_changed()
    assert _rows(page) == 1
    assert page._proxy.index(0, _C_DIR).data(Qt.UserRole) == "tx"


def test_kind_filter(page, session, bridge):
    _push(session.bus, _rec(kind="template"), _rec(kind="reply"))
    bridge._tick()
    page.kind_combo.setCurrentIndex(page.kind_combo.findData("reply"))
    page._on_filter_changed()
    assert _rows(page) == 1


def test_keyword_filter_matches_hex_without_spaces(page, session, bridge):
    _push(session.bus, _rec(data=b"AB"), _rec(data=b"FF", endpoint="UDP:6000"))
    bridge._tick()
    page._proxy.set_keyword("4142")
    assert _rows(page) == 1
    page._proxy.set_keyword("udp")
    assert _rows(page) == 1
    page._proxy.set_keyword("zzz")
    assert _rows(page) == 0


def test_sort_by_time_descending(page, session, bridge):
    _push(session.bus, _rec(ts=1000.0), _rec(ts=2000.0))
    bridge._tick()
    page.table.sortByColumn(_C_TS, Qt.DescendingOrder)
    assert float(page._proxy.index(0, _C_TS).data(Qt.UserRole)) == 2000.0


def test_model_caps_rows(qapp, session, bridge, monkeypatch):
    monkeypatch.setattr(dp, "_MAX_ROWS", 5)
    p = DataPage(session, bridge)
    try:
        _push(session.bus, *[_rec(ts=1000.0 + i) for i in range(8)])
        bridge._tick()
        assert p._model.rowCount() == 5
        assert float(p._model.index(0, _C_TS).data(Qt.UserRole)) == 1003.0
    finally:
        p.on_close()


# ------------------------------------------------------------------ 统计条
def test_stats_slot_updates_labels(page, session, bridge):
    st = PassthroughStats(
        tx_bytes=4, rx_bytes=7, tx_frames=2, rx_frames=3,
        rx_rate_bps=2048.0, rx_rate_fps=1.5, pending=2, timeouts=1,
        connected=True, port="COM9",
    )
    session.bus.publish(Events.PT_STATS, st)
    bridge._tick()
    assert "已连接" in page._labels["conn"].text()
    assert "COM9" in page._labels["port"].text()
    assert "收 3/发 2" in page._labels["frames"].text()
    assert "收 7/发 4" in page._labels["bytes"].text()
    assert "2.0 kbps" in page._labels["rate"].text()
    assert "待答 2 超时 1 错误 0" in page._labels["health"].text()


def test_flow_label_shows_visible_and_total(page, session, bridge):
    _push(session.bus, _rec(), _rec(direction="tx"))
    bridge._tick()
    page.kw_edit.setText("zzz")
    assert "行数 0/2" in page._labels["flow"].text()


# ------------------------------------------------------------------ 暂停/清空/导出
def test_pause_buffers_then_flushes(page, session, bridge):
    page.pause_btn.setChecked(True)
    _push(session.bus, _rec(), _rec())
    bridge._tick()
    assert _rows(page) == 0
    assert "暂停积压 2" in page._labels["flow"].text()
    page.pause_btn.setChecked(False)
    assert _rows(page) == 2


def test_pending_overflow_is_counted(qapp, session, bridge, monkeypatch):
    monkeypatch.setattr(dp, "_PENDING_CAP", 3)
    p = DataPage(session, bridge)
    try:
        p._on_pause(True)
        _push(session.bus, *[_rec(ts=1000.0 + i) for i in range(5)])
        bridge._tick()
        assert len(p._pending) == 3
        assert p._pending_dropped == 2
        assert "溢出丢 2" in p._labels["flow"].text()
        p._on_pause(False)
        assert _rows(p) == 3
    finally:
        p.on_close()


def test_clear_empties_model(page, session, bridge):
    _push(session.bus, _rec())
    bridge._tick()
    assert _rows(page) == 1
    page._on_clear()
    assert page._model.rowCount() == 0
    assert session.passthrough.all_records() == []


def test_export_csv_respects_filter(page, session, bridge, tmp_path):
    _push(session.bus, _rec(data=b"AB"), _rec(direction="tx", data=b"CD", kind="manual"))
    bridge._tick()
    page.dir_combo.setCurrentIndex(page.dir_combo.findData("tx"))
    page._on_filter_changed()
    out = page.export_csv(str(tmp_path / "flow"))
    assert out is not None and out.endswith("flow.csv")
    with open(out, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == list(dp._COLS)
    assert len(rows) == 2
    assert rows[1][_C_DIR] == "发送"
    assert rows[1][_C_HEX] == "43 44"
    assert rows[1][0].count("-") == 2  # 导出带完整日期


def test_export_empty_table_still_writes_header(page, tmp_path):
    out = page.export_csv(str(tmp_path / "empty.csv"))
    with open(out, newline="", encoding="utf-8-sig") as fh:
        assert list(csv.reader(fh)) == [list(dp._COLS)]


# ------------------------------------------------------------------ 降级
def test_missing_service_disables_actions(qapp, session, bridge):
    stub = SimpleNamespace(bus=session.bus, passthrough=None)
    p = DataPage(stub, bridge)
    try:
        assert not p.pause_btn.isEnabled()
        assert not p.export_btn.isEnabled()
        assert not p.clear_btn.isEnabled()
        assert "未启用" in p._labels["conn"].text()
        assert _rows(p) == 0
    finally:
        p.on_close()


# ------------------------------------------------------------------ 列宽交互
def test_columns_are_freely_resizable(page):
    hdr = page.table.horizontalHeader()
    for col in range(len(dp._COLS) - 1):
        assert hdr.sectionResizeMode(col) == QHeaderView.Interactive, "列宽必须可手动拖动"
    for col, width in enumerate(dp._COL_WIDTHS):
        assert page.table.columnWidth(col) == width, "初始宽度按列配置生效"
    assert hdr.stretchLastSection(), "备注列自动撑满剩余空间"
