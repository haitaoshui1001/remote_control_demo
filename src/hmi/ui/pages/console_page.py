"""指令控制台页：手动 485 透传 + 指令模板库。

本页最硬的约束来自一个事实：``PassthroughService.request()`` 与等应答的
``run_template()`` 是**阻塞**的——最长会等 ``reply_timeout_ms``。主线程不能碰
它们，否则等待期间整个界面卡死。所以所有"等应答"的下发都走一次性后台线程，
结果用 ``worker_done`` 信号投回主线程；``_busy`` 标志负责防重入。

日志只有一个入口：任何"指令结论"最终都会成为 ``CMD_RESULT`` 事件（引擎成功
路径 publish、服务层对失败补发），本页从桥统一接收并着色，后台线程回调不再
重复记日志——否则每条指令会出现两行。"只发不等"引擎不发结论，成功时本页
自己补一行；它失败时服务层补发过 ``CMD_RESULT``，本页只提示不动日志。

模板库直接复用 ``TemplateLibrary``：双击即下发；有 ``{占位符}`` 先弹参数框并
实时预览上线字节；编辑保存先过 ``validate()``，问题用红字而不是弹窗。任何
增删改都会 ``save()`` 落 ``config/templates.yaml``（测试可关持久化）。

右栏顶部是 485 串口选择与参数框（端口/波特率/数据位/校验/停止位）：应用走
``link.apply_passthrough_serial`` 即选即效，再 ``cfg.save()`` 写回 app.yaml，
写回失败只降级为"本次仅内存生效"。分线模式下"485 线"标签盯透传串口线
（TRANSPORT_STATE kind=pt），AT 网线的开关不再影响这里。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.events import Events
from ...core.models import CommandResult, CommandTemplate, TransportState
from ...core.utils import bytes_to_hex
from ...protocol.templates import missing_placeholders
from ..theme import mono_font
from .base import BasePage

__all__ = ["ConsolePage"]

_MODES: tuple[tuple[str, str], ...] = (("hex", "Hex"), ("ascii", "ASCII"))
_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("none", "无结束符"),
    ("crlf", "\\r\\n"),
    ("cr", "\\r"),
    ("lf", "\\n"),
    ("sp", "空格"),
    ("at", "AT"),
)
_OK_COLOR = QColor("#1b7a43")
_BAD_COLOR = QColor("#b3261e")
_DIM_COLOR = QColor("#6b7482")
_LOG_LIMIT = 300
_TID_ROLE = Qt.UserRole
_BAUDS: tuple[str, ...] = ("9600", "19200", "38400", "57600", "115200")
_BYTESIZES: tuple[tuple[str, str], ...] = (("8", "8"), ("7", "7"), ("6", "6"), ("5", "5"))
_PARITIES: tuple[tuple[str, str], ...] = (("N", "无 (N)"), ("E", "偶 (E)"), ("O", "奇 (O)"))
_STOPBITS: tuple[tuple[str, str], ...] = (("1", "1"), ("2", "2"))


def _combo_items(combo: QComboBox, pairs: tuple[tuple[str, str], ...], current: str) -> None:
    for value, label in pairs:
        combo.addItem(label, value)
    idx = combo.findData(current)
    combo.setCurrentIndex(idx if idx >= 0 else 0)


def _pick_combo(combo: QComboBox, value: str) -> None:
    idx = combo.findData(value)
    if idx >= 0:
        combo.setCurrentIndex(idx)


class ConsolePage(BasePage):
    """左模板库、右手动发送与结果日志。"""

    page_key = "console"
    title = "指令控制台"

    worker_done = pyqtSignal(object)

    def __init__(self, session, bridge, parent: QWidget | None = None) -> None:
        super().__init__(session, bridge, parent)
        self._busy = False
        self._closing = False
        self._line_online = True
        self._threaded = True
        self._watch_pt = False
        self._build_widgets()
        self.worker_done.connect(self._on_worker_done)
        self._load_defaults()
        self._refresh_tree()
        self._sync_buttons()

    # ------------------------------------------------------------------ 装配
    def _build_widgets(self) -> None:
        svc = self.service("passthrough")
        self.lib = svc.templates if svc is not None else None
        # 分线模式下"485 线"标签盯透传串口（kind=pt），共线盯 AT 线（kind=at）
        self._watch_pt = bool(getattr(svc, "split", False)) if svc is not None else False

        left = QGroupBox("指令模板库")
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["模板", "编码", "应答"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.setColumnWidth(1, 70)
        self.tree.setColumnWidth(2, 70)
        self.tree.itemDoubleClicked.connect(self._on_tree_double)

        self.btn_add = QPushButton("新增")
        self.btn_edit = QPushButton("编辑")
        self.btn_remove = QPushButton("删除")
        self.btn_up = QPushButton("上移")
        self.btn_down = QPushButton("下移")
        for btn, slot in (
            (self.btn_add, self._on_add_clicked),
            (self.btn_edit, self._on_edit_clicked),
            (self.btn_remove, self._on_remove_clicked),
            (self.btn_up, lambda: self._on_move_clicked(-1)),
            (self.btn_down, lambda: self._on_move_clicked(1)),
        ):
            btn.clicked.connect(slot)
        ops = QHBoxLayout()
        ops.setSpacing(6)
        for btn in (self.btn_add, self.btn_edit, self.btn_remove, self.btn_up, self.btn_down):
            ops.addWidget(btn)
        self.lbl_templates = QLabel("")
        self.lbl_templates.setStyleSheet("color:#6b7482;")
        lbox = QVBoxLayout(left)
        lbox.setContentsMargins(8, 6, 8, 6)
        lbox.addWidget(self.tree, 1)
        lbox.addLayout(ops)
        lbox.addWidget(self.lbl_templates)

        right = QWidget()
        serial_box = self._build_serial_group()

        manual = QGroupBox("手动透传")
        self.edit_payload = QLineEdit()
        self.edit_payload.setPlaceholderText("按所选编码填写指令，Hex 支持空格/逗号分隔")
        self.edit_payload.textChanged.connect(self._update_preview)
        self.combo_mode = QComboBox()
        self.combo_suffix = QComboBox()
        self.check_crc = QCheckBox("追加 CRC16")
        _combo_items(self.combo_mode, _MODES, "hex")
        _combo_items(self.combo_suffix, _SUFFIXES, "none")
        self.combo_mode.currentIndexChanged.connect(self._on_manual_cfg_changed)
        self.combo_suffix.currentIndexChanged.connect(self._on_manual_cfg_changed)
        self.check_crc.toggled.connect(self._update_preview)
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        row1.addWidget(QLabel("编码"))
        row1.addWidget(self.combo_mode)
        row1.addWidget(QLabel("结束符"))
        row1.addWidget(self.combo_suffix)
        row1.addWidget(self.check_crc)
        row1.addStretch(1)
        self.lbl_preview = QLabel("预览：等待输入")
        self.lbl_preview.setStyleSheet("color:#6b7482;")
        self.lbl_preview.setWordWrap(True)
        self.btn_send = QPushButton("只发送")
        self.btn_request = QPushButton("发送并等应答")
        self.btn_send.clicked.connect(self._on_send_clicked)
        self.btn_request.clicked.connect(self._on_request_clicked)
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row2.addWidget(self.btn_send)
        row2.addWidget(self.btn_request)
        row2.addStretch(1)
        mbox = QVBoxLayout(manual)
        mbox.setContentsMargins(8, 6, 8, 6)
        mbox.addWidget(self.edit_payload)
        mbox.addLayout(row1)
        mbox.addWidget(self.lbl_preview)
        mbox.addLayout(row2)

        self.lbl_line = QLabel("485 线：未知")
        self.lbl_line.setStyleSheet("color:#3d4a5c;")
        self.log_view = QListWidget()
        self.log_view.setAlternatingRowColors(True)
        self.log_view.setFont(mono_font())
        rbox = QVBoxLayout(right)
        rbox.setContentsMargins(0, 0, 0, 0)
        rbox.setSpacing(6)
        rbox.addWidget(serial_box)
        rbox.addWidget(manual)
        rbox.addWidget(self.lbl_line)
        rbox.addWidget(self.log_view, 1)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        box = QVBoxLayout(self)
        box.setContentsMargins(8, 8, 8, 4)
        box.addWidget(split)

        if self.lib is None:
            for wdg in (
                self.tree,
                self.btn_send,
                self.btn_request,
                self.edit_payload,
                self.combo_mode,
                self.combo_suffix,
                self.check_crc,
                self.combo_port,
                self.btn_ports,
                self.combo_baud,
                self.combo_bytes,
                self.combo_parity,
                self.combo_stop,
                self.btn_serial,
            ):
                wdg.setEnabled(False)
            self.lbl_preview.setText("未装配透传服务（--no-serial / --no-at）")

    def _build_serial_group(self) -> QGroupBox:
        """485 透传串口的选择与参数框：即选即效，应用后写回 app.yaml。"""
        box = QGroupBox("485 串口（USB 转 485 · 仅透传，AT 配置走网口）")
        self.combo_port = QComboBox()
        self.combo_port.setMinimumWidth(170)
        self.btn_ports = QPushButton("刷新")
        self.btn_ports.clicked.connect(self._refresh_ports)
        self.combo_baud = QComboBox()
        self.combo_baud.setEditable(True)
        for b in _BAUDS:
            self.combo_baud.addItem(b)
        self.combo_bytes = QComboBox()
        self.combo_parity = QComboBox()
        self.combo_stop = QComboBox()
        _combo_items(self.combo_bytes, _BYTESIZES, "8")
        _combo_items(self.combo_parity, _PARITIES, "N")
        _combo_items(self.combo_stop, _STOPBITS, "1")
        self.btn_serial = QPushButton("应用")
        self.btn_serial.clicked.connect(self._on_serial_apply_clicked)
        self.lbl_serial = QLabel("")
        self.lbl_serial.setStyleSheet("color:#6b7482;")
        row1 = QHBoxLayout()
        row1.setSpacing(6)
        row1.addWidget(QLabel("端口"))
        row1.addWidget(self.combo_port, 1)
        row1.addWidget(self.btn_ports)
        row2 = QHBoxLayout()
        row2.setSpacing(6)
        row2.addWidget(QLabel("波特率"))
        row2.addWidget(self.combo_baud)
        row2.addWidget(QLabel("数据位"))
        row2.addWidget(self.combo_bytes)
        row2.addWidget(QLabel("校验"))
        row2.addWidget(self.combo_parity)
        row2.addWidget(QLabel("停止位"))
        row2.addWidget(self.combo_stop)
        row2.addStretch(1)
        row2.addWidget(self.btn_serial)
        vbox = QVBoxLayout(box)
        vbox.setContentsMargins(8, 6, 8, 6)
        vbox.setSpacing(4)
        vbox.addLayout(row1)
        vbox.addLayout(row2)
        vbox.addWidget(self.lbl_serial)
        return box

    def _load_defaults(self) -> None:
        svc = self.service("passthrough")
        if svc is None:
            return
        cfg = svc.cfg.passthrough
        _pick_combo(self.combo_mode, cfg.mode)
        _pick_combo(self.combo_suffix, cfg.suffix)
        scfg = getattr(svc.cfg, "serial", None)
        if scfg is not None:
            self.combo_baud.setCurrentText(str(getattr(scfg, "baudrate", 115200)))
            _pick_combo(self.combo_bytes, str(getattr(scfg, "bytesize", 8)))
            _pick_combo(self.combo_parity, str(getattr(scfg, "parity", "N")))
            _pick_combo(self.combo_stop, str(getattr(scfg, "stopbits", 1)))
        self._refresh_ports(str(getattr(scfg, "port", "") or ""))
        self._update_serial_label()
        if self._watch_pt:
            link = self._link()
            if link is not None:
                self._line_online = bool(getattr(link, "pt_is_open", self._line_online))
                self._sync_buttons()
        self._update_preview()

    # ------------------------------------------------------------------ 485 串口配置
    def _link(self) -> Any:
        svc = self.service("passthrough")
        return getattr(svc, "link", None) if svc is not None else None

    def _refresh_ports(self, preserve: str = "") -> None:
        """重扫系统串口填下拉框；当前已配置口即使不在列表也保留可选。"""
        if not isinstance(preserve, str):
            preserve = ""  # 兼容 clicked 直连时注入的 bool 参数
        link = self._link()
        scan = getattr(link, "scan_ports", None)
        try:
            ports = list(scan()) if callable(scan) else []
        except Exception as exc:  # noqa: BLE001
            self._fail("扫描串口", exc)
            return
        current = (preserve or str(self.combo_port.currentData() or "")).strip()
        self.combo_port.blockSignals(True)
        self.combo_port.clear()
        for p in ports:
            device = str(p.get("device") or "")
            mark = " · USB" if p.get("likely_usb_uart") else ""
            self.combo_port.addItem(f"{device} — {p.get('description', '')}{mark}", device)
        if current and self.combo_port.findData(current) < 0:
            self.combo_port.addItem(f"{current}（当前未在线）", current)
        idx = self.combo_port.findData(current)
        if idx >= 0:
            self.combo_port.setCurrentIndex(idx)
        self.combo_port.blockSignals(False)

    def _serial_changes(self) -> dict[str, Any]:
        port = str(self.combo_port.currentData() or self.combo_port.currentText() or "").strip()
        try:
            baudrate = int(str(self.combo_baud.currentText()).strip())
        except ValueError:
            raise ValueError("波特率必须是整数") from None
        return {
            "port": port,
            "baudrate": baudrate,
            "bytesize": int(self.combo_bytes.currentData()),
            "parity": str(self.combo_parity.currentData()),
            "stopbits": int(self.combo_stop.currentData()),
        }

    def _update_serial_label(self) -> None:
        try:
            ch = self._serial_changes()
        except ValueError:
            return
        self.lbl_serial.setText(
            f"当前：{ch['port'] or '自动检测'} @ {ch['baudrate']} "
            f"{ch['bytesize']}{ch['parity']}{ch['stopbits']}"
        )

    def _on_serial_apply_clicked(self) -> None:
        link = self._link()
        svc = self.service("passthrough")
        if link is None or svc is None:
            return
        try:
            changes = self._serial_changes()
        except ValueError as exc:
            self.say(str(exc))
            return
        try:
            out = link.apply_passthrough_serial(**changes)
        except Exception as exc:  # noqa: BLE001
            self._fail("应用串口参数", exc)
            return
        detail = str(out.get("detail", ""))
        try:
            svc.cfg.save()
        except OSError as exc:
            self.log.warning("写回配置失败（本次仅内存生效）：%s", exc)
            detail += "；写回 app.yaml 失败，仅内存生效"
        ok = bool(out.get("ok"))
        self._append_row(f"{self._stamp()} 485 串口：{detail}", ok=ok)
        self._update_serial_label()
        self.say(detail)

    # ------------------------------------------------------------------ 生命周期
    def on_show(self) -> None:
        self._refresh_tree()

    def on_close(self) -> None:
        self._closing = True

    # ------------------------------------------------------------------ 桥事件
    def on_slot(self, topic: str) -> None:
        if topic == Events.CMD_RESULT:
            res = self.bridge.latest(topic)
            if isinstance(res, CommandResult):
                self._append_row(self._describe(res), ok=res.ok)
        elif topic == Events.TRANSPORT_STATE:
            # TRANSPORT_STATE 是合并类主题（桥文档表），发布者只有 link 服务：
            # kind=at 是 AT 线、kind=pt 是分线模式的透传串口线。本页"485 线"
            # 标签按模式只盯其中一条，另一条的状态变化与发送可用性无关。
            it = self.bridge.latest(topic)
            if not isinstance(it, dict) or it.get("kind") != ("pt" if self._watch_pt else "at"):
                return
            state = str(it.get("state") or "")
            if state == TransportState.OPEN.value:
                self._line_online = True
            elif state in (TransportState.CLOSED.value, TransportState.ERROR.value):
                self._line_online = False
            self._sync_buttons()

    # ------------------------------------------------------------------ 手动发送
    def _manual_kwargs(self) -> dict[str, Any]:
        svc = self.service("passthrough")
        cfg = svc.cfg.passthrough if svc is not None else None
        return {
            "mode": self.combo_mode.currentData(),
            "suffix": self.combo_suffix.currentData(),
            "crc16_append": self.check_crc.isChecked(),
            "timeout_ms": int(getattr(cfg, "reply_timeout_ms", 800) or 800),
        }

    def _on_manual_cfg_changed(self) -> None:
        """下拉框改动同步给引擎默认值，让"下次不带参数也这样发"成立。"""
        svc = self.service("passthrough")
        if svc is not None:
            try:
                svc.configure(
                    mode=self.combo_mode.currentData(),
                    suffix=self.combo_suffix.currentData(),
                )
            except Exception as exc:  # noqa: BLE001
                self._fail("同步透传默认值", exc)
        self._update_preview()

    def _update_preview(self) -> None:
        svc = self.service("passthrough")
        text = self.edit_payload.text()
        if svc is None:
            return
        if not text.strip():
            self.lbl_preview.setText("预览：等待输入")
            self.lbl_preview.setStyleSheet("color:#6b7482;")
            return
        try:
            out = svc.preview_manual(
                text,
                mode=self.combo_mode.currentData(),
                suffix=self.combo_suffix.currentData(),
                crc16_append=self.check_crc.isChecked(),
            )
        except Exception as exc:  # noqa: BLE001
            self._fail("预览", exc)
            return
        if out.get("ok"):
            self.lbl_preview.setText(f"预览（{out.get('detail', '')}）：{out.get('hex', '')}")
            self.lbl_preview.setStyleSheet("color:#1b7a43;")
        else:
            self.lbl_preview.setText(f"预览：{out.get('detail', '无法编码')}")
            self.lbl_preview.setStyleSheet("color:#b3261e;")

    def _on_send_clicked(self) -> None:
        svc = self.service("passthrough")
        payload = self.edit_payload.text()
        if svc is None or not payload.strip():
            return
        kw = self._manual_kwargs()
        try:
            out = svc.send(
                payload,
                mode=kw["mode"],
                suffix=kw["suffix"],
                crc16_append=kw["crc16_append"],
                note="手动发送",
                kind="manual",
            )
        except Exception as exc:  # noqa: BLE001
            self._fail("发送", exc)
            return
        if out.get("ok"):
            self._append_row(f"{self._stamp()} 手动发送: {out.get('detail', '已发出')}", ok=True)
        else:
            self.say(f"发送失败：{out.get('detail', '')}")

    def _on_request_clicked(self) -> None:
        svc = self.service("passthrough")
        payload = self.edit_payload.text()
        if svc is None or not payload.strip():
            return
        if self._busy:
            self.say("上一条指令还没结束，稍后再点")
            return
        kw = self._manual_kwargs()
        self._dispatch(
            lambda: svc.request(
                payload,
                mode=kw["mode"],
                suffix=kw["suffix"],
                crc16_append=kw["crc16_append"],
                timeout_ms=kw["timeout_ms"],
                name="手动等待",
            )
        )

    # ------------------------------------------------------------------ 后台下发
    def _dispatch(self, fn: Callable[[], CommandResult]) -> None:
        """等应答的下发入口：默认后台线程，测试可关线程同步执行。"""
        self._set_busy(True)
        if not self._threaded:
            self._on_worker_done(self._guard(fn))
            return
        threading.Thread(target=self._worker, args=(fn,), daemon=True).start()

    @staticmethod
    def _guard(fn: Callable[[], CommandResult]) -> CommandResult:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 线程里没人接异常，只能收口成失败
            return CommandResult(ts=time.time(), name="手动等待", ok=False, error=str(exc))

    def _worker(self, fn: Callable[[], CommandResult]) -> None:
        self.worker_done.emit(self._guard(fn))

    def _on_worker_done(self, res: object) -> None:
        if self._closing:
            return
        self._set_busy(False)
        if isinstance(res, CommandResult) and not res.ok:
            self.say(f"指令失败：{res.error}")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        svc_ready = self.lib is not None
        live = svc_ready and self._line_online and not self._busy
        if svc_ready:
            self.btn_send.setEnabled(self._line_online and not self._busy)
            self.btn_request.setEnabled(live)
            if self._line_online:
                self.lbl_line.setText("485 线：可发送")
                self.lbl_line.setStyleSheet("color:#1b7a43;")
            else:
                self.lbl_line.setText("485 线：未连通（发送已禁用）")
                self.lbl_line.setStyleSheet("color:#b3261e;font-weight:bold;")
        self.btn_up.setEnabled(svc_ready)
        self.btn_down.setEnabled(svc_ready)

    # ------------------------------------------------------------------ 模板树
    def _refresh_tree(self) -> None:
        if self.lib is None:
            return
        try:
            items = self.lib.ordered()
        except Exception as exc:  # noqa: BLE001
            self._fail("读取模板库", exc)
            return
        self.tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        for tpl in items:
            gname = tpl.group or "默认"
            top = groups.get(gname)
            if top is None:
                top = QTreeWidgetItem([gname, "", ""])
                groups[gname] = top
                self.tree.addTopLevelItem(top)
            enc = tpl.mode + ("+CRC" if tpl.crc16_append else "")
            reply = "等应答" if tpl.expect_reply else "只发送"
            leaf = QTreeWidgetItem([tpl.name or tpl.id, enc, reply])
            leaf.setData(0, _TID_ROLE, tpl.id)
            if not tpl.enabled:
                leaf.setText(0, f"{tpl.name or tpl.id}（停用）")
                for col in range(3):
                    leaf.setForeground(col, _DIM_COLOR)
            top.addChild(leaf)
        for top in groups.values():
            top.setExpanded(True)
        self.lbl_templates.setText(f"共 {len(items)} 条 · 文件 {self._templates_file()}")

    def _templates_file(self) -> str:
        return str(self.lib.path.name) if self.lib is not None and self.lib.path else "-"

    def _selected_id(self) -> str:
        item = self.tree.currentItem()
        if item is None:
            return ""
        tid = item.data(0, _TID_ROLE)
        return str(tid) if tid else ""

    def _on_tree_double(self, item: QTreeWidgetItem) -> None:
        tid = item.data(0, _TID_ROLE)
        if not tid or self.lib is None:
            return
        self._send_template(self.lib.get(str(tid)))

    def _send_template(self, tpl: CommandTemplate | None) -> None:
        if tpl is None or self.lib is None:
            return
        if not tpl.enabled:
            self.say("模板已停用，先在编辑框里勾选启用")
            return
        values: dict[str, Any] = {}
        names = missing_placeholders(tpl.payload)
        if names:
            got = self._ask_params(tpl, names)
            if got is None:
                return
            values = got
        svc = self.service("passthrough")
        if svc is None:
            return
        if not tpl.expect_reply:
            res = svc.run_template(tpl.id, values)
            self.say(self._describe(res))
            return
        if self._busy:
            self.say("上一条指令还没结束，稍后再点")
            return
        self._dispatch(lambda: svc.run_template(tpl.id, values))

    def _on_add_clicked(self) -> None:
        self._edit_via_dialog(None)

    def _on_edit_clicked(self) -> None:
        if self.lib is None:
            return
        tid = self._selected_id()
        if not tid:
            self.say("先在列表里选中一条模板（分组行不能编辑）")
            return
        self._edit_via_dialog(self.lib.get(tid))

    def _edit_via_dialog(self, tpl: CommandTemplate | None) -> None:
        if self.lib is None:
            return
        edited = self._open_editor(tpl)
        if edited is not None:
            self._apply_edit(edited)

    def _apply_edit(self, tpl: CommandTemplate, persist: bool = True) -> bool:
        if self.lib is None:
            return False
        problems = self.lib.validate(tpl)
        if problems:
            self.say("模板有问题：" + "；".join(problems))
            return False
        self.lib.upsert(tpl)
        if persist:
            self.lib.save()
        self._refresh_tree()
        return True

    def _on_remove_clicked(self) -> None:
        if self.lib is None:
            return
        tid = self._selected_id()
        if not tid:
            self.say("先在列表里选中一条模板")
            return
        self.lib.remove(tid)
        self.lib.save()
        self._refresh_tree()
        self.say(f"已删除模板 {tid}")

    def _on_move_clicked(self, delta: int) -> None:
        if self.lib is None:
            return
        tid = self._selected_id()
        if not tid:
            self.say("先在列表里选中一条模板")
            return
        self.lib.move(tid, delta)
        self.lib.save()
        self._refresh_tree()

    # ------------------------------------------------------------------ 对话框
    def _open_editor(self, tpl: CommandTemplate | None) -> CommandTemplate | None:
        """模态编辑对话框；offscreen 测试里这个方法通常被替换掉。"""
        dlg = _TemplateDialog(self.lib, tpl, self)
        if dlg.exec_() == QDialog.Accepted:
            return dlg.result_template()
        return None

    def _ask_params(self, tpl: CommandTemplate, names: list[str]) -> dict[str, str] | None:
        dlg = _ParamsDialog(self.lib, tpl, names, self)
        if dlg.exec_() == QDialog.Accepted:
            return dlg.values()
        return None

    # ------------------------------------------------------------------ 日志
    def _describe(self, res: CommandResult) -> str:
        try:
            return str(self.lib.describe_result(res))
        except Exception:  # noqa: BLE001
            head = time.strftime("%H:%M:%S", time.localtime(res.ts))
            label = res.name or res.template_id or "指令"
            return f"{head} {label}: {'OK' if res.ok else '失败'} {res.error}".strip()

    @staticmethod
    def _stamp() -> str:
        return time.strftime("%H:%M:%S", time.localtime())

    def _append_row(self, text: str, ok: bool | None = None) -> None:
        item = QListWidgetItem(text)
        item.setForeground(_OK_COLOR if ok else _BAD_COLOR if ok is False else _DIM_COLOR)
        self.log_view.addItem(item)
        while self.log_view.count() > _LOG_LIMIT:
            self.log_view.takeItem(0)
        self.log_view.scrollToBottom()


class _TemplateDialog(QDialog):
    """模板编辑：字段覆盖 CommandTemplate 全部可编辑项，保存先过 validate。"""

    def __init__(self, lib, tpl: CommandTemplate | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._lib = lib
        base = tpl if tpl is not None else CommandTemplate()
        self._is_new = tpl is None
        self._base_order = base.order
        self.setWindowTitle("编辑模板" if tpl is not None else "新增模板")
        self.setMinimumWidth(420)

        self.edit_id = QLineEdit(base.id)
        self.edit_id.setEnabled(self._is_new)
        self.edit_name = QLineEdit(base.name)
        self.edit_group = QLineEdit(base.group)
        self.edit_payload = QPlainTextEdit()
        self.edit_payload.setPlainText(base.payload)
        self.edit_payload.setMaximumHeight(72)
        self.combo_mode = QComboBox()
        self.combo_suffix = QComboBox()
        _combo_items(self.combo_mode, _MODES, base.mode)
        _combo_items(self.combo_suffix, _SUFFIXES, base.suffix)
        self.check_reply = QCheckBox("等待设备应答")
        self.check_reply.setChecked(base.expect_reply)
        self.spin_timeout = QSpinBox()
        self.spin_timeout.setRange(100, 120000)
        self.spin_timeout.setSingleStep(100)
        self.spin_timeout.setValue(max(100, int(base.reply_timeout_ms or 800)))
        self.edit_prefix = QLineEdit(base.match_prefix)
        self.spin_minlen = QSpinBox()
        self.spin_minlen.setRange(0, 4096)
        self.spin_minlen.setValue(int(base.min_reply_bytes or 0))
        self.check_crc = QCheckBox("追加 CRC16（先 CRC 后结束符）")
        self.check_crc.setChecked(bool(base.crc16_append))
        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(0, 60000)
        self.spin_interval.setSingleStep(100)
        self.spin_interval.setValue(int(base.interval_ms or 0))
        self.check_enabled = QCheckBox("启用")
        self.check_enabled.setChecked(bool(base.enabled))
        self.edit_note = QLineEdit(base.note)

        form = QFormLayout()
        form.addRow("ID（留空自动生成）", self.edit_id)
        form.addRow("名称", self.edit_name)
        form.addRow("分组", self.edit_group)
        form.addRow("payload", self.edit_payload)
        form.addRow("编码", self.combo_mode)
        form.addRow("结束符", self.combo_suffix)
        form.addRow("应答", self.check_reply)
        form.addRow("应答超时 ms", self.spin_timeout)
        form.addRow("应答前缀", self.edit_prefix)
        form.addRow("最少应答字节", self.spin_minlen)
        form.addRow("CRC16", self.check_crc)
        form.addRow("轮询间隔 ms", self.spin_interval)
        form.addRow("状态", self.check_enabled)
        form.addRow("备注", self.edit_note)

        self.lbl_err = QLabel("")
        self.lbl_err.setStyleSheet("color:#b3261e;")
        self.lbl_err.setWordWrap(True)
        self.lbl_preview = QLabel("")
        self.lbl_preview.setStyleSheet("color:#3d4a5c;")
        self.lbl_preview.setWordWrap(True)
        self.edit_payload.textChanged.connect(self._update_preview)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        box = QVBoxLayout(self)
        box.addLayout(form)
        box.addWidget(self.lbl_preview)
        box.addWidget(self.lbl_err)
        box.addWidget(buttons)

    def collect(self) -> CommandTemplate:
        return CommandTemplate(
            id=self.edit_id.text().strip(),
            name=self.edit_name.text().strip(),
            group=self.edit_group.text().strip(),
            payload=self.edit_payload.toPlainText(),
            mode=self.combo_mode.currentData(),
            suffix=self.combo_suffix.currentData(),
            expect_reply=self.check_reply.isChecked(),
            reply_timeout_ms=self.spin_timeout.value(),
            match_prefix=self.edit_prefix.text().strip(),
            min_reply_bytes=self.spin_minlen.value(),
            crc16_append=self.check_crc.isChecked(),
            interval_ms=self.spin_interval.value(),
            enabled=self.check_enabled.isChecked(),
            order=self._base_order,
            note=self.edit_note.text().strip(),
        )

    def result_template(self) -> CommandTemplate:
        tpl = self.collect()
        if not tpl.name:
            tpl.name = tpl.id or "未命名指令"
        if not tpl.group:
            tpl.group = getattr(getattr(self._lib, "cfg", None), "default_group", "默认")
        return tpl

    def _update_preview(self) -> None:
        try:
            data = self._lib.preview(self.collect(), self._placeholder_defaults())
        except Exception as exc:  # noqa: BLE001
            self.lbl_preview.setText(f"预览：{exc}")
            return
        self.lbl_preview.setText(f"上线字节（{len(data)}）：{bytes_to_hex(data)}")

    @staticmethod
    def _placeholder_defaults() -> dict[str, str]:
        return {}

    def _on_accept(self) -> None:
        problems = self._lib.validate(self.collect())
        if problems:
            self.lbl_err.setText("；".join(problems))
            return
        self.accept()


class _ParamsDialog(QDialog):
    """占位符参数框：边填边预览上线字节。"""

    def __init__(
        self,
        lib,
        tpl: CommandTemplate,
        names: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._lib = lib
        self._tpl = tpl
        self._edits: dict[str, QLineEdit] = {}
        self.setWindowTitle(f"参数 · {tpl.name or tpl.id}")
        form = QFormLayout()
        for name in names:
            edit = QLineEdit()
            edit.textChanged.connect(self._update_preview)
            self._edits[name] = edit
            form.addRow(name, edit)
        self.lbl_preview = QLabel("")
        self.lbl_preview.setStyleSheet("color:#3d4a5c;")
        self.lbl_preview.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        box = QVBoxLayout(self)
        box.addLayout(form)
        box.addWidget(self.lbl_preview)
        box.addWidget(buttons)
        self._update_preview()

    def values(self) -> dict[str, str]:
        return {name: edit.text() for name, edit in self._edits.items()}

    def _filled_values(self) -> dict[str, str]:
        """validate 把"键存在"视为已给值，空串会被放过——空白一律按未填处理。"""
        return {name: text for name, text in self.values().items() if text.strip()}

    def _update_preview(self) -> None:
        vals = self._filled_values()
        try:
            problems = self._lib.validate(self._tpl, vals)
        except Exception as exc:  # noqa: BLE001
            self.lbl_preview.setText(f"预览：{exc}")
            return
        if problems:
            self.lbl_preview.setStyleSheet("color:#b3261e;")
            self.lbl_preview.setText("还需填写：" + "；".join(problems))
            return
        data = self._lib.preview(self._tpl, vals)
        self.lbl_preview.setStyleSheet("color:#1b7a43;")
        self.lbl_preview.setText(f"将发送 {len(data)} 字节：{bytes_to_hex(data)}")

    def _on_accept(self) -> None:
        problems = self._lib.validate(self._tpl, self._filled_values())
        if problems:
            self.lbl_preview.setStyleSheet("color:#b3261e;")
            self.lbl_preview.setText("；".join(problems))
            return
        self.accept()
