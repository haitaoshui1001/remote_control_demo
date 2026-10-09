"""视频页运动控制 UI 测试（offscreen）。

沿用 test_ui_video_page 的确定性手法：不靠 QTimer 自然触发，桥更新一律
``bus.publish`` 后手动 ``bridge._tick()``；键盘事件直接构造 ``QKeyEvent``
喂给页面的 keyPressEvent/keyReleaseEvent，不进事件循环。重点盯四件事：
方向盘按钮/键盘到服务的连线、autorep 过滤、MOTION_STATE 回显、失焦急停
（含空发动作守卫与 stop_on_focus_loss=False 的旁路）与降级路径。
"""

from __future__ import annotations

import pytest
from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtGui import QKeyEvent

from hmi.core.events import Events
from hmi.services.session import build_session
from hmi.ui.bridge import UiBridge
from hmi.ui.pages.video_page import VideoPage


def _key(ev_type, key: int, autorep: bool = False) -> QKeyEvent:
    return QKeyEvent(ev_type, key, Qt.NoModifier, "", autorep=autorep)


def _make_page(session):
    bridge = UiBridge(session.bus, interval_ms=200)
    bridge.start()
    return VideoPage(session, bridge), bridge


@pytest.fixture()
def session():
    return build_session(None, mock=True)


@pytest.fixture()
def page(qapp, session):
    p, bridge = _make_page(session)
    yield p
    bridge.stop()


def test_pad_present_with_four_directions(page):
    pad = page.motion_pad
    assert pad.isEnabled()
    assert sorted(pad._btns) == ["down", "left", "right", "up"]
    assert all(b.focusPolicy() == Qt.NoFocus for b in pad._btns.values())


def test_button_press_release_roundtrip(page):
    motion = page.service("motion")
    pad = page.motion_pad
    pad._on_btn_press("up")
    st = motion.snapshot()
    assert st["held"] == ["up"]
    assert st["target_vx"] == pytest.approx(1.0)
    assert "目标 +1.00/+0.00" in pad.readout.text()
    assert pad.center_label.text() == "行"
    assert "background" in pad._btns["up"].styleSheet()

    pad._on_btn_release("up")
    st = motion.snapshot()
    assert st["held"] == []
    assert st["target_vx"] == pytest.approx(0.0)
    assert "background" not in pad._btns["up"].styleSheet()
    assert "font-size" in pad._btns["up"].styleSheet()


def test_disabled_pad_does_not_emit_press(page):
    motion = page.service("motion")
    page.motion_pad.setEnabled(False)
    page.motion_pad._on_btn_press("up")
    assert motion.snapshot()["held"] == []


def test_keyboard_up_press_and_release(page):
    motion = page.service("motion")
    page.keyPressEvent(_key(QEvent.KeyPress, Qt.Key_Up))
    assert motion.snapshot()["held"] == ["up"]
    page.keyReleaseEvent(_key(QEvent.KeyRelease, Qt.Key_Up))
    assert motion.snapshot()["held"] == []


def test_keyboard_autorep_press_and_release_both_filtered(page):
    motion = page.service("motion")
    page.keyPressEvent(_key(QEvent.KeyPress, Qt.Key_Up))
    page.keyPressEvent(_key(QEvent.KeyPress, Qt.Key_Up, autorep=True))
    st = motion.snapshot()
    assert st["held"] == ["up"]
    assert st["target_vx"] == pytest.approx(1.0)
    # 中间那次自动重复的"释放"绝不能当成松手（否则按住会"递增又回零"）
    page.keyReleaseEvent(_key(QEvent.KeyRelease, Qt.Key_Up, autorep=True))
    assert motion.snapshot()["held"] == ["up"]
    assert motion.snapshot()["target_vx"] == pytest.approx(1.0)
    # 真正松手那一次（非自动重复）才释放方向
    page.keyReleaseEvent(_key(QEvent.KeyRelease, Qt.Key_Up))
    assert motion.snapshot()["held"] == []


def test_keyboard_hold_cycle_keeps_direction_until_real_release(page):
    # 复现"一直按住前进箭头却递增又回零"：Windows/X11 长按会发成
    # press → (release_auto, press_auto) × N → release（真实松手）。
    # 修复后整个按住期间 held 必须一直是 up，只有最后真实松手才清零。
    motion = page.service("motion")
    page.keyPressEvent(_key(QEvent.KeyPress, Qt.Key_Up))
    for _ in range(5):
        page.keyReleaseEvent(_key(QEvent.KeyRelease, Qt.Key_Up, autorep=True))
        page.keyPressEvent(_key(QEvent.KeyPress, Qt.Key_Up, autorep=True))
        assert motion.snapshot()["held"] == ["up"]
    page.keyReleaseEvent(_key(QEvent.KeyRelease, Qt.Key_Up))
    assert motion.snapshot()["held"] == []


def test_keyboard_left_maps_to_positive_vr(page):
    motion = page.service("motion")
    page.keyPressEvent(_key(QEvent.KeyPress, Qt.Key_Left))
    st = motion.snapshot()
    assert st["target_vr"] == pytest.approx(0.5)
    assert "目标 +0.00/+0.50" in page.motion_pad.readout.text()


