"""视频页冒烟测试（offscreen）。

刻意不靠 QTimer 自然触发：帧一律先 publish 再手动 ``_poll_frames()``，指标
先 bus.publish 再手动 ``bridge._tick()``——把异步节奏压成确定性调用，断言的
就是"转换与分流"本身，而不是调度时机。重点盯两件事：RGB888→QImage 的缓冲
脱链（``QPixmap.fromImage`` 在帧数组存活期内同步深拷贝，省掉了旧的
``.copy()`` 双拷贝），以及服务未装配时的降级路径。
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from PyQt5.QtGui import QFont, QFontMetrics, QImage, QPainter

from hmi.core.config import TelemetryCfg
from hmi.core.events import Events
from hmi.core.models import FrameMeta, LinkSnapshot, VideoStats
from hmi.protocol.telemetry_frames import TelemetrySample
from hmi.services.session import build_session
from hmi.ui.bridge import UiBridge
from hmi.ui.pages.video_page import VideoPage
from hmi.ui.widgets.camera_dialog import CameraConfigDialog
from hmi.ui.widgets.telemetry_bar import (
    TelemetryBar,
    _legend_layout,
    _MiniCurve,
    _scale_range,
    _series_scale,
)


def _solid(color: tuple[int, int, int]) -> np.ndarray:
    arr = np.zeros((576, 704, 3), dtype="uint8")
    arr[:] = color
    return arr


@pytest.fixture()
def session():
    return build_session(None, mock=True)


@pytest.fixture()
def page(qapp, session):
    bridge = UiBridge(session.bus, interval_ms=200)
    bridge.start()
    yield VideoPage(session, bridge)
    bridge.stop()


def test_frame_poll_releases_one_frame_per_tick(page):
    channel = page.service("video").frames
    channel.publish(_solid((10, 20, 200)), FrameMeta(width=704, height=576, codec="H.264"))
    channel.publish(_solid((200, 30, 30)), FrameMeta(width=704, height=576, codec="H.264"))

    page._poll_frames()
    first = page._full_pm.toImage().pixelColor(5, 5)
    assert (first.red(), first.green(), first.blue()) == (10, 20, 200), "一次轮询只放一帧"
    assert page._frames_shown == 1

    page._poll_frames()
    pm = page._full_pm
    assert (pm.width(), pm.height()) == (704, 576)
    color = pm.toImage().pixelColor(5, 5)
    assert (color.red(), color.green(), color.blue()) == (200, 30, 30), "第二个节拍放次帧"
    assert channel.pull_next() is None, "两帧已按到达顺序放完"
    assert page.video_label.pixmap() is not None and not page.video_label.pixmap().isNull()


def test_show_hide_toggles_frame_timer(page):
    page.on_show()
    assert page._timer.isActive()
    page.on_hide()
    assert not page._timer.isActive()


def test_stream_toggle_flips_config_and_button(page):
    assert page.service("video").stream_index == 1  # 出厂默认子码流（带宽优先）
    page._on_stream_clicked()
    assert page.service("video").cfg.video.stream_index == 0
    assert page.btn_stream.text() == "切换子码流"
    page._on_stream_clicked()
    assert page.service("video").cfg.video.stream_index == 1
    assert page.btn_stream.text() == "切换主码流"


def test_source_button_starts_and_stops_mock_stream(qapp, session):
    bridge = UiBridge(session.bus)
    bridge.start()
    page = VideoPage(session, bridge)
    try:
        page._on_source_clicked()
        assert page.service("video").running
        assert page.btn_source.text() == "停止视频"
        page._on_source_clicked()
        assert not page.service("video").running
        assert page.btn_source.text() == "启动视频"
    finally:
        session.stop()
        bridge.stop()


def test_snapshot_routes_through_record(qapp, session, monkeypatch):
    calls: list[str] = []

    def fake_capture(path=None, *, label=""):
        calls.append("capture")
        return {"ok": True, "path": "fake.png"}

    monkeypatch.setattr(session.record, "capture", fake_capture)
    bridge = UiBridge(session.bus)
    page = VideoPage(session, bridge)
    page._on_snapshot_clicked()
    assert calls == ["capture"]
    bridge.stop()


def test_record_toggle_reflects_recording_state(qapp, session, monkeypatch):
    state = {"recording": False, "started": 0, "stopped": 0}
    monkeypatch.setattr(
        type(session.record), "recording", property(lambda self: state["recording"])
    )

    def fake_start():
        state["started"] += 1
        state["recording"] = True
        return {"ok": True, "path": "clip.mp4", "detail": "开始"}

    def fake_stop():
        state["stopped"] += 1
        state["recording"] = False
        return {"ok": True, "path": "clip.mp4", "frames": 42}

    monkeypatch.setattr(session.record, "start_video", fake_start)
    monkeypatch.setattr(session.record, "stop_video", fake_stop)
    bridge = UiBridge(session.bus)
    page = VideoPage(session, bridge)

    page._on_record_clicked()
    assert page.btn_record.text() == "停止录像"
    assert "background" in page.btn_record.styleSheet()
    page._on_record_clicked()
    assert page.btn_record.text() == "录像"
    assert (state["started"], state["stopped"]) == (1, 1)
    bridge.stop()


def test_info_bar_renders_stats_and_link(page):
    bus = page.session.bus
    bus.publish(
        Events.VIDEO_STATS,
        VideoStats(
            connected=True,
            resolution="704x576",
            codec="H.264",
            fps_decode=24.3,
            frames_dropped=2,
            reconnects=1,
        ),
    )
    bus.publish(
        Events.LINK_SNAPSHOT,
        LinkSnapshot(connected=True, rssi_dbm=-70.0, evm_db=-18.0, freq_mhz=868.5),
    )
    page.bridge._tick()

    text = page.info_label.text()
    assert "704x576" in text and "24.3fps" in text and "丢帧2" in text
    assert "-70dBm" in text and "-18.0dB" in text and "868.5MHz" in text
    assert "链路正常" in text


def test_info_bar_shows_merged_only_when_nonzero(page):
    bus = page.session.bus
    base = dict(
        connected=True, resolution="800x448", codec="hevc", fps_decode=20.0, reconnects=0
    )
    bus.publish(Events.VIDEO_STATS, VideoStats(frames_dropped=1, frames_merged=38, **base))
    page.bridge._tick()
    text = page.info_label.text()
    assert "丢帧1" in text and "合并38" in text

    bus.publish(Events.VIDEO_STATS, VideoStats(frames_dropped=1, frames_merged=0, **base))
    page.bridge._tick()
    assert "合并" not in page.info_label.text()


def test_page_survives_without_services(qapp):
    session = build_session(None, mock=True, video=False, record=False)
    bridge = UiBridge(session.bus)
    page = VideoPage(session, bridge)

    page._poll_frames()
    page._on_source_clicked()
    page._on_snapshot_clicked()
    page._on_record_clicked()
    page._on_stream_clicked()
    page._on_transport_selected(1)
    page.on_show()
    page.on_slot(Events.VIDEO_STATS)

    assert not page.btn_source.isEnabled()
    assert not page.btn_snapshot.isEnabled()
    assert not page.btn_record.isEnabled()
    assert not page.btn_stream.isEnabled()
    assert not page.combo_transport.isEnabled()
    assert "未装配视频服务" in page.video_label.text()
    assert "等待指标" in page.info_label.text()
    bridge.stop()


def test_transport_combo_switches_and_persists(page, session, tmp_path):
    combo = page.combo_transport
    assert combo.currentData() == str(session.cfg.video.transport or "tcp").lower(), (
        "控件初值必须和生效配置一致"
    )
    session.cfg.meta.path = str(tmp_path / "app.yaml")
    page._on_transport_selected(combo.findData("udp"))
    assert session.cfg.video.transport == "udp"
    text = (tmp_path / "app.yaml").read_text(encoding="utf-8")
    assert "transport: udp" in text, "工具条切换要能落盘，重启软件后仍是 UDP"


def test_combo_reflects_saved_transport(qapp, session):
    session.cfg.video.transport = "udp"
    bridge = UiBridge(session.bus)
    page = VideoPage(session, bridge)
    assert page.combo_transport.currentData() == "udp", "配置里存的传输方式要还原到控件"
    bridge.stop()


def test_legend_layout_uses_real_text_width(qapp):
    """图例按实测字宽排布——旧版按 8px/字符估宽，中文图例会叠在一起。"""
    fm = QFontMetrics(QFont("Microsoft YaHei", 9))
    texts = ["电压 23.2V", "速度 0.00m/s", "电流 1.2A"]
    xs = _legend_layout(fm, texts, 10)
    assert xs[0] == 10
    for i in range(1, len(texts)):
        assert xs[i] >= xs[i - 1] + fm.horizontalAdvance(texts[i - 1]) + 14


# ------------------------------------------------------ 遥测曲线：电压+x向速度双纵轴

def test_scale_range_pads_span_and_opens_constant():
    lo, hi = _scale_range([22.0, 26.0])
    assert lo < 22.0 and hi > 26.0
    assert hi - lo == pytest.approx((26.0 - 22.0) * 1.16)
    lo, hi = _scale_range([3.0, 3.0])
    assert lo < 3.0 < hi, "常量序列要张开量程，不能除零"


def test_scale_range_min_span_opens_symmetrically():
    lo, hi = _scale_range([24.30, 24.32], min_span=0.4)
    assert hi - lo == pytest.approx(0.4)
    assert (lo + hi) / 2 == pytest.approx(24.31), "补量程要绕中点，不能偏一条"


def test_voltage_axis_min_division_vx_unbounded():
    lo, hi = _series_scale("voltage", [24.30, 24.32])
    assert (hi - lo) / 2 >= 0.2 - 1e-9, "电压每格不得低于 0.2V，否则 1 位小数刻度三档同数"
    lo, hi = _series_scale("vx", [0.5, 0.52])
    assert hi - lo < 0.4, "vx 轴 2 位小数可分辨，沿用细量程不受电压下限影响"


def test_default_curve_series_is_voltage_and_vx():
    """默认曲线显示电压+x向速度：Wheeltec 帧没有电流字段，右轴换成 vx。"""
    assert TelemetryCfg().curve.series == ["voltage", "vx"]


def test_bar_falls_back_to_voltage_vx_when_series_empty(qapp):
    cfg = TelemetryCfg()
    cfg.curve.series = []
    bar = TelemetryBar(cfg)
    assert bar.curve._series == ["voltage", "vx"]


def test_mini_curve_paint_dual_axes(qapp):
    """电压刻度在左、x向速度刻度在右：双轴/单轴/空态三条分支都要画得出来。"""
    curve = _MiniCurve(60.0, ["voltage", "vx"])
    base = time.time()
    for i in range(6):
        curve.add(TelemetrySample(
            ts=base - 30 + i * 5, voltage=22.5 + 0.2 * i, vx=0.2 * i,
        ))
    image = QImage(360, 120, QImage.Format_ARGB32)
    painter = QPainter(image)
    curve._paint(painter, 360, 120)
    painter.end()
    assert curve.enabled

    single = _MiniCurve(60.0, ["voltage"])
    single.add(TelemetrySample(ts=base, voltage=23.0))
    painter = QPainter(image)
    single._paint(painter, 360, 120)
    painter.end()

    empty = _MiniCurve(60.0, ["voltage", "vx"])
    painter = QPainter(image)
    empty._paint(painter, 360, 120)
    painter.end()


def test_camera_button_opens_dialog_once(page, session, monkeypatch):
    calls: list[int] = []

    def fake_load_async(idx: int) -> bool:
        calls.append(int(idx))
        return True

    monkeypatch.setattr(session.camera, "load_async", fake_load_async)
    page.btn_camera.click()
    dlg = page._camera_dialog
    assert isinstance(dlg, CameraConfigDialog)
    assert calls == [session.cfg.video.stream_index], "首次打开应自动读当前码流档"
    page.btn_camera.click()
    assert page._camera_dialog is dlg, "按钮复用同一个对话框"
    assert len(calls) == 1, "在途期间不得重复自动读"


def test_camera_button_degrades_without_service(page, session, monkeypatch):
    monkeypatch.setattr(session, "camera", None)
    page.btn_camera.click()
    assert page._camera_dialog is None, "没有服务时不许凭空造对话框"
