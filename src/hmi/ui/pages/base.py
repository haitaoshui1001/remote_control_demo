"""页面基类与占位页。

页面只许认识两样东西：``session``（调服务）与 ``bridge``（订阅刷新）。协议解析、
通道句柄、字节分流都不该出现在这一层——那是 services 以下的事。

异常兜底不是可选项：PyQt5 里 Python 方法（重写虚函数或槽函数）抛出未捕获异常会直接
终止进程。界面在野外跑几小时，绝不能因为某一次负载形状意外就整个崩掉，所以
``on_slot`` / ``on_batch`` / ``on_show`` 一律包在 try 里，出错记日志并在状态栏提示。
"""

from __future__ import annotations

from typing import Any

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QLabel, QVBoxLayout, QWidget

from ...core.logger import get_logger
from ..theme import font_px, head_px

__all__ = ["BasePage", "PlaceholderPage"]


class BasePage(QWidget):
    """所有功能页的基类：接好桥、备好钩子、兜住异常。"""

    page_key: str = ""
    title: str = "页面"

    def __init__(self, session: Any, bridge: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.bridge = bridge
        self.log = get_logger(f"hmi.ui.{self.page_key or 'page'}")
        bridge.slot_updated.connect(self._on_slot_updated)
        bridge.batch_ready.connect(self._on_batch_ready)

    # ------------------------------------------------------------------ 桥分发
    def _on_slot_updated(self, topic: str) -> None:
        try:
            self.on_slot(topic)
        except Exception as exc:  # noqa: BLE001 - 界面必须活着，异常只配被记下来
            self._fail("刷新", exc)

    def _on_batch_ready(self, topic: str, items: list) -> None:
        try:
            self.on_batch(topic, items)
        except Exception as exc:  # noqa: BLE001
            self._fail("流水", exc)

    def on_slot(self, topic: str) -> None:
        """合并类主题更新时调用（最新值用 ``self.bridge.latest(topic)`` 取）。"""

    def on_batch(self, topic: str, items: list) -> None:
        """流水类主题一次性给一批，按顺序，不多不少。"""

    # ------------------------------------------------------------------ 生命周期
    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        try:
            self.on_show()
        except Exception as exc:  # noqa: BLE001
            self._fail("进入页面", exc)

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        try:
            self.on_hide()
        except Exception as exc:  # noqa: BLE001
            self._fail("离开页面", exc)

    def on_show(self) -> None:
        """每次切到本页时调用；页面自己的定时器建议在这里启停。"""

    def on_hide(self) -> None:
        """每次切走本页时调用。"""

    def on_close(self) -> None:
        """关窗前统一调用一次（父窗口负责调用）。"""

    # ------------------------------------------------------------------ 小工具
    def say(self, text: str, timeout_ms: int = 6000) -> None:
        """在主窗口状态栏留一句话；页面不该弹模态框打断操作。"""
        top = self.window()
        bar = top.statusBar() if hasattr(top, "statusBar") else None
        if bar is not None:
            bar.showMessage(text, timeout_ms)

    def _fail(self, where: str, exc: Exception) -> None:
        self.log.exception("%s 失败：%s", where, exc)
        self.say(f"{self.title}：{where}出错（已记录日志）——{exc}", 8000)

    def service(self, name: str) -> Any:
        """取服务实例，服务可能没装配（如 ``--no-video``），取不到返回 None。"""
        return getattr(self.session, name, None)


class PlaceholderPage(BasePage):
    """尚未实现的功能页占位。

    步骤 6 是按"骨架 → 视频 → 控制台/数据 → 配置/信道/日志"分批交付的，
    占位页让主窗口从一开始就能完整导航，也方便步骤 7 的 offscreen 冒烟测试
    在只装了部分页面时照样跑得通。
    """

    def __init__(
        self,
        page_key: str,
        title: str,
        note: str,
        session: Any,
        bridge: Any,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(session, bridge, parent)
        self.page_key = page_key
        self.title = title
        self.note = note
        box = QVBoxLayout(self)
        box.setAlignment(Qt.AlignCenter)
        head = QLabel(title)
        head.setAlignment(Qt.AlignCenter)
        head.setStyleSheet(f"font-size:{head_px()}px; color:#2f3b4c;")
        hint = QLabel(note or "本页尚未实现")
        hint.setAlignment(Qt.AlignCenter)
        hint.setWordWrap(True)
        hint.setStyleSheet(f"font-size:{font_px()}px; color:#8a94a3;")
        box.addWidget(head)
        box.addWidget(hint)
