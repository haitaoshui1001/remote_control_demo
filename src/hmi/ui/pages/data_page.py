"""数据流水页：485 双向字节记录表 + 过滤 + 链路统计条 + CSV 导出。

字节进表只有一条路：桥的 ``PT_BYTES`` 批次事件（流水类主题，按序交付、不多
不少）。暂停时事件继续攒进一个有界队列，恢复时一次性补进表里——这样"表所见"
与"事件流"永远一一对应，不需要拿服务游标接口和批次事件做两条通路的去重协调。
初始进入用 ``all_records()`` 回填缓冲尾部，之后一律靠事件流增量。

``PT_STATS`` 是合并类主题：只取最新值刷统计条，不排队。桥侧的 ``dropped()``
计数会显示在行数标签里——积压过多时事件在桥被丢弃意味着表不完整，必须让用户
看得见这件事。

行数有上限（``_MAX_ROWS``），超限从队首裁剪，长时间运行内存可控；清空走服务层
``clear()``，只清记录缓冲、不影响收发计数（这一点在状态栏里说清楚）。
"""

from __future__ import annotations

import csv
import time
from collections import deque

from PyQt5.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ...core.events import Events
from ...core.models import ByteRecord, PassthroughStats
from ..theme import label_px, mono_font
from .base import BasePage

__all__ = ["DataPage"]

_COLS: tuple[str, ...] = ("时间", "方向", "种类", "端点", "通道", "Hex", "ASCII", "字节", "备注")
_C_TS, _C_DIR, _C_KIND, _C_EP, _C_CH, _C_HEX, _C_ASCII, _C_LEN, _C_NOTE = range(9)
_COL_WIDTHS: tuple[int, ...] = (96, 52, 52, 84, 84, 300, 180, 48)
_MAX_ROWS = 2000
_PENDING_CAP = 2000
_SORT_ROLE = Qt.UserRole
_RX_COLOR = QColor("#1b7a43")
_TX_COLOR = QColor("#0b57d0")
_DIM_COLOR = QColor("#6b7482")

_KIND_LABELS: dict[str, str] = {
    "rx": "接收",
    "reply": "应答",
    "command": "指令",
    "manual": "手动",
    "template": "模板",
    "raw": "原始",
}
_DIR_LABELS: dict[str, str] = {"rx": "接收", "tx": "发送"}


def _fmt_ts(ts: float, with_date: bool = False) -> str:
    if not ts:
        return ""
    pattern = "%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S"
    return f"{time.strftime(pattern, time.localtime(ts))}.{int(ts % 1 * 1000):03d}"


def _fmt_bps(value: float) -> str:
    if value >= 1024 * 1024:
        return f"{value / 1024 / 1024:.2f} Mbps"
    if value >= 1024:
        return f"{value / 1024:.1f} kbps"
    return f"{value:.0f} bps"


