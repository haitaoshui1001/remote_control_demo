"""信道监控页：RSSI / EVM 历史曲线 + 最新链路快照 + STA 列表。

``LINK_SNAPSHOT`` 是合并类主题（桥只留最新），曲线却需要历史——所以采取
"初始回填 + 按 ts 增量"：进页时用 ``LinkMonitor.history()`` 回填已有快照，
之后每次 ``on_slot`` 取最新值，只有 ``ts`` 比队尾新才入曲线，重复投递天然去重。
手动"读取一次"即便触发服务层 publish，同一快照也会被 ts 去重挡住，不会画两个点。

RSSI / EVM 共用一条时间轴、各用一条独立纵轴（左 RSSI、右 EVM），阈值横线取自
配置（``link.rssi_warn / rssi_crit / evm_warn``），颜色分档与告警评估保持同一
套数值，界面不会和告警"打架"。STA 行数很少，每次快照全量重建表格，简单可靠。
"""

from __future__ import annotations

import time

from PyQt5.QtCore import QPointF, QRect, Qt
from PyQt5.QtGui import QColor, QPainter, QPen, QPolygonF
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.events import Events
from ...core.models import LinkSnapshot
from ..theme import mono_font, small_font
from .base import BasePage

__all__ = ["ChannelPage"]

_GREEN = QColor("#1b7a43")
_ORANGE = QColor("#b25a00")
_RED = QColor("#b3261e")
_BLUE = QColor("#0b57d0")
_DIM = QColor("#6b7482")
_TICK = QColor("#3c4450")
_GRID = QColor("#d5dbe3")

_RSSI_LO, _RSSI_HI = -100.0, -40.0
_EVM_LO, _EVM_HI = -40.0, -10.0
_MAX_POINTS = 1200

_STA_COLS = ("序号", "MAC", "RSSI dBm", "EVM dB", "TX MCS", "RX MCS", "收包", "发包", "丢包")


def _mcs_text(value: int | None) -> str:
    """手册 6.3.17：0~7 或 10（1M 模式）为固定速率，其他值（默认 255）是速率自动调整。"""
    if value is None:
        return "—"
    if 0 <= value <= 7 or value == 10:
        return f"MCS{value}"
    return f"自适应(MCS{value})"


