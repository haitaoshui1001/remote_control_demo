"""主窗口的离屏冒烟测试：全 mock 会话 + 真桥 + 真窗口。

不装视频、不装落盘（两个灯应老实说"未装配"），四盏灯只验语义不验排版。
模态框一律 monkeypatch 掉——offscreen 下真弹框会把测试挂死。

调 ``bridge._tick()`` 与直接 ``closeEvent()`` 都是有意为之：前者等价于"主线程走了
一次节拍"，后者跳过 ``close()`` 的显示状态依赖，断言才确定。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt5.QtGui import QCloseEvent
from PyQt5.QtWidgets import QMessageBox

from hmi.core.events import EventBus, Events, get_bus, set_bus
from hmi.services.session import build_session
from hmi.ui.bridge import UiBridge
from hmi.ui.main_window import Lamp, MainWindow
from hmi.ui.pages import BasePage, page_keys


@pytest.fixture()
def session():
    """在私有总线上装配真会话：mock 信道打开、轮询在跑，退出时干净停掉。"""
    prev = get_bus()
    set_bus(EventBus())
    s = build_session(None, mock=True, video=False, record=False)
    try:
        result = s.start()
        assert result["link"], f"mock 信道打不开：{result['detail']}"
        yield s
    finally:
        s.stop()
        set_bus(prev)


@pytest.fixture()
def window(session):
    bridge = UiBridge(session.bus)
    bridge.start()
    win = MainWindow(session, bridge)
    try:
        yield win
    finally:
        bridge.stop()


def test_navigation_has_all_pages(window):
    assert tuple(window.pages) == page_keys()
    for key, page in window.pages.items():
        assert isinstance(page, BasePage)
        assert page.page_key == key
    assert window.nav.count() == len(page_keys())


def test_title_states_mock_mode(window):
    assert "仿真模式" in window.windowTitle()


def test_lamps_report_assembly_and_channel(window):
    window.refresh_status()
    assert window.lamp_channel.level == "ok"
    assert window.lamp_poll.level in ("ok", "warn")
    assert window.lamp_video.level == "idle"
    assert window.lamp_video.detail == "未装配"
    assert window.lamp_record.detail == "未装配"


def test_bus_label_refreshes_via_bridge(window):
    window.bus_label.setText("")
    window.session.bus.publish(Events.LINK_SNAPSHOT, {"open": True})
    window.bridge._tick()
    assert "总线" in window.bus_label.text()


def test_stream_topics_do_not_repaint_lamps(window):
    """AT 流水等 1 秒节拍，不该每条都触发一次全量状态刷新。"""
    window.bus_label.setText("")
    window.session.bus.publish(Events.AT_EXCHANGE, {"dir": "tx", "text": "AT"})
    window.bridge._tick()
    assert window.bus_label.text() == ""


def test_page_selection_and_bounds(window):
    window.select_page("log")
    assert window.nav.currentRow() == 5
    assert window.current_page is window.pages["log"]
    window.select_index(99)
    assert window.nav.currentRow() == 5
    window.select_page("nope")
    assert window.nav.currentRow() == 5


def test_lamp_rejects_unknown_level(qapp):
    lamp = Lamp("测试")
    lamp.set_state("purple", "乱写的级别")
    assert lamp.level == "idle"
    assert lamp.detail == "乱写的级别"
    lamp.set_state("rec", "录像中")
    assert lamp.level == "rec"


def test_close_accepts_without_dialog_and_closes_pages(window, monkeypatch):
    """只有把退出确认关掉才该直接放行（出厂配置默认是要确认的）。"""
    window.session.cfg.ui.confirm_exit = False
    closed: list[str] = []
    for page in window.pages.values():
        page.on_close = lambda p=page: closed.append(p.page_key)
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: pytest.fail("不该弹框"))
    )
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted()
    assert set(closed) == set(page_keys())


@pytest.mark.parametrize("answer, accepted", [(QMessageBox.No, False), (QMessageBox.Yes, True)])
def test_recording_exit_guard(window, monkeypatch, answer, accepted):
    window.session.record = SimpleNamespace(
        recording=True, rows=5, snapshots=2, dropped=0, stop=lambda: None
    )
    asked: list[str] = []

    def fake_question(parent, title, text, buttons, default_button):
        asked.append(title)
        return answer

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted() is accepted
    assert asked == ["正在录像"]


def test_confirm_exit_setting_is_honored(window, monkeypatch):
    asked: list[str] = []

    def fake_question(parent, title, text, buttons, default_button):
        asked.append(title)
        return QMessageBox.No

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))
    assert window.session.cfg.ui.confirm_exit is True, "出厂默认退出要确认"

    event = QCloseEvent()
    window.closeEvent(event)
    assert asked == ["退出"]
    assert not event.isAccepted()

    window.session.cfg.ui.confirm_exit = False
    window.closeEvent(QCloseEvent())
    assert asked == ["退出"], "关掉确认之后不该再弹一次"
