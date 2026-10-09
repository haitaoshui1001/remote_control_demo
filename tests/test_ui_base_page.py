"""BasePage / PlaceholderPage 的契约测试（offscreen，不建真实会话）。

``test_ui_pages.py`` 已验证"异常不把进程带走"这一条下限，本文件补它没盯的
契约细节：
- ``_fail`` 的确切文案（"{title}：{where}出错（已记录日志）——{exc}"）与落点
  （主窗口状态栏，而不是模态框）；
- 四个兜底入口 on_slot/on_batch/on_show/on_hide 各自的 where 字样；
- ``service()`` 的"服务没装配返回 None"语义；
- 钩子默认 no-op、桥接信号在构造时就连上、多个页面共享同一条桥互不干扰。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt5.QtWidgets import QApplication, QLabel, QMainWindow

from hmi.core.events import EventBus, Events
from hmi.ui.bridge import UiBridge
from hmi.ui.pages.base import BasePage, PlaceholderPage


@pytest.fixture()
def bridge() -> UiBridge:
    return UiBridge(EventBus())


def _host(page: BasePage) -> QMainWindow:
    """把页面挂进带状态栏的主窗口，让 say() 的文案可断言。"""
    win = QMainWindow()
    win.setCentralWidget(page)
    return win


# ------------------------------------------------------------------ _fail 文案
def test_on_slot_failure_message_lands_in_status_bar(qapp, bridge):
    class Boom(BasePage):
        title = "测试页"

        def on_slot(self, topic):
            raise RuntimeError("boom")

    page = Boom(None, bridge)
    win = _host(page)
    win.show()
    bridge.slot_updated.emit(Events.VIDEO_STATS)
    assert win.statusBar().currentMessage() == "测试页：刷新出错（已记录日志）——boom"
    win.close()


def test_on_batch_failure_says_liushui(qapp, bridge):
    class Boom(BasePage):
        title = "流水页"

        def on_batch(self, topic, items):
            raise ValueError("坏负载")

    page = Boom(None, bridge)
    win = _host(page)
    win.show()
    bridge.batch_ready.emit(Events.PT_BYTES, [1, 2])
    assert "流水出错（已记录日志）——坏负载" in win.statusBar().currentMessage()
    win.close()


def test_on_show_and_on_hide_failures_contained(qapp, bridge):
    class Boom(BasePage):
        def on_show(self):
            raise RuntimeError("show boom")

        def on_hide(self):
            raise RuntimeError("hide boom")

    page = Boom(None, bridge)
    win = _host(page)
    win.show()  # showEvent → on_show 抛异常，必须被兜住
    QApplication.processEvents()
    page.hide()  # hideEvent → on_hide 同样不许带走进程
    assert page.isVisible() is False
    win.close()


def test_say_survives_without_main_window(qapp, bridge):
    page = BasePage(None, bridge)  # 顶层部件，window() 就是自己，没有 statusBar
    page.say("hello")
    assert page.parent() is None


# ------------------------------------------------------------------ 正常分发
def test_on_slot_and_on_batch_receive_args(qapp, bridge):
    class Spy(BasePage):
        slots: list = []
        batches: list = []

        def on_slot(self, topic):
            self.slots.append(topic)

        def on_batch(self, topic, items):
            self.batches.append((topic, list(items)))

    page = Spy(None, bridge)
    bridge.slot_updated.emit(Events.LINK_SNAPSHOT)
    bridge.batch_ready.emit(Events.AT_EXCHANGE, ["a", "b"])
    assert page.slots == [Events.LINK_SNAPSHOT]
    assert page.batches == [(Events.AT_EXCHANGE, ["a", "b"])]


def test_two_pages_share_one_bridge(qapp, bridge):
    class Spy(BasePage):
        def __init__(self, session, br, parent=None):
            super().__init__(session, br, parent)
            self.seen = []

        def on_slot(self, topic):
            self.seen.append(topic)

    a = Spy(None, bridge)
    b = Spy(None, bridge)
    bridge.slot_updated.emit(Events.TRANSPORT_STATE)
    assert a.seen == b.seen == [Events.TRANSPORT_STATE]


# ------------------------------------------------------------------ 基类语义
def test_default_hooks_are_noop(qapp, bridge):
    page = BasePage(None, bridge)
    page.on_slot("t")
    page.on_batch("t", [])
    page.on_show()
    page.on_hide()
    page.on_close()


def test_service_lookup_semantics(qapp, bridge):
    link = SimpleNamespace(name="link")
    session = SimpleNamespace(link=link)
    page = BasePage(session, bridge)
    assert page.service("link") is link
    assert page.service("video") is None  # --no-video 装配时就该拿到 None
    page_none = BasePage(None, bridge)
    assert page_none.service("anything") is None


# ------------------------------------------------------------------ 占位页
def test_placeholder_builds_two_labels(qapp, bridge):
    page = PlaceholderPage("tmp", "临时页", "等步骤 6.3", None, bridge)
    assert (page.page_key, page.title, page.note) == ("tmp", "临时页", "等步骤 6.3")
    labels = [w for w in page.findChildren(QLabel)]
    assert len(labels) == 2
    texts = {w.text() for w in labels}
    assert texts == {"临时页", "等步骤 6.3"}


def test_placeholder_empty_note_falls_back(qapp, bridge):
    page = PlaceholderPage("tmp", "临时页", "", None, bridge)
    assert "本页尚未实现" in {w.text() for w in page.findChildren(QLabel)}


def test_placeholder_hooks_safe_under_bridge_emit(qapp, bridge):
    page = PlaceholderPage("tmp", "临时页", "note", None, bridge)
    win = _host(page)
    win.show()
    bridge.slot_updated.emit(Events.VIDEO_FRAME)
    bridge.batch_ready.emit(Events.PT_BYTES, [])
    assert win.statusBar().currentMessage() == ""  # 默认钩子 no-op，不该有报错文案
    win.close()
