"""日志页：AT 交互、告警与运行日志三条流水的统一视图。

三条流水（``AT_EXCHANGE`` / ``ALARM`` / ``LOG``）都是合并外的 STREAM 主题，
桥按批投递、不多不少，所以本页只实现 ``on_batch``：按 topic 分流格式化后
追加到只读文本视图，用 ``setMaximumBlockCount`` 硬截断防内存膨胀。

过滤规则：来源复选框管三条流水各自开关；级别下拉只作用于告警和运行日志
（AT 是操作反馈，失败必须可见，不受级别过滤）。计数显示"收到"条数而非
"显示"条数——被过滤掉的也是发生过的事件。

会话录制的文件路径从 ``RecordService.paths()`` 取，record 可能没装配
（``--no-record``），此时按钮禁用即可，日志视图本身不依赖任何服务。
"""

from __future__ import annotations

import html
import time

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QTextCursor
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.events import Events
from ...core.models import AlarmEvent, AtExchange
from ..theme import mono_font
from .base import BasePage

__all__ = ["LogPage"]

_MAX_BLOCKS = 4000

_C_OK = QColor("#1b7a43").name()
_C_BAD = QColor("#b3261e").name()
_C_WARN = QColor("#b25a00").name()
_C_DIM = QColor("#6b7482").name()
_C_TEXT = QColor("#2f3b4c").name()

_AT_SYMBOL = {
    "exec": "▶",
    "query": "?",
    "set": "✎",
    "action": "⚡",
    "apply": "⇓",
    "unsolicited": "~",
}
_LOG_COLORS = {
    "DEBUG": _C_DIM,
    "INFO": _C_TEXT,
    "WARNING": _C_WARN,
    "ERROR": _C_BAD,
}
_ALARM_COLORS = {"info": _C_DIM, "warn": _C_WARN, "crit": _C_BAD}
_WARN_LEVELS = {"WARNING", "ERROR", "warn", "crit"}
_ERR_LEVELS = {"ERROR", "crit"}


def _stamp(ts: object) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError):
        return "--:--:--"


def _esc(text: object) -> str:
    return html.escape(str(text), quote=False)


def _span(color: str, body: str) -> str:
    return f"<span style='color:{color};'>{body}</span>"


