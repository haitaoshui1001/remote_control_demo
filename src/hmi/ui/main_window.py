"""主窗口：左侧导航 + 页面栈 + 状态栏四盏灯。

状态栏只做一件事——把 services 的 summary() 翻译成颜色和文字，不参与判断、不自己
统计。四盏灯对应四个"能不能干活"的前置条件：

1. **信道**：AT/透传共用的那条字节通道开没开（串口被占用、UDP 未就绪都在这里显形）；
2. **遥测**：轮询节拍在跑、且拿到的快照还新鲜（``stale`` / ``age_s``）；
3. **视频**：取流线程在跑、且真的收到帧（连着但没画面是"警告"而不是"正常"）；
4. **落盘**：流水目录可写；正在录像时单独用红色提示，退出前会据此拦一道。

灯灭不代表软件坏了——现场就是允许"串口没插、摄像头没通电"继续用其他功能的，
所以标题栏和灯都只陈述事实，不弹窗。
"""

from __future__ import annotations

from typing import Any

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QShortcut,
    QStackedWidget,
    QStatusBar,
    QWidget,
)

from .. import __version__
from ..core.events import Events
from ..core.logger import get_logger
from .pages import PAGES, BasePage, create_page
from .theme import nav_width_px

__all__ = ["Lamp", "MainWindow"]

_DOT_COLORS = {
    "ok": "#2e9e5b",
    "warn": "#d99100",
    "bad": "#d0433b",
    "rec": "#d0433b",
    "idle": "#9aa4b0",
}


class Lamp(QLabel):
    """状态栏上的一盏灯：一个彩色圆点 + 短语，详情放 tooltip。"""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.label = label
        self.level = "idle"
        self.detail = ""
        self.setTextFormat(Qt.RichText)
        self.set_state("idle", "—")

    def set_state(self, level: str, text: str = "", tip: str = "") -> None:
        self.level = level if level in _DOT_COLORS else "idle"
        self.detail = text
        color = _DOT_COLORS[self.level]
        body = f'{self.label}&nbsp;<b>{text}</b>' if text else self.label
        self.setText(f'<span style="color:{color}">&#9679;</span>&nbsp;{body}')
        self.setToolTip(tip or f"{self.label}：{text or level}")