class _RecordModel(QAbstractTableModel):
    """字节流水表模型：只读，排序键走 UserRole，显示文本走 DisplayRole。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[ByteRecord] = []

    def rowCount(self, parent: QModelIndex | None = None) -> int:  # noqa: N802
        if parent is not None and parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex | None = None) -> int:  # noqa: N802
        return len(_COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return _COLS[section]
        return str(section + 1)

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        rec = self._rows[index.row()]
        col = index.column()
        if role in (Qt.DisplayRole, Qt.ToolTipRole):
            return self._text(rec, col)
        if role == _SORT_ROLE:
            return self._key(rec, col)
        if role == Qt.ForegroundRole:
            if col == _C_DIR:
                return _RX_COLOR if rec.direction == "rx" else _TX_COLOR
            if col == _C_LEN:
                return _DIM_COLOR
            return None
        if role == Qt.FontRole and col in (_C_HEX, _C_ASCII):
            return mono_font()
        return None

    @staticmethod
    def _text(rec: ByteRecord, col: int) -> str:
        if col == _C_TS:
            return _fmt_ts(rec.ts)
        if col == _C_DIR:
            return _DIR_LABELS.get(rec.direction, rec.direction)
        if col == _C_KIND:
            return _KIND_LABELS.get(rec.kind, rec.kind)
        if col == _C_EP:
            return rec.endpoint
        if col == _C_CH:
            return rec.channel
        if col == _C_HEX:
            return rec.hex()
        if col == _C_ASCII:
            return rec.ascii()
        if col == _C_LEN:
            return str(len(rec.data))
        return rec.note

    @staticmethod
    def _key(rec: ByteRecord, col: int):
        if col == _C_TS:
            return rec.ts
        if col == _C_LEN:
            return len(rec.data)
        if col == _C_DIR:
            return rec.direction
        if col == _C_KIND:
            return rec.kind
        return _RecordModel._text(rec, col)

    def append_many(self, records: list[ByteRecord]) -> None:
        if not records:
            return
        first = len(self._rows)
        self.beginInsertRows(QModelIndex(), first, first + len(records) - 1)
        self._rows.extend(records)
        self.endInsertRows()
        surplus = len(self._rows) - _MAX_ROWS
        if surplus > 0:
            self.beginRemoveRows(QModelIndex(), 0, surplus - 1)
            del self._rows[:surplus]
            self.endRemoveRows()

    def reset_rows(self, records: list[ByteRecord]) -> None:
        self.beginResetModel()
        self._rows = list(records[-_MAX_ROWS:])
        self.endResetModel()


class _RecordFilter(QSortFilterProxyModel):
    """方向 / 种类按枚举值精确过滤，关键字按去空格小写子串扫文本列。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._direction = ""
        self._kind = ""
        self._keyword = ""

    def set_direction(self, value: str) -> None:
        self._direction = value
        self.invalidateFilter()

    def set_kind(self, value: str) -> None:
        self._kind = value
        self.invalidateFilter()

    def set_keyword(self, value: str) -> None:
        self._keyword = value.lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802
        sm = self.sourceModel()
        if self._direction:
            got = sm.index(source_row, _C_DIR, source_parent).data(_SORT_ROLE)
            if str(got or "") != self._direction:
                return False
        if self._kind:
            got = sm.index(source_row, _C_KIND, source_parent).data(_SORT_ROLE)
            if str(got or "") != self._kind:
                return False
        if self._keyword:
            for col in (_C_EP, _C_CH, _C_HEX, _C_ASCII, _C_NOTE):
                text = str(sm.index(source_row, col, source_parent).data(Qt.DisplayRole) or "")
                if self._keyword in text.lower().replace(" ", ""):
                    return True
            return False
        return True


