"""电台配置页：由参数注册表生成的 AT 参数表单（读取 / 编辑 / 预览 / 下发）。

表单行来自 ``RadioService.catalog()``——注册表是唯一真相源，页面不硬编码任何
参数名、范围或枚举；协议手册改了注册表，这一页自动跟着变。可写行给出编辑控件
（有枚举的用下拉，其余用文本框，留空即不发送），只读行禁用编辑。
"动作指令（只写）"分组单独处理：不进"预览/下发修改"批量流程，每行一个"执行"
按钮经 ``action()`` 下发；注册表标了 ``confirm`` 的危险动作（PAIR / UNPAIR /
LOADDEF）先弹二次确认，确认文案同样来自注册表，页面不写死参数名。

周期回填走合并类主题 ``RADIO_PARAMS``（遥测快照的 16 键视图）：只刷"当前值"
列，不碰用户尚未下发的编辑。手动"读取全部"放到后台线程跑 ``read_many()``——
实机模块不应答时每个参数都要等满超时加重试，同步跑会把界面冻死；结果经信号
回填表格，读取期间按钮置灰防止叠加请求。

预览面板是下发前的最后一道检查：用与客户端同款的 ``encode + format_at`` 列出
将要发出的完整 AT 行，校验失败的值单独标出，绝不静默吞掉。
服务未装配时所有动作禁用，页面仍然可读。
"""

from __future__ import annotations

import time

from PyQt5.QtCore import QSize, Qt, QThread, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QFont
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.events import Events
from ...protocol import param_registry as registry
from ...protocol.at_codec import format_at
from ...transport.serial_transport import list_serial_ports
from ..theme import font_px, mono_font
from .base import BasePage

__all__ = ["RadioPage"]

_COLS = ("参数", "当前值", "新值", "限制", "读写", "说明")
_C_NAME, _C_CUR, _C_EDIT, _C_BOUND, _C_RW, _C_DESC = range(6)
_RW_LABELS = {"rw": "读+写", "ro": "只读", "wo": "只写"}
_VIEW_KEYS = {
    "role": "MODE",
    "ssid": "SSID",
    "keymgmt": "KEYMGMT",
    "bandwidth_mhz": "BSS_BW",
    "freq_range": "FREQ_RANGE",
    "chan_list": "CHAN_LIST",
    "txpower_dbm": "TXPOWER",
    "tx_mcs": "TX_MCS",
    "acktmo_us": "ACKTMO",
    "heart_int_ms": "HEART_INT",
    "rssi_dbm": "RSSI",
    "evm_db": "EVM",
}
_DIM_COLOR = QColor("#6b7482")
_ERR_COLOR = QColor("#b3261e")
_GROUP_BG = QColor("#eef2f7")

#: "当前值"列固定像素宽度：长值（如信道列表）在列内换行，不再撑宽整列。
_CUR_COL_PX = 300

#: "读写"列固定像素宽度：约两倍的自适应宽度，"读+写""只读"看着不局促。
_RW_COL_PX = 140

#: 读到值染绿底、错误码染红底；判定用包含式，兼容 "+ERR=6" / "ERROR" / "sta:1 is not exist"。
_OK_BG = QColor("#e3f4e4")
_ERR_BG = QColor("#fbe4e2")
_ERR_TOKENS = ("ERR", "FAIL", "NOT EXIST", "不存在")

_cur_font_cache: QFont | None = None


def _is_error_text(text: str) -> bool:
    upper = text.upper()
    return any(tok in upper for tok in _ERR_TOKENS)