class LogPage(BasePage):
    """日志页：三条流水 + 过滤 + 录制文件路径。"""

    page_key = "log"
    title = "日志"

    def __init__(self, session, bridge, parent: QWidget | None = None) -> None:
        super().__init__(session, bridge, parent)
        self._counts = {"at": 0, "alarm": 0, "log": 0}
        self._build_widgets()

    # ------------------------------------------------------------------ UI 搭建
    def _build_widgets(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.chk_at = QCheckBox("AT 交互")
        self.chk_alarm = QCheckBox("告警")
        self.chk_log = QCheckBox("运行日志")
        for c in (self.chk_at, self.chk_alarm, self.chk_log):
            c.setChecked(True)
            bar.addWidget(c)
        bar.addWidget(QLabel("级别"))
        self.cmb_level = QComboBox()
        self.cmb_level.addItems(["全部", "警告及以上", "仅错误"])
        self.cmb_level.setToolTip("级别过滤只作用于告警与运行日志；AT 交互始终可见")
        bar.addWidget(self.cmb_level)
        self.btn_clear = QPushButton("清空视图")
        self.btn_paths = QPushButton("录制文件路径")
        bar.addWidget(self.btn_clear)
        bar.addWidget(self.btn_paths)
        self.chk_autoscroll = QCheckBox("自动滚到底")
        self.chk_autoscroll.setChecked(True)
        bar.addWidget(self.chk_autoscroll)
        bar.addStretch(1)
        self.lbl_counts = QLabel("")
        self.lbl_counts.setStyleSheet(f"color:{_C_DIM};")
        bar.addWidget(self.lbl_counts)
        root.addLayout(bar)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(mono_font())
        self.view.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.view.setMaximumBlockCount(_MAX_BLOCKS)
        self.view.setPlaceholderText("等待日志流水……（AT 交互 / 告警 / 运行日志会出现在这里）")
        root.addWidget(self.view, 1)

        self.lbl_paths = QLabel("")
        self.lbl_paths.setFont(mono_font())
        self.lbl_paths.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_paths.setWordWrap(True)
        self.lbl_paths.setStyleSheet(f"color:{_C_DIM};")
        self.lbl_paths.setVisible(False)
        root.addWidget(self.lbl_paths)

        self.btn_clear.clicked.connect(self._on_clear)
        self.btn_paths.clicked.connect(self._on_paths)
        self._update_counters()

        if self.service("record") is None:
            self.btn_paths.setEnabled(False)
            self.btn_paths.setToolTip("会话录制服务未启用")

    # ------------------------------------------------------------------ 桥分发
    def on_batch(self, topic: str, items: list) -> None:
        if topic == Events.AT_EXCHANGE:
            for item in items:
                self._counts["at"] += 1
                if self.chk_at.isChecked():
                    self._append(self._fmt_at(item))
        elif topic == Events.ALARM:
            level_min = self.cmb_level.currentIndex()
            for item in items:
                self._counts["alarm"] += 1
                level = str(getattr(item, "level", ""))
                if self.chk_alarm.isChecked() and self._level_pass(level, level_min):
                    self._append(self._fmt_alarm(item))
        elif topic == Events.LOG:
            level_min = self.cmb_level.currentIndex()
            for item in items:
                self._counts["log"] += 1
                level = ""
                if isinstance(item, dict):
                    level = str(item.get("level", ""))
                if self.chk_log.isChecked() and self._level_pass(level, level_min):
                    self._append(self._fmt_log(item))
        else:
            return
        self._update_counters()

    @staticmethod
    def _level_pass(level: str, level_min: int) -> bool:
        if level_min <= 0:
            return True
        if level_min == 1:
            return level in _WARN_LEVELS
        return level in _ERR_LEVELS

    # ------------------------------------------------------------------ 格式化
    def _fmt_at(self, item: object) -> str:
        if not isinstance(item, AtExchange):
            return f"<span style='color:{_C_DIM};'>[{_stamp(None)}] AT {_esc(item)}</span>"
        sym = _AT_SYMBOL.get(item.kind, "•")
        color = _C_OK if item.ok else _C_BAD
        state = "OK" if item.ok else "FAIL"
        name = item.name or "-"
        bits = [f"{sym} {name} {state}"]
        if item.rtt_ms:
            bits.append(f"{item.rtt_ms:.0f}ms")
        if item.line:
            bits.append(f"→ {item.line}")
        if not item.ok and item.error:
            bits.append(f"✗ {item.error}")
        elif item.response:
            first = item.response[0]
            more = f" (+{len(item.response) - 1})" if len(item.response) > 1 else ""
            bits.append(f"⤷ {first}{more}")
        if item.channel:
            bits.append(f"[{item.channel}]")
        body = _esc("  ".join(bits))
        return _span(_C_DIM, f"[{_stamp(item.ts)}]") + " " + _span(color, body)

    def _fmt_alarm(self, item: object) -> str:
        if not isinstance(item, AlarmEvent):
            return f"<span style='color:{_C_WARN};'>[{_stamp(None)}] 告警 {_esc(item)}</span>"
        color = _ALARM_COLORS.get(item.level, _C_WARN)
        text = _esc(f"[{item.level.upper()}] {item.source}/{item.code}: {item.message}")
        return _span(_C_DIM, f"[{_stamp(item.ts)}]") + " " + _span(color, f"⚠ {text}")

    def _fmt_log(self, item: object) -> str:
        if isinstance(item, dict):
            level = str(item.get("level", ""))
            color = _LOG_COLORS.get(level, _C_TEXT)
            text = str(item.get("text") or item.get("message") or item)
            stamp = _stamp(item.get("ts"))
        else:
            color = _C_TEXT
            text = str(item)
            stamp = _stamp(None)
        return _span(_C_DIM, f"[{stamp}]") + " " + _span(color, _esc(text))

    # ------------------------------------------------------------------ 追加与滚动
    def _append(self, rich_line: str) -> None:
        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.End)
        # QPlainTextEdit 里 <br/> 只是行分隔符、不产生新段落，多条事件会挤在同一行；
        # 每条事件先起一个真正的文本块，保证一条一行、按时间各自换行。
        if self.view.document().lastBlock().text():
            cursor.insertBlock()
        cursor.insertHtml(rich_line)
        self.view.setTextCursor(cursor)
        if self.chk_autoscroll.isChecked():
            bar = self.view.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _update_counters(self) -> None:
        self.lbl_counts.setText(
            f"AT {self._counts['at']} · 告警 {self._counts['alarm']} · 日志 {self._counts['log']}"
        )

    # ------------------------------------------------------------------ 动作
    def _on_clear(self) -> None:
        self.view.clear()
        self._counts = {"at": 0, "alarm": 0, "log": 0}
        self._update_counters()

    def _on_paths(self) -> None:
        record = self.service("record")
        if record is None:
            self.say("会话录制服务未启用", 8000)
            return
        paths = record.paths()
        if not paths:
            self.say("录制还没生成文件（先开始采集/录制）", 8000)
            return
        lines = [f"<b>{_esc(k)}</b> : {_esc(v)}" for k, v in sorted(paths.items())]
        self.lbl_paths.setText("<br/>".join(lines))
        self.lbl_paths.setVisible(True)
        self.say(f"录制文件路径已显示在下方（{len(paths)} 项）")