class MainWindow(QMainWindow):
    """装配导航与状态栏；页面由 ``pages`` 注册表提供。"""

    def __init__(self, session: Any, bridge: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.bridge = bridge
        self.log = get_logger("hmi.ui.main")
        self.pages: dict[str, BasePage] = {}

        cfg = session.cfg
        self.setObjectName("MainWindow")
        self.setWindowTitle(self._title())
        geo = list(cfg.ui.geometry or [1280, 800, 60, 40])
        while len(geo) < 4:
            geo.append(40)
        self.resize(int(geo[0]), int(geo[1]))
        self.move(int(geo[2]), int(geo[3]))

        central = QWidget(self)
        central.setObjectName("Central")
        row = QHBoxLayout(central)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        self.nav = QListWidget(central)
        self.nav.setObjectName("Nav")
        self.nav.setFixedWidth(nav_width_px())
        self.nav.currentRowChanged.connect(self._on_row)
        for index, spec in enumerate(PAGES):
            item = QListWidgetItem(f"{index + 1}  {spec.title}", self.nav)
            item.setToolTip(spec.note)
            page = create_page(spec, session, bridge)
            self.pages[spec.key] = page
        self.stack = QStackedWidget(central)
        for spec in PAGES:
            self.stack.addWidget(self.pages[spec.key])
        row.addWidget(self.nav)
        row.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        bar = QStatusBar(self)
        bar.setObjectName("Status")
        self.setStatusBar(bar)
        self.lamp_channel = Lamp("信道")
        self.lamp_poll = Lamp("遥测")
        self.lamp_video = Lamp("视频")
        self.lamp_record = Lamp("落盘")
        for lamp in (self.lamp_channel, self.lamp_poll, self.lamp_video, self.lamp_record):
            lamp.setMargin(2)
            bar.addWidget(lamp)
        self.bus_label = QLabel("")
        self.bus_label.setObjectName("BusLabel")
        bar.addPermanentWidget(self.bus_label)

        for index, _spec in enumerate(PAGES):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self)
            shortcut.activated.connect(lambda i=index: self.select_index(i))

        bridge.slot_updated.connect(self._on_slot)
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self.refresh_status)
        self._status_timer.start()

        start = cfg.ui.start_page if cfg.ui.start_page in self.pages else PAGES[0].key
        self.select_page(start)
        self.refresh_status()

    # ------------------------------------------------------------------ 标题
    def _title(self) -> str:
        mode = "仿真模式" if self.session.mock else "实机模式"
        base = str(self.session.cfg.app.title or "EWD611 数传图传上位机控制台")
        return f"{base} · {mode} · v{__version__}"

    def refresh_title(self) -> None:
        self.setWindowTitle(self._title())

    # ------------------------------------------------------------------ 导航
    def select_page(self, key: str) -> None:
        for index, spec in enumerate(PAGES):
            if spec.key == key:
                self.select_index(index)
                return

    def select_index(self, index: int) -> None:
        if 0 <= index < self.nav.count():
            self.nav.setCurrentRow(index)

    def _on_row(self, index: int) -> None:
        if 0 <= index < self.stack.count():
            self.stack.setCurrentIndex(index)

    @property
    def current_page(self) -> BasePage | None:
        widget = self.stack.currentWidget()
        return widget if isinstance(widget, BasePage) else None

    def _on_slot(self, topic: str) -> None:
        # 通道/视频状态一变就重画灯，其余主题等 1 秒节拍；避免每秒几十次全量刷新。
        if topic in (Events.TRANSPORT_STATE, Events.VIDEO_STATS, Events.LINK_SNAPSHOT):
            self.refresh_status()

    # ------------------------------------------------------------------ 状态栏
    def refresh_status(self) -> None:
        session = self.session
        link = session.link
        info = link.summary()
        if info["open"]:
            self.lamp_channel.set_state(
                "ok",
                str(info["channel"]),
                f"通道 {info['channel']} 已打开；AT 发送 {info.get('at_sent')} 条，"
                f"错误 {info.get('at_error')}，超时 {info.get('at_timeout')}",
            )
        else:
            self.lamp_channel.set_state(
                "bad",
                f"{info['channel']} 未开",
                str(info.get("error") or "控制线未打开：电台配置与 485 透传都不可用"),
            )

        if link.polling and info["open"]:
            stale = bool(info.get("stale"))
            age = float(info.get("age_s") or 0.0)
            self.lamp_poll.set_state(
                "warn" if stale else "ok",
                f"{int(info.get('polls') or 0)} 次" + ("· 停更" if stale else ""),
                f"最后一次遥测快照距今 {age:.1f}s；停更表示通道开着但一直没回数据",
            )
        else:
            self.lamp_poll.set_state("idle", "未轮询", "信道打开后自动开始轮询")

        video = session.video
        if video is None:
            self.lamp_video.set_state("idle", "未装配", "本次会话没有装视频服务")
        elif video.running:
            stats = video.summary()
            if stats["connected"]:
                fps = float(stats.get("fps") or 0.0)
                self.lamp_video.set_state(
                    "ok",
                    f"{stats.get('resolution') or '?'} · {fps:.0f}fps",
                    f"{stats.get('codec')} 流 #{stats.get('stream_index')}；"
                    f"重连 {stats.get('reconnects')} 次；{stats.get('url')}",
                )
            else:
                self.lamp_video.set_state("warn", "取流中", "线程在跑但还没收到帧")
        else:
            self.lamp_video.set_state(
                "bad", "已停", str(video.info().get("start_error") or "取流线程未运行")
            )

        record = session.record
        if record is None:
            self.lamp_record.set_state("idle", "未装配", "本次会话没有装落盘服务")
        elif record.recording:
            self.lamp_record.set_state(
                "rec", "录像中", f"流水 {record.rows} 行，抓拍 {record.snapshots} 张"
            )
        elif record.started:
            self.lamp_record.set_state(
                "ok", f"{record.rows} 行", f"抓拍 {record.snapshots} 张，丢弃 {record.dropped} 条"
            )
        else:
            self.lamp_record.set_state("bad", "未落盘", "落盘目录不可写或起不来，本次不记流水")

        stats = self.bridge.stats()
        dropped = sum(int(v) for v in stats["dropped"].values())
        self.bus_label.setText(
            f"总线 {stats['bus_published']} 条 · UI {stats['interval_ms']}ms"
            + (f" · 丢弃 {dropped}" if dropped else "")
        )

    # ------------------------------------------------------------------ 关闭
    def closeEvent(self, event) -> None:  # noqa: N802
        self._status_timer.stop()
        record = self.session.record
        if record is not None and record.recording:
            answer = QMessageBox.question(
                self,
                "正在录像",
                "录像还在进行，退出会截断当前录像文件。确定退出吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        elif self.session.cfg.ui.confirm_exit:
            answer = QMessageBox.question(
                self,
                "退出",
                "确定退出上位机？退出会关闭串口与取流线程。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        for page in self.pages.values():
            try:
                page.on_close()
            except Exception as exc:  # noqa: BLE001
                self.log.exception("页面 %s 关闭异常：%s", page.title, exc)
        event.accept()

    def __repr__(self) -> str:
        return f"<MainWindow pages={len(self.pages)} channel={self.session.link.channel}>"