class DataPage(BasePage):
    """字节流水表 + 过滤 + 链路统计条 + CSV 导出。"""

    page_key = "data"
    title = "数据流水"

    def __init__(self, session, bridge, parent: QWidget | None = None) -> None:
        super().__init__(session, bridge, parent)
        self._paused = False
        self._pending: deque[ByteRecord] = deque(maxlen=_PENDING_CAP)
        self._pending_dropped = 0
        self._model = _RecordModel(self)
        self._proxy = _RecordFilter(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setSortRole(_SORT_ROLE)
        self._known_kinds: set[str] = set(_KIND_LABELS)
        self._build_widgets()
        self._backfill()
        self._prime_stats()

    # ------------------------------------------------------------------ 装配
    def _build_widgets(self) -> None:
        svc = self.service("passthrough")
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("方向"))
        self.dir_combo = QComboBox()
        for value, label in (("", "全部"), ("rx", "接收"), ("tx", "发送")):
            self.dir_combo.addItem(label, value)
        self.dir_combo.activated.connect(self._on_filter_changed)
        bar.addWidget(self.dir_combo)

        bar.addWidget(QLabel("种类"))
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("全部", "")
        for key in ("rx", "reply", "command", "manual", "template", "raw"):
            self.kind_combo.addItem(_KIND_LABELS[key], key)
        self.kind_combo.activated.connect(self._on_filter_changed)
        bar.addWidget(self.kind_combo)

        self.kw_edit = QLineEdit()
        self.kw_edit.setPlaceholderText("关键字：Hex/ASCII/端点/通道/备注")
        self.kw_edit.setClearButtonEnabled(True)
        self.kw_edit.textChanged.connect(self._on_keyword)
        bar.addWidget(self.kw_edit, 1)

        self.pause_btn = QPushButton("暂停")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._on_pause)
        bar.addWidget(self.pause_btn)

        self.autoscroll_check = QCheckBox("自动滚动")
        self.autoscroll_check.setChecked(True)
        bar.addWidget(self.autoscroll_check)

        self.export_btn = QPushButton("导出 CSV")
        self.export_btn.clicked.connect(self._on_export)
        bar.addWidget(self.export_btn)

        self.clear_btn = QPushButton("清空流水")
        self.clear_btn.clicked.connect(self._on_clear)
        bar.addWidget(self.clear_btn)
        root.addLayout(bar)

        stats = QHBoxLayout()
        self._labels: dict[str, QLabel] = {}
        for key, text in (
            ("conn", "● --"),
            ("port", "端口 --"),
            ("frames", "帧 收--/发--"),
            ("bytes", "字节 收--/发--"),
            ("rate", "速率 --"),
            ("health", "待答-- 超时-- 错误--"),
            ("flow", "行数 --"),
        ):
            lab = QLabel(text)
            lab.setStyleSheet(f"color:#3c4654; font-size:{label_px()}px;")
            self._labels[key] = lab
            stats.addWidget(lab)
        stats.addStretch(1)
        root.addLayout(stats)

        self.table = QTableView()
        self.table.setModel(self._proxy)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setEditTriggers(QTableView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setStretchLastSection(True)
        hdr.setMinimumSectionSize(44)
        for col, width in enumerate(_COL_WIDTHS):
            self.table.setColumnWidth(col, width)
        self.table.setSortingEnabled(True)
        self._proxy.sort(_C_TS, Qt.AscendingOrder)
        root.addWidget(self.table, 1)

        if svc is None:
            for btn in (self.pause_btn, self.export_btn, self.clear_btn):
                btn.setEnabled(False)
            self._labels["conn"].setText("● 透传服务未启用")
            self._labels["conn"].setStyleSheet(f"color:#b3261e; font-size:{label_px()}px;")

    # ------------------------------------------------------------------ 数据通路
    def _backfill(self) -> None:
        svc = self.service("passthrough")
        if svc is None:
            return
        records = svc.all_records()
        for rec in records:
            self._register_kind(rec.kind)
        self._model.reset_rows(records)
        self._update_flow_label()

    def on_batch(self, topic: str, items: list) -> None:
        if topic != Events.PT_BYTES:
            return
        if self._paused:
            overflow = len(self._pending) + len(items) - self._pending.maxlen
            if overflow > 0:
                self._pending_dropped += overflow
            self._pending.extend(items)
            self._update_flow_label()
            return
        self._append(list(items))

    def on_slot(self, topic: str) -> None:
        if topic != Events.PT_STATS:
            return
        st = self.bridge.latest(topic)
        if isinstance(st, PassthroughStats):
            self._render_stats(st)

    def _append(self, records: list[ByteRecord]) -> None:
        if not records:
            return
        sb = self.table.verticalScrollBar()
        stick = sb.value() >= sb.maximum() - 40
        for rec in records:
            self._register_kind(rec.kind)
        self._model.append_many(records)
        if stick and self.autoscroll_check.isChecked():
            sb.setValue(sb.maximum())
        self._update_flow_label()

    def _register_kind(self, kind: str) -> None:
        if not kind or kind in self._known_kinds:
            return
        self._known_kinds.add(kind)
        self.kind_combo.blockSignals(True)
        self.kind_combo.addItem(_KIND_LABELS.get(kind, kind), kind)
        self.kind_combo.blockSignals(False)

    # ------------------------------------------------------------------ 交互
    def _on_filter_changed(self) -> None:
        self._proxy.set_direction(str(self.dir_combo.currentData() or ""))
        self._proxy.set_kind(str(self.kind_combo.currentData() or ""))
        self._update_flow_label()

    def _on_keyword(self, text: str) -> None:
        self._proxy.set_keyword(text.strip())
        self._update_flow_label()

    def _on_pause(self, checked: bool) -> None:
        self._paused = checked
        self.pause_btn.setText("继续" if checked else "暂停")
        if not checked:
            self._flush_pending()
        self._update_flow_label()

    def _flush_pending(self) -> None:
        if not self._pending:
            return
        batch = list(self._pending)
        self._pending.clear()
        self._pending_dropped = 0
        self._append(batch)

    def _on_export(self) -> None:
        try:
            self.export_csv()
        except OSError as exc:
            self.say(f"导出失败：{exc}", 8000)

    def _on_clear(self) -> None:
        svc = self.service("passthrough")
        if svc is None:
            return
        try:
            n = svc.clear()
        except Exception as exc:  # noqa: BLE001 - 按钮槽必须兜住
            self.say(f"清空失败：{exc}", 8000)
            return
        self._pending.clear()
        self._pending_dropped = 0
        self._model.reset_rows([])
        self._update_flow_label()
        self.say(f"已清空 {n} 条流水记录（收发计数不受影响）")

    def export_csv(self, path: str | None = None) -> str | None:
        """导出当前过滤可见的行；path 为空弹保存框，用户取消则返回 None。"""
        if path is None:
            default = f"passthrough_{time.strftime('%Y%m%d_%H%M%S')}.csv"
            path, _sel = QFileDialog.getSaveFileName(
                self, "导出 CSV", default, "CSV 文件 (*.csv);;所有文件 (*)"
            )
            if not path:
                return None
        if not path.lower().endswith(".csv"):
            path += ".csv"
        rows = self._proxy.rowCount()
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(_COLS)
            for r in range(rows):
                cells = []
                for c in range(len(_COLS)):
                    idx = self._proxy.index(r, c)
                    if c == _C_TS:
                        cells.append(_fmt_ts(float(self._proxy.data(idx, _SORT_ROLE) or 0.0), True))
                    else:
                        cells.append(str(self._proxy.data(idx, Qt.DisplayRole) or ""))
                writer.writerow(cells)
        self.say(f"已导出 {rows} 条流水 → {path}", 8000)
        return path

    # ------------------------------------------------------------------ 状态呈现
    def _prime_stats(self) -> None:
        st = self.bridge.latest(Events.PT_STATS)
        if not isinstance(st, PassthroughStats):
            svc = self.service("passthrough")
            st = svc.stats() if svc is not None else None
        if isinstance(st, PassthroughStats):
            self._render_stats(st)

    def _render_stats(self, st: PassthroughStats) -> None:
        conn = self._labels["conn"]
        conn.setText("● 已连接" if st.connected else "● 未连接")
        conn_color = "#1b7a43" if st.connected else "#b3261e"
        conn.setStyleSheet(f"color:{conn_color}; font-size:{label_px()}px;")
        self._labels["port"].setText(f"端口 {st.port or '--'}")
        self._labels["frames"].setText(f"帧 收 {st.rx_frames}/发 {st.tx_frames}")
        self._labels["bytes"].setText(f"字节 收 {st.rx_bytes}/发 {st.tx_bytes}")
        self._labels["rate"].setText(
            f"速率 ↓{_fmt_bps(st.rx_rate_bps)} ↑{_fmt_bps(st.tx_rate_bps)} "
            f"{st.rx_rate_fps:.1f} fps"
        )
        parts = []
        if st.pending:
            parts.append(f"待答 {st.pending}")
        parts.append(f"超时 {st.timeouts}")
        parts.append(f"错误 {st.errors}")
        self._labels["health"].setText(" ".join(parts))

    def _update_flow_label(self) -> None:
        parts = [f"行数 {self._proxy.rowCount()}/{self._model.rowCount()}"]
        try:
            dropped = int(self.bridge.dropped(Events.PT_BYTES))
        except Exception:  # noqa: BLE001 - 提示性信息，取不到就算了
            dropped = 0
        if dropped:
            parts.append(f"桥丢弃 {dropped}")
        if self._paused:
            extra = f"暂停积压 {len(self._pending)}"
            if self._pending_dropped:
                extra += f"（溢出丢 {self._pending_dropped}）"
            parts.append(extra)
        self._labels["flow"].setText(" · ".join(parts))