def _cur_font() -> QFont:
    """"当前值"列的小一号等宽字体，按正文基准联动，固定宽度下能容下长值。"""
    global _cur_font_cache
    if _cur_font_cache is None:
        _cur_font_cache = QFont("Consolas", max(9, font_px() // 2 + 1))
    return _cur_font_cache


class _SingleLineDelegate(QStyledItemDelegate):
    """说明列单行显示：超长截断加省略号，行高不再被折行撑大，全文看悬浮提示。"""

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        base = super().sizeHint(option, index)
        return QSize(base.width(), option.fontMetrics.lineSpacing() + 6)

    def paint(self, painter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        if not text:
            super().paint(painter, option, index)
            return
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)
        # 文本矩形直接用单元格 rect：全局样式表生效时 QStyleSheetStyle 的
        # subElementRect(SE_ItemViewItemText) 会返回塌陷矩形，文字整列画不出来。
        rect = opt.rect.adjusted(4, 0, -4, 0)
        painter.save()
        painter.setFont(opt.font)
        selected = bool(opt.state & QStyle.State_Selected)
        # QPalette 的 highlightedText()/windowText() 返回 QBrush，setPen 只收 QColor；
        # 直接传 Brush 会在每次绘制时抛 TypeError，文字整列画不出来。
        brush = opt.palette.highlightedText() if selected else opt.palette.windowText()
        painter.setPen(brush.color())
        metrics = painter.fontMetrics()
        painter.drawText(
            rect,
            Qt.AlignVCenter | Qt.AlignLeft,
            metrics.elidedText(text, Qt.ElideRight, max(0, rect.width())),
        )
        painter.restore()


class _ReadWorker(QThread):
    """后台跑 ``read_many()``：一个参数没应答就要等满超时+重试，不能占着 UI 线程。"""

    done = pyqtSignal(dict)

    def __init__(self, svc, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._svc = svc

    def run(self) -> None:
        try:
            self.done.emit(self._svc.read_many())
        except Exception as exc:  # noqa: BLE001
            self.done.emit({"ok": False, "detail": f"读取异常：{exc}", "values": {}})


_ORPHAN_WORKERS: list[_ReadWorker] = []


def _prune_orphans() -> None:
    """回收已跑完的孤儿线程；没跑完的必须留引用，否则对象被 GC 时会连线程一起崩。"""
    _ORPHAN_WORKERS[:] = [w for w in _ORPHAN_WORKERS if w.isRunning()]


class RadioPage(BasePage):
    """电台参数配置页：注册表驱动表单 + 读取/预览/下发/动作。"""

    page_key = "radio"
    title = "电台配置"

    def __init__(self, session, bridge, parent: QWidget | None = None) -> None:
        super().__init__(session, bridge, parent)
        self._row_of: dict[str, int] = {}
        self._kind_of: dict[str, str] = {}
        self._confirm_of: dict[str, str] = {}
        #: 在线模块 ≥2 台后置 False：不再提供"自动（首个在线模块）"，必须先手选一台。
        self._auto_allowed = True
        self._editors: dict[str, QWidget] = {}
        self._read_worker: _ReadWorker | None = None
        self._build_widgets()
        self._load_catalog()
        view = self.bridge.latest(Events.RADIO_PARAMS)
        if isinstance(view, dict):
            self._apply_view(view)
        self._refresh_devices()

    # ------------------------------------------------------------------ UI 搭建
    def _build_widgets(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # 目标设备行：UDP 通道下同网段可能有多个模块，先发现、再按 MAC 定向读写。
        dev_row = QHBoxLayout()
        dev_row.setSpacing(6)
        dev_row.addWidget(QLabel("目标设备"))
        self.combo_device = QComboBox()
        self.combo_device.setMinimumWidth(260)
        self.combo_device.setPlaceholderText("选择目标模块")
        self.btn_discover = QPushButton("发现/刷新")
        self.dev_label = QLabel("尚未发现模块")
        self.dev_label.setStyleSheet(f"color:{_DIM_COLOR.name()};")
        dev_row.addWidget(self.combo_device)
        dev_row.addWidget(self.btn_discover)
        dev_row.addWidget(self.dev_label)
        dev_row.addStretch(1)
        root.addLayout(dev_row)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.btn_read = QPushButton("读取全部")
        self.btn_preview = QPushButton("预览下发")
        self.btn_apply = QPushButton("下发修改")
        self.btn_clear = QPushButton("清空编辑")
        self.btn_defaults = QPushButton("清空本地缓存")
        self.btn_defaults.setToolTip(
            "丢弃本机缓存的参数值，下次读取完全以模块为准；"
            "不会向模块发任何指令。真要恢复出厂请展开"
            "“动作指令（只写）”分组执行 LOADDEF"
        )
        self.btn_reconnect = QPushButton("重新连接")
        for b in (self.btn_read, self.btn_preview, self.btn_apply, self.btn_clear):
            bar.addWidget(b)
        bar.addWidget(self.btn_defaults)
        bar.addWidget(self.btn_reconnect)
        bar.addStretch(1)
        self.tel_label = QLabel("等待遥测快照")
        self.tel_label.setStyleSheet(f"color:{_DIM_COLOR.name()};")
        bar.addWidget(self.tel_label)
        root.addLayout(bar)

        self.table = QTableWidget(0, len(_COLS))
        self.table.setHorizontalHeaderLabels(_COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(_C_CUR, QHeaderView.Fixed)
        hdr.setSectionResizeMode(_C_RW, QHeaderView.Fixed)
        hdr.setSectionResizeMode(_C_DESC, QHeaderView.Stretch)
        hdr.setStretchLastSection(False)
        self.table.setColumnWidth(_C_CUR, _CUR_COL_PX)
        self.table.setColumnWidth(_C_RW, _RW_COL_PX)
        self.table.setWordWrap(True)
        self.table.setItemDelegateForColumn(_C_DESC, _SingleLineDelegate(self.table))
        self.table.setMinimumHeight(300)
        root.addWidget(self.table, 1)

        self.preview_view = QPlainTextEdit()
        self.preview_view.setReadOnly(True)
        self.preview_view.setFont(mono_font())
        self.preview_view.setPlaceholderText("“预览下发”会在这里列出将要发出的完整 AT 行")
        self.preview_view.setMaximumHeight(140)
        root.addWidget(self.preview_view)

        self.btn_read.clicked.connect(self._on_read)
        self.btn_preview.clicked.connect(self._on_preview)
        self.btn_apply.clicked.connect(self._on_apply)
        self.btn_clear.clicked.connect(self._on_clear_edits)
        self.btn_defaults.clicked.connect(self._on_defaults)
        self.btn_reconnect.clicked.connect(self._on_reconnect)
        self.btn_discover.clicked.connect(self._on_discover)
        self.combo_device.currentIndexChanged.connect(self._on_device_changed)

        if self.service("link") is None:
            self.btn_reconnect.setEnabled(False)
            self.btn_discover.setEnabled(False)
            self.combo_device.setEnabled(False)
        if self.service("radio") is None:
            for b in (
                self.btn_read,
                self.btn_preview,
                self.btn_apply,
                self.btn_clear,
                self.btn_defaults,
            ):
                b.setEnabled(False)
            self.tel_label.setText("电台服务未启用")
            self.tel_label.setStyleSheet(f"color:{_ERR_COLOR.name()};")

    # ------------------------------------------------------------------ 目标设备选择
    @staticmethod
    def _device_support(link) -> bool:
        """仅 UDP 且带 select_device 的链路能按 MAC 定向；串口/mock 一律视为不支持。"""
        return (
            link is not None
            and str(getattr(link, "channel", "")) == "udp"
            and callable(getattr(link, "select_device", None))
        )

    def _current_device_mac(self, link) -> str:
        at = getattr(link, "at", None)
        raw = getattr(at, "target_mac", b"") if at is not None else b""
        if isinstance(raw, bytes) and len(raw) == 6:
            from ...transport.udp_at import format_mac

            return format_mac(raw)
        return ""

    def _refresh_devices(self, modules: list[dict] | None = None) -> None:
        """按模块表重建下拉；只读表不发网络，init 时安全。"""
        link = self.service("link")
        if link is None:
            return
        if not self._device_support(link):
            self.combo_device.setEnabled(False)
            self.btn_discover.setEnabled(False)
            self.dev_label.setText("当前通道不支持选择模块")
            return
        self.combo_device.setEnabled(True)
        self.btn_discover.setEnabled(True)
        if modules is None:
            getter = getattr(link, "at_modules", None)
            modules = list(getter()) if callable(getter) else []
        prev = ""
        idx = self.combo_device.currentIndex()
        if idx >= 0:
            prev = str(self.combo_device.itemData(idx) or "")
        if not prev:
            prev = self._current_device_mac(link)
        chosen = prev if any(str(m.get("mac", "")) == prev for m in modules) else ""
        online_n = sum(1 for m in modules if m.get("online"))
        # 只有一台（或还没发现到设备）时才允许"自动"。两台都在线还自动选，等于
        # 随机挑一台下发——正是"写功率 8 显示成功、读回还是 6"的成因。
        self._auto_allowed = online_n <= 1
        self.combo_device.blockSignals(True)
        try:
            self.combo_device.clear()
            if self._auto_allowed:
                self.combo_device.addItem("自动（首个在线模块）", "")
            for m in modules:
                mac = str(m.get("mac", ""))
                tag = "在线" if m.get("online") else "离线"
                self.combo_device.addItem(f"{mac}（{tag}）", mac)
            if chosen:
                self.combo_device.setCurrentIndex(self.combo_device.findData(chosen))
            elif self._auto_allowed:
                self.combo_device.setCurrentIndex(0)
            else:
                self.combo_device.setCurrentIndex(-1)
        finally:
            self.combo_device.blockSignals(False)
        self._render_dev_label(modules)
        if prev and chosen != prev:
            # 原选中模块已消失：直接按新的 chosen（可能是 ""）同步服务层定向目标，
            # 不能依赖下拉当前项——多台在线时这里可能根本没有可选项（index = -1）。
            self._select_target(chosen)

    def _render_dev_label(self, modules: list[dict]) -> None:
        """设备标签只反映"模块表 + 实际定向"，任何一处变了就该重画一次。

        原来这段文本只在点“发现/刷新”时算，选定目标后仍显示"当前未定向"，与预览区
        的"已定向"互相矛盾；多台在线又确实没定向时遥测会被 LinkMonitor 跳过，这里
        直接把后果写出来，用户才知道为什么数字不动。
        """
        if not modules:
            self.dev_label.setText("未发现模块，点“发现/刷新”")
            return
        online_n = sum(1 for m in modules if m.get("online"))
        target = self._radio_target().get("mac") or ""
        if target:
            tail = f"，当前定向 {target}"
        elif online_n >= 2:
            tail = "，当前未定向（多台在线，遥测已暂停，请先选一台）"
        else:
            tail = "，当前未定向"
        self.dev_label.setText(f"共 {len(modules)} 个，在线 {online_n} 个{tail}")

    def _sync_device_label(self) -> None:
        """按当前模块表重画设备标签（不发网络，只读传输层缓存）。"""
        link = self.service("link")
        if link is None or not self._device_support(link):
            return
        getter = getattr(link, "at_modules", None)
        self._render_dev_label(list(getter()) if callable(getter) else [])

    def _on_discover(self) -> None:
        link = self.service("link")
        discover = getattr(link, "discover", None) if link is not None else None
        if not callable(discover):
            self.say("当前会话没有链路服务，无法发现", 8000)
            return
        self.btn_discover.setEnabled(False)
        self.btn_discover.setText("发现中…")
        try:
            try:
                res = dict(discover())
            except Exception as exc:  # noqa: BLE001
                self.say(f"发现失败：{exc}", 10000)
                return
        finally:
            self.btn_discover.setText("发现/刷新")
            self.btn_discover.setEnabled(True)
        modules = list(res.get("modules") or [])
        if not modules:
            getter = getattr(link, "at_modules", None)
            modules = list(getter()) if callable(getter) else []
        self._refresh_devices(modules)
        self.say(str(res.get("detail") or f"发现完成：{len(modules)} 个"))

    def _select_target(self, mac: str) -> bool:
        """把下拉里的选择真正落到服务层/传输层；返回是否切换成功。"""
        link = self.service("link")
        if not self._device_support(link):
            return False
        radio = self.service("radio")
        switcher = getattr(radio, "select_device", None) if radio is not None else None
        if not callable(switcher):
            return False
        res = dict(switcher(mac) or {})
        if not res.get("ok"):
            self.say(f"切换失败：{res.get('detail', '')}", 8000)
            return False
        # 换模块 = 换一套参数：清掉未下发的编辑，重读注册表并回填该设备缓存视图。
        self._on_clear_edits()
        self._load_catalog()
        view = self.bridge.latest(Events.RADIO_PARAMS)
        if isinstance(view, dict):
            self._apply_view(view)
        # 定向一变就重画标签：别让下拉已选中、预览区"已定向"，标签还停在"当前未定向"。
        self._sync_device_label()
        self.say(
            str(res.get("detail") or (f"已切换目标模块 {mac}" if mac else "已恢复自动选择"))
        )
        return True

    def _on_device_changed(self, _index: int = 0) -> None:
        idx = self.combo_device.currentIndex()
        if idx < 0:
            return
        self._select_target(str(self.combo_device.itemData(idx) or ""))

    def _load_catalog(self) -> None:
        svc = self.service("radio")
        if svc is None:
            return
        rows = svc.catalog()
        self.table.setRowCount(0)
        self._row_of.clear()
        self._kind_of.clear()
        self._confirm_of.clear()
        self._editors.clear()
        current_group = None
        for row in rows:
            name = str(row.get("name", ""))
            group = str(row.get("group", ""))
            if group != current_group:
                self._insert_group_row(group)
                current_group = group
            table_row = self.table.rowCount()
            self.table.insertRow(table_row)
            self._row_of[name] = table_row

            title_item = QTableWidgetItem(str(row.get("title", name)))
            title_item.setToolTip(f"{name}  (AT+{row.get('at', name)})")
            self.table.setItem(table_row, _C_NAME, title_item)

            cur_text = self._display(name, row.get("value"))
            cur_item = QTableWidgetItem(cur_text)
            cur_item.setFont(_cur_font())
            cur_item.setTextAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
            self._tint_current(cur_item, cur_text)
            self.table.setItem(table_row, _C_CUR, cur_item)

            rw = str(row.get("rw", "ro"))
            kind = self._row_action_kind(row, rw)
            self._kind_of[name] = kind
            self._confirm_of[name] = str(row.get("confirm", "") or "")
            self.table.setCellWidget(table_row, _C_EDIT, self._make_editor(row, rw, kind))

            bound_item = QTableWidgetItem(str(row.get("bound", "")))
            bound_item.setForeground(_DIM_COLOR)
            self.table.setItem(table_row, _C_BOUND, bound_item)

            rw_item = QTableWidgetItem(_RW_LABELS.get(rw, rw))
            if rw == "ro":
                rw_item.setForeground(_DIM_COLOR)
            self.table.setItem(table_row, _C_RW, rw_item)

            desc = str(row.get("desc", ""))
            persist = str(row.get("persist", ""))
            tip = desc if not persist else f"{desc}\n掉电保存：{persist}"
            desc_item = QTableWidgetItem(desc)
            desc_item.setToolTip(tip)
            self.table.setItem(table_row, _C_DESC, desc_item)
        self.table.resizeRowsToContents()

    @staticmethod
    def _row_action_kind(row: dict, rw: str) -> str:
        if rw == "ro":
            return "ro"
        if str(row.get("group", "")) == registry.G_ACTION:
            return "action"
        return "write"

    def _insert_group_row(self, group: str) -> None:
        table_row = self.table.rowCount()
        self.table.insertRow(table_row)
        self.table.setSpan(table_row, 0, 1, len(_COLS))
        for col in range(len(_COLS)):
            item = QTableWidgetItem(group if col == 0 else "")
            item.setFlags(Qt.NoItemFlags)
            item.setBackground(_GROUP_BG)
            font = item.font()
            font.setBold(True)
            item.setFont(font)
            self.table.setItem(table_row, col, item)

    def _make_editor(self, row: dict, rw: str, kind: str) -> QWidget:
        name = str(row.get("name", ""))
        if rw == "ro":
            hold = QLineEdit()
            hold.setPlaceholderText("只读")
            hold.setEnabled(False)
            return hold
        options = list(row.get("options") or ())
        field: QWidget
        if options:
            combo = QComboBox()
            combo.addItem("")
            for opt in options:
                combo.addItem(str(opt))
            combo.setCurrentIndex(0)
            field = combo
        else:
            edit = QLineEdit()
            edit.setFont(mono_font())
            edit.setPlaceholderText("点“执行”下发" if kind == "action" else "留空不发送")
            unit = str(row.get("unit", "")).strip()
            if unit:
                edit.setToolTip(f"单位：{unit}")
            field = edit
        self._editors[name] = field
        if kind != "action":
            return field
        # 动作指令逐行手动执行：不随"下发修改"批量上线，危险动作先弹二次确认。
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(4)
        lay.addWidget(field, 1)
        btn = QPushButton("执行")
        danger = self._confirm_of.get(name, "")
        btn.setToolTip(danger or f"单独执行 AT+{name}，不参与批量下发")
        if danger:
            btn.setStyleSheet(f"color:{_ERR_COLOR.name()}; font-weight:bold;")
        btn.clicked.connect(lambda _checked=False, n=name: self._execute_action(n))
        lay.addWidget(btn)
        return box

    # ------------------------------------------------------------------ 值显示
    def _display(self, name: str, value: object) -> str:
        if value is None:
            return ""
        spec = registry.spec_for(name)
        scale = float(getattr(spec, "scale", 0.0) or 0.0) if spec is not None else 0.0
        if isinstance(value, bool):
            return "是" if value else "否"
        if isinstance(value, (list, tuple)):
            parts = [f"{v / scale:g}" if scale else str(v) for v in value]
            return " / ".join(parts)
        if isinstance(value, (int, float)):
            if scale:
                return f"{value / scale:g}"
            if isinstance(value, float):
                return f"{value:g}"
            return str(value)
        return str(value)

    @staticmethod
    def _tint_current(item: QTableWidgetItem, text: str) -> None:
        """读到值染绿底、错误码染红底，空值恢复默认底色。"""
        if not text:
            item.setBackground(QBrush())
        elif _is_error_text(text):
            item.setBackground(_ERR_BG)
        else:
            item.setBackground(_OK_BG)

    def _set_current(self, name: str, text: str) -> None:
        row = self._row_of.get(name)
        if row is None:
            return
        item = self.table.item(row, _C_CUR)
        if item is None:
            item = QTableWidgetItem()
            item.setFont(_cur_font())
            item.setTextAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
            self.table.setItem(row, _C_CUR, item)
        item.setText(text)
        self._tint_current(item, text)
        self.table.resizeRowToContents(row)

    def _editor_text(self, name: str) -> str:
        """取某行新值列当前填写的值；下拉首项（空）视为未填写。"""
        widget = self._editors.get(name)
        if isinstance(widget, QComboBox):
            return widget.currentText().strip() if widget.currentIndex() > 0 else ""
        if isinstance(widget, QLineEdit):
            return widget.text().strip()
        return ""

    def _collect_edits(self) -> list[tuple[str, str]]:
        """批量下发用的普通参数行；只读行和动作行都不在这里。"""
        out: list[tuple[str, str]] = []
        for name in self._editors:
            if self._kind_of.get(name) != "write":
                continue
            text = self._editor_text(name)
            if text:
                out.append((name, text))
        return out

    def _pending_actions(self) -> list[tuple[str, str]]:
        """新值列填了值的动作行：只用于提示"这些不会随批量下发上线"。"""
        out: list[tuple[str, str]] = []
        for name in self._editors:
            if self._kind_of.get(name) != "action":
                continue
            text = self._editor_text(name)
            if text:
                out.append((name, text))
        return out

    def _radio_target(self) -> dict:
        """服务层口径的"这条指令会发给谁"——不是下拉框文本，而是传输层实际定向值。"""
        svc = self.service("radio")
        getter = getattr(svc, "target", None) if svc is not None else None
        if not callable(getter):
            return {"mac": "", "auto": True, "total": 0, "online": 0}
        return dict(getter() or {})

    def _target_label(self) -> str:
        """给人看的当前目标：优先用传输层实际定向 MAC，其次回落到下拉选择。"""
        t = self._radio_target()
        mac = str(t.get("mac") or "")
        if mac:
            return f"{mac}（已定向）"
        idx = self.combo_device.currentIndex()
        if idx >= 0 and str(self.combo_device.itemData(idx) or ""):
            return f"{self.combo_device.itemData(idx)}（待定向）"
        return "自动（首个在线模块）" if t.get("auto", True) else "未选择目标模块"

    def _require_target(self) -> bool:
        """多模块在线时未手选一台就阻断读/写/动作：避免把指令发给"上一台"。"""
        link = self.service("link")
        if not self._device_support(link):
            return True
        idx = self.combo_device.currentIndex()
        if idx >= 0 and str(self.combo_device.itemData(idx) or ""):
            return True
        if self._auto_allowed:
            return True
        self.say(
            "发现多台在线模块：请先在“目标模块”下拉框选定一台，再读取/下发。"
            "未选定时程序不会替你猜设备",
            10000,
        )
        return False

    # ------------------------------------------------------------------ 动作
    def _on_read(self) -> None:
        svc = self.service("radio")
        if svc is None or self._read_worker is not None:
            return
        if not self._require_target():
            return
        self.btn_read.setEnabled(False)
        self.btn_read.setText("读取中…")
        _prune_orphans()
        worker = _ReadWorker(svc, self)
        worker.done.connect(self._on_read_done)
        worker.finished.connect(self._on_read_finished)
        self._read_worker = worker
        worker.start()

    def _on_read_finished(self) -> None:
        worker = self._read_worker
        self._read_worker = None
        if worker is not None:
            worker.deleteLater()
        self.btn_read.setEnabled(True)
        self.btn_read.setText("读取全部")

    def _on_read_done(self, res: dict) -> None:
        values = res.get("values") or {}
        for key, one in values.items():
            name = registry.resolve_key(key) or str(key)
            if not isinstance(one, dict):
                continue
            if one.get("ok"):
                self._set_current(name, self._display(name, one.get("value")))
                continue
            err = str(one.get("detail") or "")
            if _is_error_text(err):
                self._set_current(name, err)
        detail = str(res.get("detail") or "")
        if res.get("ok"):
            self.say(f"读取完成（目标：{self._target_label()}）："
                     f"{detail or f'{len(values)}/{len(values)} 项'}")
            return
        link = self.service("link")
        if link is not None and not link.is_open:
            self.say(f"读取失败：AT 通道未打开，{self._link_reason(link)}", 15000)
            return
        reason = ""
        for one in values.values():
            if isinstance(one, dict) and not one.get("ok"):
                reason = str(one.get("detail") or "")
                break
        self.say(f"读取失败：{detail or '全部参数无应答'}"
                 + (f"，第一条原因：{reason}" if reason else ""), 10000)

    def _link_reason(self, link) -> str:
        """拼出"通道为什么打不开"：LinkService 记录的原因 + 传输层残留原因 + 通道对应提示。"""
        err = ""
        if hasattr(link, "summary"):
            err = str(link.summary().get("error") or "").strip()
        if not err:
            transport = getattr(link, "transport", None)
            err = str(getattr(transport, "state_detail", "") or "").strip()
        head = f"失败原因：{err}。" if err else "未见记录的失败原因（可能从未打开或打开后被拔出）。"
        if str(getattr(link, "channel", "")) == "udp":
            return (
                f"{head}网口是本模块 AT 的唯一配置入口，协议为 UDP 56789 广播上的"
                "成帧 AT（先 op1 发现握手、AT 帧再定向模块 MAC），按序排查：电台处于"
                "唤醒模式（手册第六章首行注意）→ 网线接通 → 本机网卡设为 192.168.1.x "
                "静态 IP → 防火墙放行 UDP 56789，再点“重新连接”。可用 "
                "tools/probe_udp_at.py 查看模块 MAC 清单；多模块时在页顶“发现/刷新”"
                "选择目标设备（或把目标 MAC 填进 udp_at.target_mac）。串口是纯透传"
                "通道，AT 发串口只会透传到远端，程序不会回退串口"
            )
        ports = "、".join(
            f"{p['device']}（{p['description']}）" for p in list_serial_ports()[:8]
        ) or "一个都没有"
        return (
            f"{head}本模块 AT 只走网口配置（手册 6.2），串口是纯透传通道："
            f"向串口发 AT 会被透传到远端、不为本机电台解析。当前可见串口：{ports}；"
            "请把 config/app.yaml 的 at.channel 改为 udp，按网口提示排查后点“重新连接”"
        )

    def _on_reconnect(self) -> None:
        link = self.service("link")
        if link is None:
            return
        self.btn_reconnect.setEnabled(False)
        self.btn_reconnect.setText("重连中…")
        try:
            ok = link.restart()
        finally:
            self.btn_reconnect.setText("重新连接")
            self.btn_reconnect.setEnabled(True)
        if ok:
            self.say(f"AT 通道已重新打开（{link.channel}）")
            return
        self.say(f"重连失败：{self._link_reason(link)}", 20000)

    def on_close(self) -> None:
        worker = self._read_worker
        if worker is not None:
            self._read_worker = None
            for sig, slot in ((worker.done, self._on_read_done),
                              (worker.finished, self._on_read_finished)):
                try:
                    sig.disconnect(slot)
                except TypeError:
                    pass
            if not worker.wait(5000):
                # 还在等超时：摘掉父子关系并保命引用，否则页面析构时线程仍活着会崩
                worker.setParent(None)
                _ORPHAN_WORKERS.append(worker)
        super().on_close()

    def _on_preview(self) -> None:
        edits = self._collect_edits()
        actions = self._pending_actions()
        if not edits:
            note = "（新值列留空即不发送，预览为空）"
            if actions:
                note += "\n动作指令不参与批量下发，请在该行点“执行”：" + "、".join(
                    n for n, _ in actions
                )
            self.preview_view.setPlainText(note)
            self.say("没有待预览的修改" if not actions else "填的是动作指令，请点该行的“执行”")
            return
        bad: list[str] = []
        good: list[str] = []
        for name, text in edits:
            ok, value, err = registry.encode(name, text)
            if not ok:
                bad.append(f"✗ {name}={text} —— {err}")
                continue
            spec = registry.spec_for(name)
            wire = spec.wire_name if spec is not None else name.upper()
            good.append(format_at(wire, value if value else None))
        head = f"—— 计划下发 {len(good)} 条（目标：{self._target_label()}）——"
        if not good:
            head = "—— 无可下发项 ——"
        lines = bad + [head] + good
        if actions:
            lines.append(
                "—— 动作指令不在此列，需逐行点“执行”：" + "、".join(n for n, _ in actions) + " ——"
            )
        self.preview_view.setPlainText("\n".join(lines))
        if bad:
            self.say(f"预览：{len(bad)} 项校验失败，修正后才能下发", 8000)
        else:
            self.say(f"预览：{len(good)} 条待下发，确认后点“下发修改”")

    def _on_apply(self) -> None:
        svc = self.service("radio")
        if svc is None:
            return
        if not self._require_target():
            return
        edits = self._collect_edits()
        actions = self._pending_actions()
        if not edits:
            self.say(
                "没有待下发的修改（新值列留空即不发送）"
                if not actions
                else f"动作指令 {len(actions)} 项不参与批量下发，请点该行“执行”"
            )
            return
        lines: list[str] = [f"—— 目标：{self._target_label()} ——"]
        ok_n = 0
        noop_n = 0
        for name, text in edits:
            res = svc.write(name, text)
            ok = bool(res.get("ok"))
            if ok:
                ok_n += 1
                self._set_current(name, text)
            else:
                # "回了 OK 但值没生效"：当前值列必须显示模块里真正读到的值，
                # 否则界面会继续骗人。
                if res.get("verified") is False:
                    noop_n += 1
                    got = res.get("readback")
                    self._set_current(name, self._display(name, got) if got is not None else text)
            detail = str(res.get("detail") or res.get("error") or "")
            lines.append(f"{'OK ' if ok else 'FAIL'} {name}={text}  {detail}")
        if actions:
            lines.append(
                f"—— 已跳过动作指令 {len(actions)} 项（{ '、'.join(n for n, _ in actions)}）："
                '需要时逐行点"执行" ——'
            )
        self.preview_view.setPlainText("\n".join(lines))
        skip = f"，另跳过动作指令 {len(actions)} 项（需逐行点“执行”）" if actions else ""
        if ok_n == len(edits):
            self.say(f"下发完成 {ok_n}/{len(edits)}{skip}", 8000 if skip else 5000)
        elif noop_n:
            self.say(
                f"下发 {ok_n}/{len(edits)} 生效；{noop_n} 项模块回了 OK 但回读值没变，"
                "当前值列已按真实读值显示，详见预览区",
                12000,
            )
        else:
            self.say(f"下发 {ok_n}/{len(edits)} 成功，失败项见预览区{skip}", 8000)

    def _execute_action(self, name: str) -> None:
        """单条动作指令：取值 → 危险项弹确认 → 经服务层 action() 下发。"""
        svc = self.service("radio")
        if svc is None:
            return
        if not self._require_target():
            return
        text = self._editor_text(name)
        if not text:
            self.say(f"{name} 需要先填写执行参数（选值或输入值）", 8000)
            return
        danger = self._confirm_of.get(name, "")
        if danger:
            spec = registry.spec_for(name)
            wire = spec.wire_name if spec is not None else name.upper()
            ret = QMessageBox.question(
                self,
                f"确认执行 {name}？",
                f"目标模块：{self._target_label()}\n"
                f"指令：AT+{wire}={text}\n\n{danger}",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ret != QMessageBox.Yes:
                self.say(f"已取消 {name}，未向模块下发")
                return
        res = dict(svc.action(name, text, confirmed=True) or {})
        ok = bool(res.get("ok"))
        detail = str(res.get("detail") or res.get("error") or "")
        self.preview_view.setPlainText(f"{'OK ' if ok else 'FAIL'} {name}={text}  {detail}")
        if res.get("need_confirm"):
            self.say(f"{name} 被服务层拦下：{detail}", 10000)
        elif ok:
            self.say(f"动作 {name}={text} 已执行：{detail}")
        else:
            self.say(f"动作 {name} 执行失败：{detail}", 10000)

    def _on_clear_edits(self) -> None:
        for widget in self._editors.values():
            if isinstance(widget, QComboBox):
                widget.setCurrentIndex(0)
            elif isinstance(widget, QLineEdit):
                widget.clear()
        self.say("已清空新值列")

    def _on_defaults(self) -> None:
        svc = self.service("radio")
        if svc is None:
            return
        res = svc.load_defaults()
        self._load_catalog()
        if res.get("ok"):
            self.say("已清空本机参数缓存，当前值列待重新读取（未向模块下发任何指令）")
        else:
            self.say(f"恢复默认失败：{res.get('detail', '')}", 8000)

    # ------------------------------------------------------------------ 桥刷新
    def on_slot(self, topic: str) -> None:
        if topic != Events.RADIO_PARAMS:
            return
        view = self.bridge.latest(topic)
        if isinstance(view, dict):
            self._apply_view(view)

    def _apply_view(self, view: dict) -> None:
        for key, name in _VIEW_KEYS.items():
            if key in view and view[key] is not None:
                self._set_current(name, self._display(name, view[key]))
        connected = view.get("connected")
        if isinstance(connected, bool):
            self._set_current("CONN_STATE", "已连接" if connected else "未连接")
        ts = view.get("ts")
        stamp = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "--:--:--"
        link = "已连接" if connected else "未连接"
        endpoint = str(view.get("endpoint", "local"))
        self.tel_label.setText(f"遥测 {stamp} · {endpoint} · {link}")
        self.tel_label.setStyleSheet(
            f"color:{_DIM_COLOR.name() if connected else _ERR_COLOR.name()};"
        )
