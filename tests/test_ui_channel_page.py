"""信道监控页测试（offscreen）。

曲线部件单独测数据层（ts 去重 / 回填过滤 / 清空 / 上限截断），渲染断言
直接调 ``_render`` 避开桥时序；``on_slot`` 通路走合并类三件套
``publish(LINK_SNAPSHOT) -> bridge._tick() -> 看控件``。
动作按钮一律 monkeypatch 服务实例方法做确定性断言。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from PyQt5.QtGui import QPainter, QPixmap
from PyQt5.QtWidgets import QHeaderView

from hmi.core.events import Events
from hmi.core.models import LinkSnapshot, StaInfo
from hmi.services.session import build_session
from hmi.ui.bridge import UiBridge
from hmi.ui.pages.channel_page import _MAX_POINTS, ChannelPage, _CurveWidget, _mcs_text


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
    p = ChannelPage(session, bridge)
    yield p
    p.on_close()


def _snap(**kw):
    base = {
        "ts": time.time(),
        "endpoint": "local",
        "role": "ap",
        "connected": True,
        "rssi_dbm": -70.0,
        "evm_db": -30.0,
        "tx_mcs": 10,
        "bandwidth_mhz": 8,
        "freq_mhz": 868.0,
        "txpower_dbm": 20.0,
        "sta_count": 0,
        "sta_list": [],
        "degraded": False,
        "errors": [],
    }
    base.update(kw)
    return LinkSnapshot(**base)


# --------------------------------------------------------------------- 曲线数据层
def test_curve_add_dedups_by_ts(qapp):
    curve = _CurveWidget(-85.0, -92.0, -20.0)
    assert curve.add(100.0, -70.0, -30.0) is True
    assert curve.add(90.0, -71.0, -31.0) is False
    assert curve.add(100.0, -72.0, -32.0) is False
    assert curve.add(101.0, -73.0, -33.0) is True
    pts = curve.points()
    assert len(pts) == 2
    assert pts[-1] == (101.0, -73.0, -33.0)


def test_curve_reset_filters_non_snapshots(qapp):
    curve = _CurveWidget(-85.0, -92.0, -20.0)
    curve.reset([_snap(ts=5.0), "junk", _snap(ts=9.0), object()])
    pts = curve.points()
    assert [p[0] for p in pts] == [5.0, 9.0]


def test_curve_clear_points(qapp):
    curve = _CurveWidget(-85.0, -92.0, -20.0)
    curve.add(1.0, -70.0, -30.0)
    curve.clear_points()
    assert curve.points() == []


def test_curve_trims_to_max_points(qapp):
    curve = _CurveWidget(-85.0, -92.0, -20.0)
    for i in range(_MAX_POINTS + 5):
        curve.add(float(i), -70.0, -30.0)
    pts = curve.points()
    assert len(pts) == _MAX_POINTS
    assert pts[0][0] == 5.0


def test_curve_span_adaptive(qapp):
    assert _CurveWidget._span([], -100.0, -40.0) == (-100.0, -40.0)
    # 近似直线：先撑开 ±5（4.0~14.0），再各扩 10% 边距（pad=1.0）。
    assert _CurveWidget._span([9.0, 9.0], -100.0, -40.0) == pytest.approx((3.0, 15.0))
    # 正常分布：min/max 各扩 10%（跨度 10 → pad 1.0）。
    assert _CurveWidget._span([-70.0, -60.0], -100.0, -40.0) == pytest.approx((-71.0, -59.0))


def test_curve_ts_at_interpolates(qapp):
    curve = _CurveWidget(-85.0, -92.0, -20.0)
    curve.add(100.0, -70.0, -30.0)
    curve.add(200.0, -71.0, -31.0)
    assert _CurveWidget._ts_at(curve.points(), 0.0) == 100.0
    assert _CurveWidget._ts_at(curve.points(), 1.0) == 200.0
    assert _CurveWidget._ts_at(curve.points(), 0.5) == 150.0
    # 单点/空点序也要给出可用时间戳，不许抛错。
    assert _CurveWidget._ts_at([(123.0, None, None)], 0.7) == 123.0
    assert isinstance(_CurveWidget._ts_at([], 0.5), float)


def test_curve_paint_with_ticks_raises_nothing(qapp):
    """时间刻度 + 左右轴 5 档刻度值：离屏 painter 直调 _paint，绘制出错当场暴露。"""
    curve = _CurveWidget(-85.0, -92.0, -20.0)
    base = 1_700_000_000.0
    for i in range(10):
        curve.add(base + i * 3.0, -70.0 + i, None if i < 3 else -30.0 + i)
    pm = QPixmap(600, 300)
    painter = QPainter(pm)
    try:
        curve._paint(painter, 600, 300)  # EVM 缺点 + 时间刻度分支
        curve.clear_points()
        curve.add(base, -70.0, None)
        curve._paint(painter, 600, 300)  # 单点空态分支
    finally:
        painter.end()


def test_time_box_uses_real_text_metrics(qapp):
    """时间框高=字体实际行高、宽=传入的文字实宽：写死 16x84 时高分屏缩放下
    Consolas 实际度量超出框，drawText 按矩形裁剪，真机上时间轴就成了半个字。"""
    from PyQt5.QtGui import QFontMetrics

    from hmi.ui.theme import small_font

    fm = QFontMetrics(small_font())
    fh = fm.height()
    bw = fm.horizontalAdvance("00:00:00") + 8
    box = _CurveWidget._time_box(46, 220, 800, 260, fh, bw)
    assert box.height() == fh
    assert box.width() == bw  # 框宽必须容得下 HH:MM:SS 整串，两端不裁
    assert box.y() + box.height() <= 260  # 底缘不出 widget
    assert box.x() >= 0  # 首条格线不越左
    last = _CurveWidget._time_box(736, 220, 800, 260, fh, bw)
    assert last.x() + last.width() <= 800  # 末条格线不越右
    big = _CurveWidget._time_box(46, 220, 800, 260, 40, 200)  # 极端大字号仍被钳进窗口
    assert big.y() + big.height() <= 259


# --------------------------------------------------------------------- 页面构建
def test_page_builds_core_widgets(page):
    assert isinstance(page.curve, _CurveWidget)
    assert page.sta_table.columnCount() == 9
    assert page.btn_once.isEnabled()
    assert page.btn_start.isEnabled()
    assert page.btn_stop.isEnabled()
    assert page.chk_follow.isChecked()


def test_page_endpoint_label(page):
    assert "端点" in page.endpoint_label.text()


# --------------------------------------------------------------------- 渲染
def test_render_connected_and_metrics(page):
    page._render(_snap(connected=True, role="ap"))
    assert "已连接（ap）" in page.lbl_link.text()
    assert "#1b7a43" in page.lbl_link.styleSheet()
    assert page.lbl_metrics.text() == "RSSI -70.0 dBm · EVM -30.0 dB"


def test_render_disconnected(page):
    page._render(_snap(connected=False))
    assert "未连接" in page.lbl_link.text()
    assert "#b3261e" in page.lbl_link.styleSheet()


def test_render_unknown_state_and_null_metrics(page):
    page._render(_snap(connected=None, rssi_dbm=None, evm_db=None))
    assert "状态未知" in page.lbl_link.text()
    assert "RSSI — · EVM —" in page.lbl_metrics.text()
    assert page.lbl_metrics.styleSheet() == ""


def test_metrics_color_tiers(page):
    page._render(_snap(rssi_dbm=-70.0))
    assert "#1b7a43" in page.lbl_metrics.styleSheet()
    page._render(_snap(rssi_dbm=-88.0))
    assert "#b25a00" in page.lbl_metrics.styleSheet()
    page._render(_snap(rssi_dbm=-95.0))
    assert "#b3261e" in page.lbl_metrics.styleSheet()


def test_render_radio_line(page):
    page._render(_snap())
    txt = page.lbl_radio.text()
    assert "868 MHz" in txt
    assert "8 MHz" in txt
    assert "MCS10" in txt
    assert "20 dBm" in txt


def test_mcs_text_fixed_vs_auto():
    assert _mcs_text(None) == "—"
    assert _mcs_text(7) == "MCS7"
    assert _mcs_text(10) == "MCS10"
    assert _mcs_text(255) == "自适应(MCS255)"


def test_render_radio_line_shows_auto(page):
    page._render(_snap(tx_mcs=255))
    assert "自适应(MCS255)" in page.lbl_radio.text()


def test_render_degraded_and_normal(page):
    page._render(_snap(degraded=True, errors=["EVM 超阈", "丢包"][:1]))
    assert "⚠ 退化" in page.lbl_health.text()
    assert "EVM 超阈" in page.lbl_health.text()
    page._render(_snap())
    assert page.lbl_health.text() == "指标正常"


def test_render_sta_table(page):
    sta = StaInfo(
        index=0,
        mac="AA:BB:CC:DD:EE:FF",
        rssi_dbm=-71.0,
        evm_db=-33.0,
        tx_mcs=6,
        rx_mcs=7,
        tx_packets=111,
        rx_packets=222,
        lost_packets=3,
    )
    page._render(_snap(sta_count=1, sta_list=[sta]))
    assert page.sta_table.rowCount() == 1

    def cell(col):
        item = page.sta_table.item(0, col)
        return item.text() if item is not None else ""

    assert cell(1) == "AA:BB:CC:DD:EE:FF"
    assert cell(2) == "-71.0"
    assert cell(6) == "222"
    assert cell(7) == "111"
    assert cell(8) == "3"


def test_sta_table_empty_hint(page):
    page._render(_snap(role="sta", sta_list=[]))
    assert page.sta_table.rowCount() == 1
    assert "本机为 STA" in page.sta_table.item(0, 0).text()
    page._render(_snap(role="ap", sta_list=[]))
    assert "暂无从机上报" in page.sta_table.item(0, 0).text()
    sta = StaInfo(
        index=0,
        mac="AA:BB:CC:DD:EE:FF",
        rssi_dbm=-71.0,
        evm_db=-33.0,
        tx_mcs=6,
        rx_mcs=7,
        tx_packets=1,
        rx_packets=2,
        lost_packets=0,
    )
    page._render(_snap(sta_count=1, sta_list=[sta]))
    assert page.sta_table.rowCount() == 1
    assert page.sta_table.item(0, 1).text() == "AA:BB:CC:DD:EE:FF"


def test_sta_table_columns_stretch_and_tooltip(page):
    """等宽 Stretch 铺满视口 + 单元格 tooltip 兜底；指标行说明 EVM 数据来源。"""
    header = page.sta_table.horizontalHeader()
    assert header.sectionResizeMode(0) == QHeaderView.Stretch
    sta = StaInfo(
        index=1,
        mac="AA:BB:CC:DD:EE:FF",
        rssi_dbm=-71.0,
        evm_db=-33.0,
        tx_mcs=6,
        rx_mcs=7,
        tx_packets=1,
        rx_packets=2,
        lost_packets=0,
    )
    page._render(_snap(sta_count=1, sta_list=[sta]))
    assert page.sta_table.item(0, 1).toolTip() == "AA:BB:CC:DD:EE:FF"
    assert "STA_INFO" in page.lbl_metrics.toolTip()


def test_ingest_ts_line_counts_curve_points(page):
    page.curve.clear_points()
    page._ingest(_snap(ts=time.time() + 400.0))
    assert "曲线 1 点" in page.lbl_ts.text()


# --------------------------------------------------------------------- on_slot 通路
def test_on_slot_publish_tick(page, session, bridge):
    page.curve.clear_points()
    snap = _snap(ts=time.time() + 500.0, rssi_dbm=-66.0, evm_db=-28.0)
    session.bus.publish(Events.LINK_SNAPSHOT, snap)
    bridge._tick()
    pts = page.curve.points()
    assert pts[-1] == (snap.ts, -66.0, -28.0)
    assert "-66.0 dBm" in page.lbl_metrics.text()


def test_on_slot_duplicate_snapshot_adds_one_point(page, session, bridge):
    page.curve.clear_points()
    snap = _snap(ts=time.time() + 600.0)
    for _ in range(3):
        session.bus.publish(Events.LINK_SNAPSHOT, snap)
        bridge._tick()
    assert len([p for p in page.curve.points() if p[0] == snap.ts]) == 1


def test_follow_unchecked_skips_curve(page, session, bridge):
    page.curve.clear_points()
    page.chk_follow.setChecked(False)
    snap = _snap(ts=time.time() + 700.0, rssi_dbm=-64.0)
    session.bus.publish(Events.LINK_SNAPSHOT, snap)
    bridge._tick()
    assert page.curve.points() == []
    assert "-64.0 dBm" in page.lbl_metrics.text()


def test_other_topic_ignored(page, session, bridge):
    page.curve.clear_points()
    session.bus.publish(Events.RADIO_PARAMS, {"endpoint": "local", "ts": time.time()})
    bridge._tick()
    assert page.curve.points() == []


# --------------------------------------------------------------------- 动作
def test_once_success(page, session, monkeypatch):
    page.curve.clear_points()
    result = _snap(ts=time.time() + 800.0)
    monkeypatch.setattr(session.radio, "poll_once", lambda: result)
    said = []
    monkeypatch.setattr(page, "say", lambda text, *a, **k: said.append(text))
    page._on_once()
    assert any("已读取一次" in s for s in said)
    assert len(page.curve.points()) == 1


def test_once_failure_says_error(page, session, monkeypatch):
    monkeypatch.setattr(session.radio, "poll_once", lambda: None)
    said = []
    monkeypatch.setattr(page, "say", lambda text, *a, **k: said.append(text))
    page._on_once()
    assert any("读取失败" in s for s in said)


def test_start_and_stop_polling(page, session, monkeypatch):
    said = []
    monkeypatch.setattr(page, "say", lambda text, *a, **k: said.append(text))
    monkeypatch.setattr(session.link, "start_polling", lambda: True)
    page._on_start()
    assert any("已启动" in s for s in said)

    said.clear()

    def start_fail():
        return False

    monkeypatch.setattr(session.link, "start_polling", start_fail)
    page._on_start()
    assert any("失败" in s for s in said)

    said.clear()
    stopped = []
    monkeypatch.setattr(session.link, "stop_polling", lambda: stopped.append(True))
    page._on_stop()
    assert stopped == [True]
    assert any("已停止" in s for s in said)


def test_clear_button(page):
    page.curve.add(1.0, -70.0, -30.0)
    page.curve.add(2.0, -71.0, -31.0)
    page._on_clear()
    assert page.curve.points() == []
    assert "清空" in page.lbl_ts.text()


# --------------------------------------------------------------------- 回填与降级
def test_prime_history_backfill(qapp, session, bridge, monkeypatch):
    monkeypatch.setattr(
        session.radio, "history", lambda: [_snap(ts=5.0), "junk", _snap(ts=9.0)]
    )
    p = ChannelPage(session, bridge)
    try:
        pts = p.curve.points()
        assert [t[0] for t in pts] == [5.0, 9.0]
    finally:
        p.on_close()


def test_degrades_without_services(qapp, session, bridge):
    bare = SimpleNamespace(bus=session.bus, cfg=session.cfg, radio=None, link=None)
    p = ChannelPage(bare, bridge)
    try:
        assert not p.btn_once.isEnabled()
        assert not p.btn_start.isEnabled()
        assert not p.btn_stop.isEnabled()
        assert "链路服务未启用" in p.endpoint_label.text()
    finally:
        p.on_close()