def test_keyboard_diagonal_combo(page):
    motion = page.service("motion")
    page.keyPressEvent(_key(QEvent.KeyPress, Qt.Key_Up))
    page.keyPressEvent(_key(QEvent.KeyPress, Qt.Key_Right))
    st = motion.snapshot()
    assert st["held"] == ["right", "up"]
    assert st["target_vx"] == pytest.approx(1.0)
    assert st["target_vr"] == pytest.approx(-0.5)


def test_motion_state_via_bridge_refreshes_readout(page, session):
    session.bus.publish(
        Events.MOTION_STATE,
        {
            "vx": 0.2,
            "vr": 0.0,
            "target_vx": 1.0,
            "target_vr": 0.0,
            "moving": True,
            "held": ["up"],
            "active": True,
            "sent_frames": 3,
        },
    )
    page.bridge._tick()
    text = page.motion_pad.readout.text()
    assert "vx +0.20 vr +0.00" in text
    assert page.motion_pad.center_label.text() == "行"


def test_on_show_reflects_current_service_state(page, session):
    session.start()
    try:
        motion = page.service("motion")
        motion.press("up")
        page.on_show()
        assert "目标 +1.00/+0.00" in page.motion_pad.readout.text()
        motion.release_all()
    finally:
        session.stop()


def test_on_hide_estops_when_moving(qapp, session):
    # 通道要真收发才谈得上"补发停止帧"，所以这条起真 session（mock 通道）
    session.start()
    page, bridge = _make_page(session)
    try:
        motion = page.service("motion")
        motion.press("up")
        motion.advance(0.1)
        st = motion.snapshot()
        assert st["vx"] == pytest.approx(0.05)
        sent0 = st["sent_frames"]

        page.on_hide()
        st = motion.snapshot()
        assert st["held"] == []
        assert st["vx"] == pytest.approx(0.0)
        assert st["sent_frames"] > sent0, "滑行中的急停必须补发停止帧"
    finally:
        bridge.stop()
        session.stop()


def test_on_hide_when_idle_sends_nothing(qapp, session):
    session.start()
    page, bridge = _make_page(session)
    try:
        motion = page.service("motion")
        sent0 = motion.snapshot()["sent_frames"]
        page.on_hide()
        assert motion.snapshot()["sent_frames"] == sent0, "空闲切页不该发帧"
    finally:
        bridge.stop()
        session.stop()


def test_focus_loss_estop_disabled_keeps_held(qapp):
    session = build_session(None, mock=True, **{"motion.stop_on_focus_loss": False})
    page, bridge = _make_page(session)
    try:
        motion = page.service("motion")
        page.keyPressEvent(_key(QEvent.KeyPress, Qt.Key_Up))
        page.on_hide()
        assert motion.snapshot()["held"] == ["up"]
        motion.release_all()
    finally:
        bridge.stop()


def test_missing_motion_service_degrades(qapp):
    session = build_session(None, mock=True, motion=False)
    page, bridge = _make_page(session)
    try:
        assert not page.motion_pad.isEnabled()
        assert page.motion_pad.readout.text() == "未装配运动服务"
        page.keyPressEvent(_key(QEvent.KeyPress, Qt.Key_Up))
        page.on_hide()
    finally:
        bridge.stop()


def test_motion_disabled_by_config(qapp):
    session = build_session(None, mock=True, **{"motion.enabled": False})
    page, bridge = _make_page(session)
    try:
        assert not page.motion_pad.isEnabled()
        assert page.motion_pad.readout.text() == "运动控制已禁用"
    finally:
        bridge.stop()


# ------------------------------------------------------------------ vx/vr 限速设置行
def test_direction_buttons_have_no_tooltip(page):
    """肩头悬停不再弹提示：方向含义按钮上已写明，气泡只会挡操作。"""
    assert all(btn.toolTip() == "" for btn in page.motion_pad._btns.values())


def test_limit_row_defaults_follow_cfg(page, session):
    m = session.cfg.motion
    assert page.motion_pad.spin_vx.value() == pytest.approx(m.max_linear_speed)
    assert page.motion_pad.spin_vr.value() == pytest.approx(m.max_angular_speed)


def test_apply_limits_updates_cfg_saves_and_next_press(page, session, monkeypatch):
    saved: list[int] = []
    monkeypatch.setattr(session.cfg, "save", lambda: saved.append(1))
    motion = page.service("motion")
    page.motion_pad.spin_vx.setValue(2.5)
    page.motion_pad.spin_vr.setValue(1.2)
    page.motion_pad.btn_limits.click()
    assert session.cfg.motion.max_linear_speed == pytest.approx(2.5)
    assert session.cfg.motion.max_angular_speed == pytest.approx(1.2)
    assert saved == [1], "应用限速必须写回 app.yaml"
    page.motion_pad._on_btn_press("up")
    assert motion.snapshot()["target_vx"] == pytest.approx(2.5), "服务动态读 cfg，下一次按键即生效"


def test_apply_limits_survives_save_failure(page, session, monkeypatch):
    def boom():
        raise OSError("app.yaml 只读")

    monkeypatch.setattr(session.cfg, "save", boom)
    page.motion_pad.spin_vx.setValue(0.5)
    page.motion_pad.btn_limits.click()
    assert session.cfg.motion.max_linear_speed == pytest.approx(0.5), "保存失败也要本次会话生效"