class _CurveWidget(QWidget):
    """双纵轴折线：绿线 RSSI（左轴），蓝线 EVM（右轴）。

    纵轴量程按数据自适应（各扩 10% 边距）：模块上报的 RSSI 语义未定
    （负 dBm 或 0~100 指数都可能），固定量程会把贴边数据压成一条直线。
    """

    def __init__(self, rssi_warn: float, rssi_crit: float, evm_warn: float,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pts: list[tuple[float, float | None, float | None]] = []
        self._rssi_warn = rssi_warn
        self._rssi_crit = rssi_crit
        self._evm_warn = evm_warn
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def points(self) -> list[tuple[float, float | None, float | None]]:
        return list(self._pts)

    def add(self, ts: float, rssi: float | None, evm: float | None) -> bool:
        """按时间入点；不比队尾新的（重复投递）直接忽略。"""
        if self._pts and ts <= self._pts[-1][0]:
            return False
        self._pts.append((ts, rssi, evm))
        if len(self._pts) > _MAX_POINTS:
            del self._pts[: len(self._pts) - _MAX_POINTS]
        self.update()
        return True

    def reset(self, snaps: list[LinkSnapshot]) -> None:
        self._pts = [
            (s.ts, s.rssi_dbm, s.evm_db) for s in snaps if isinstance(s, LinkSnapshot)
        ][-_MAX_POINTS:]
        self.update()

    def clear_points(self) -> None:
        self._pts = []
        self.update()

    @staticmethod
    def _y(value: float, lo: float, hi: float, top: float, bottom: float) -> float:
        frac = (value - lo) / (hi - lo)
        frac = max(0.0, min(1.0, frac))
        return bottom + frac * (top - bottom)

    @staticmethod
    def _span(values: list[float], default_lo: float, default_hi: float) -> tuple[float, float]:
        """按数据取纵轴量程：min/max 各扩 10% 边距；近似直线时先撑开 ±5 再留边。"""
        if not values:
            return default_lo, default_hi
        lo, hi = min(values), max(values)
        if hi - lo < 1.0:
            lo, hi = lo - 5.0, hi + 5.0
        pad = (hi - lo) * 0.1
        return lo - pad, hi + pad

    @staticmethod
    def _ts_at(pts: list[tuple[float, float | None, float | None]], frac: float) -> float:
        """横轴按点序均匀分布，把格线位置换算回该处的时间戳（线性插值）。"""
        n = len(pts)
        if n < 2:
            return pts[0][0] if pts else time.time()
        pos = max(0.0, min(1.0, frac)) * (n - 1)
        i = min(int(pos), n - 2)
        k = pos - i
        return pts[i][0] + (pts[i + 1][0] - pts[i][0]) * k

    @staticmethod
    def _time_box(x: float, bottom: float, w: int, h: int, fh: int, bw: int) -> QRect:
        """时间刻度文字框：高=实际行高、宽=实际文字宽（都由 fontMetrics 传入，
        写死尺寸会在高分屏缩放下被矩形裁字），顶缘压到轴值最低一行下缘之下，
        底缘与左右缘都钳进窗口内。"""
        bx = max(0, min(int(x) - bw // 2, w - bw))
        by = min(int(bottom) + fh // 2 + 2, h - fh - 1)
        return QRect(bx, by, bw, fh)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        self._paint(painter, self.width(), self.height())

    def _paint(self, painter: QPainter, w: int, h: int) -> None:
        """全部绘制逻辑收进 _paint：测试可用离屏 QPixmap 的 painter 直调。"""
        painter.setRenderHint(QPainter.Antialiasing)
        # 字体先设好：所有文字框/边距一律按 fontMetrics 实际度量算。
        # drawText(QRect) 按矩形裁剪，写死高 16px、宽 84px 时，高分屏缩放下
        # Consolas 实际行高/字宽超出框，时间轴就被裁得"看不见"。
        font = small_font()
        painter.setFont(font)
        fm = painter.fontMetrics()
        fh = fm.height()
        tw = fm.horizontalAdvance("00:00:00") + 8  # 时间刻度框宽（含左右余量）
        rlo, rhi = self._span(
            [r for _ts, r, _e in self._pts if r is not None], _RSSI_LO, _RSSI_HI
        )
        elo, ehi = self._span(
            [e for _ts, _r, e in self._pts if e is not None], _EVM_LO, _EVM_HI
        )
        # 边距随最宽刻度文字/行高动态撑开：上给图例、下给轴值+时间两行、
        # 左右给轴刻度值，任何字号 DPI 下都不贴边裁字。
        lw = max(fm.horizontalAdvance(f"{rhi - (rhi - rlo) * i / 4:g}") for i in range(5))
        ew = max(fm.horizontalAdvance(f"{ehi - (ehi - elo) * i / 4:g}") for i in range(5))
        left, right = max(46, lw + 8), w - max(64, ew + 12)
        top, bottom = max(26, fh + 6), h - max(40, 2 * fh + 8)
        if right <= left:  # 极窄窗口兜底：绘图区不翻转
            right = max(left + 1, w - 1)
        painter.fillRect(0, 0, w, h, QColor("#fbfcfe"))

        pen = QPen(_GRID)
        pen.setWidth(1)
        painter.setPen(pen)
        for i in range(5):
            y = top + (bottom - top) * i / 4
            painter.drawLine(int(left), int(y), int(right), int(y))
        for i in range(7):
            x = left + (right - left) * i / 6
            painter.drawLine(int(x), int(top), int(x), int(bottom))

        if len(self._pts) >= 2:
            step = (right - left) / (len(self._pts) - 1)
            rpts: list[tuple[float, float]] = []
            epts: list[tuple[float, float]] = []
            for i, (_ts, rssi, evm) in enumerate(self._pts):
                x = left + step * i
                if rssi is not None:
                    rpts.append((x, self._y(rssi, rlo, rhi, top, bottom)))
                if evm is not None:
                    epts.append((x, self._y(evm, elo, ehi, top, bottom)))

            # 阈值虚线只画落在当前量程内的：量程自适应后 -85/-92 可能在视野外。
            for value, color in ((self._rssi_warn, _ORANGE), (self._rssi_crit, _RED)):
                if not rlo <= value <= rhi:
                    continue
                dash = QPen(color)
                dash.setStyle(Qt.DashLine)
                painter.setPen(dash)
                y_t = self._y(value, rlo, rhi, top, bottom)
                painter.drawLine(int(left), int(y_t), int(right), int(y_t))

            if len(rpts) >= 2:
                pen = QPen(_GREEN)
                pen.setWidth(2)
                painter.setPen(pen)
                painter.drawPolyline(QPolygonF([QPointF(x, y) for x, y in rpts]))
            if len(epts) >= 2:
                pen = QPen(_BLUE)
                pen.setWidth(2)
                painter.setPen(pen)
                painter.drawPolyline(QPolygonF([QPointF(x, y) for x, y in epts]))

        # 左右轴都画满 5 档格线值：只标端点时中间格线没法读数，
        # 文字框宽/高都按 fontMetrics 度量留足，画在格线右侧 5px 处。
        for i in range(5):
            y = top + (bottom - top) * i / 4
            r_val = rhi - (rhi - rlo) * i / 4
            e_val = ehi - (ehi - elo) * i / 4
            painter.setPen(_GREEN)
            painter.drawText(
                QRect(0, int(y) - fh // 2, int(left) - 4, fh),
                Qt.AlignRight | Qt.AlignVCenter,
                f"{r_val:g}",
            )
            painter.setPen(_BLUE)
            painter.drawText(
                QRect(int(right) + 5, int(y) - fh // 2, max(40, w - int(right) - 7), fh),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"{e_val:g}",
            )

        # 横轴时间刻度：与竖格线对齐；格距放不下 HH:MM:SS 实际字宽时隔线抽稀。
        # 起笔压到轴值最低一行下缘之下，框宽=文字实宽，保证整字完整。
        n = len(self._pts)
        if n >= 2:
            painter.setPen(_TICK)
            gap = (right - left) / 6
            every = 1 if gap >= tw + 6 else 3
            for i in range(0, 7, every):
                frac = i / 6
                x = left + gap * i
                stamp = time.strftime(
                    "%H:%M:%S", time.localtime(self._ts_at(self._pts, frac))
                )
                painter.drawText(
                    self._time_box(x, bottom, w, h, fh, tw),
                    Qt.AlignHCenter | Qt.AlignVCenter,
                    stamp,
                )

        # 图例移到绘图区顶部：底部空间让给时间刻度，两行不再叠字。
        painter.setPen(_DIM)
        painter.drawText(
            QRect(int(left), 2, max(60, int(right) - int(left)), fh),
            Qt.AlignLeft | Qt.AlignVCenter,
            "RSSI dBm（左） · EVM dB（右）",
        )
        if n < 2:
            painter.drawText(
                int(w / 2) - 80, int(h / 2), "等待遥测快照（至少 2 个点才画线）"
            )


class ChannelPage(BasePage):
    """信道监控页：RSSI/EVM 曲线 + 链路遥测 + STA 列表。"""

    page_key = "channel"
    title = "信道监控"

    def __init__(self, session, bridge, parent: QWidget | None = None) -> None:
        super().__init__(session, bridge, parent)
        link_cfg = getattr(getattr(session, "cfg", None), "link", None)
        self._rssi_warn = float(getattr(link_cfg, "rssi_warn", -85.0))
        self._rssi_crit = float(getattr(link_cfg, "rssi_crit", -92.0))
        self._evm_warn = float(getattr(link_cfg, "evm_warn", -20.0))
        self._build_widgets()
        self._prime_history()

    # ------------------------------------------------------------------ UI 搭建
    def _build_widgets(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.btn_once = QPushButton("读取一次")
        self.btn_start = QPushButton("开始轮询")
        self.btn_stop = QPushButton("停止轮询")
        self.btn_clear = QPushButton("清空曲线")
        self.chk_follow = QCheckBox("曲线跟随刷新")
        self.chk_follow.setChecked(True)
        for b in (self.btn_once, self.btn_start, self.btn_stop, self.btn_clear):
            bar.addWidget(b)
        bar.addWidget(self.chk_follow)
        bar.addStretch(1)
        self.endpoint_label = QLabel("")
        self.endpoint_label.setStyleSheet(f"color:{_DIM.name()};")
        bar.addWidget(self.endpoint_label)
        root.addLayout(bar)

        self.curve = _CurveWidget(self._rssi_warn, self._rssi_crit, self._evm_warn)
        root.addWidget(self.curve, 2)

        grid = QHBoxLayout()
        grid.setSpacing(10)
        info = QVBoxLayout()
        info.setSpacing(3)
        self.lbl_link = QLabel("链路：未知")
        self.lbl_metrics = QLabel("RSSI — · EVM —")
        self.lbl_radio = QLabel("频点 — · 带宽 — · MCS —")
        self.lbl_health = QLabel("")
        self.lbl_ts = QLabel("等待快照")
        for lbl in (self.lbl_link, self.lbl_metrics, self.lbl_radio, self.lbl_health, self.lbl_ts):
            info.addWidget(lbl)
        info.addStretch(1)
        grid.addLayout(info, 1)

        self.sta_table = QTableWidget(0, len(_STA_COLS))
        self.sta_table.setHorizontalHeaderLabels(_STA_COLS)
        self.sta_table.verticalHeader().setVisible(False)
        self.sta_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.sta_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sta_table.setAlternatingRowColors(True)
        # 等宽铺满视口（Stretch）：按内容伸缩会让各列忽宽忽窄、还挤掉右侧列；
        # 小一号字体提高密度，MAC 等被截断的单元格用 tooltip 兜底。
        self.sta_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.sta_table.setFont(small_font())
        grid.addWidget(self.sta_table, 2)
        root.addLayout(grid, 1)

        self.lbl_metrics.setFont(mono_font())
        self.lbl_radio.setFont(mono_font())
        # 手册 6.3.12：EVM 只在 AP 侧 AT+STA_INFO 的 LMAC 统计里上报，
        # 本机为 STA 时固件没有 EVM 数据源，"EVM —"属正常而非故障。
        self.lbl_metrics.setToolTip(
            "EVM（调制误差）仅随 AT+STA_INFO 从 AP 侧上报（手册 6.3.12）；"
            "本机为 STA 时无数据来源，显示 — 属正常。"
        )

        self.btn_once.clicked.connect(self._on_once)
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_clear.clicked.connect(self._on_clear)

        if self.service("radio") is None and self.service("link") is None:
            for b in (self.btn_once, self.btn_start, self.btn_stop):
                b.setEnabled(False)
            self.endpoint_label.setText("链路服务未启用")
            self.endpoint_label.setStyleSheet(f"color:{_RED.name()};")

    # ------------------------------------------------------------------ 数据入口
    def _prime_history(self) -> None:
        radio = self.service("radio")
        if radio is None:
            return
        try:
            snaps = radio.history()
        except Exception:  # noqa: BLE001 - 回填失败不应阻塞页面构建
            snaps = []
        if snaps:
            self.curve.reset(snaps)
        link = self.service("link")
        if link is not None:
            self.endpoint_label.setText(
                f"端点 {link.endpoint} · 通道 {link.channel}"
            )
        last = self.bridge.latest(Events.LINK_SNAPSHOT)
        if isinstance(last, LinkSnapshot):
            self._render(last)

    def on_slot(self, topic: str) -> None:
        if topic != Events.LINK_SNAPSHOT:
            return
        snap = self.bridge.latest(topic)
        if isinstance(snap, LinkSnapshot):
            self._ingest(snap)

    def _ingest(self, snap: LinkSnapshot) -> None:
        if self.chk_follow.isChecked():
            self.curve.add(snap.ts, snap.rssi_dbm, snap.evm_db)
        self._render(snap)

    # ------------------------------------------------------------------ 渲染
    def _render(self, snap: LinkSnapshot) -> None:
        if snap.connected is True:
            self.lbl_link.setText(f"链路：已连接（{snap.role or '?'}）")
            self.lbl_link.setStyleSheet(f"color:{_GREEN.name()};")
        elif snap.connected is False:
            self.lbl_link.setText("链路：未连接")
            self.lbl_link.setStyleSheet(f"color:{_RED.name()};")
        else:
            self.lbl_link.setText("链路：状态未知")
            self.lbl_link.setStyleSheet(f"color:{_DIM.name()};")

        rssi_txt = "—" if snap.rssi_dbm is None else f"{snap.rssi_dbm:.1f} dBm"
        evm_txt = "—" if snap.evm_db is None else f"{snap.evm_db:.1f} dB"
        self.lbl_metrics.setText(f"RSSI {rssi_txt} · EVM {evm_txt}")
        if snap.rssi_dbm is None:
            self.lbl_metrics.setStyleSheet("")
        elif snap.rssi_dbm >= self._rssi_warn:
            self.lbl_metrics.setStyleSheet(f"color:{_GREEN.name()};")
        elif snap.rssi_dbm >= self._rssi_crit:
            self.lbl_metrics.setStyleSheet(f"color:{_ORANGE.name()};")
        else:
            self.lbl_metrics.setStyleSheet(f"color:{_RED.name()};")

        freq = "—" if snap.freq_mhz is None else f"{snap.freq_mhz:g} MHz"
        bw = "—" if snap.bandwidth_mhz is None else f"{snap.bandwidth_mhz:g} MHz"
        mcs = _mcs_text(snap.tx_mcs)
        power = "—" if snap.txpower_dbm is None else f"{snap.txpower_dbm:g} dBm"
        self.lbl_radio.setText(f"频点 {freq} · 带宽 {bw} · 速率 {mcs} · 功率 {power}")

        if snap.degraded or snap.errors:
            errs = "；".join(str(e) for e in snap.errors[:3])
            self.lbl_health.setText(f"⚠ 退化：{errs or '指标低于阈值'}")
            self.lbl_health.setStyleSheet(f"color:{_RED.name()};")
        else:
            self.lbl_health.setText("指标正常")
            self.lbl_health.setStyleSheet(f"color:{_DIM.name()};")

        stamp = time.strftime("%H:%M:%S", time.localtime(snap.ts))
        self.lbl_ts.setText(
            f"快照 {stamp} · STA {snap.sta_count} · 曲线 {len(self.curve.points())} 点"
        )

        self._render_stas(snap)

    def _render_stas(self, snap: LinkSnapshot) -> None:
        rows = list(snap.sta_list)
        self.sta_table.clearSpans()
        if not rows:
            # 说明书 6.3.12：STA_INFO 仅 AP 可用。本机是 STA 时拿不到从机列表，
            # 空表属正常，给一行提示免得被当成故障。
            hint = (
                "本机为 STA：从机列表由 AP 侧查询（AT+STA_INFO），此处无数据属正常"
                if snap.role == "sta"
                else "暂无从机上报（等待 STA 入网）"
            )
            self.sta_table.setRowCount(1)
            self.sta_table.setSpan(0, 0, 1, len(_STA_COLS))
            item = QTableWidgetItem(hint)
            item.setFlags(Qt.NoItemFlags)
            item.setForeground(_DIM)
            self.sta_table.setItem(0, 0, item)
            return
        self.sta_table.setRowCount(len(rows))
        for i, sta in enumerate(rows):
            vals = (
                sta.index,
                sta.mac,
                "" if sta.rssi_dbm is None else f"{sta.rssi_dbm:.1f}",
                "" if sta.evm_db is None else f"{sta.evm_db:.1f}",
                sta.tx_mcs,
                sta.rx_mcs,
                sta.rx_packets,
                sta.tx_packets,
                sta.lost_packets,
            )
            for col, val in enumerate(vals):
                item = QTableWidgetItem(str(val))
                if col != 1:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                item.setToolTip(str(val))  # 等宽列截断时悬停可看全值
                self.sta_table.setItem(i, col, item)

    # ------------------------------------------------------------------ 动作
    def _on_once(self) -> None:
        radio = self.service("radio")
        if radio is None:
            return
        snap = radio.poll_once()
        if isinstance(snap, LinkSnapshot):
            self._ingest(snap)
            self.say("已读取一次链路遥测")
        else:
            self.say("读取失败：通道未开或模块无响应", 8000)

    def _on_start(self) -> None:
        link = self.service("link")
        if link is None:
            return
        if link.start_polling():
            self.say("周期遥测轮询已启动")
        else:
            self.say("轮询启动失败（通道未开或配置关闭）", 8000)

    def _on_stop(self) -> None:
        link = self.service("link")
        if link is None:
            return
        link.stop_polling()
        self.say("周期遥测轮询已停止")

    def _on_clear(self) -> None:
        self.curve.clear_points()
        self.lbl_ts.setText("曲线已清空，等待新快照")
