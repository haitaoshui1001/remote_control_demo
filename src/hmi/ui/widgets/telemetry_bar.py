"""遥测状态栏：视频页底部的主要遥测数字 + 迷你曲线。

数据只有一个入口——桥上合并类主题 ``robot/telemetry`` 的最新载荷
（``{"sample", "stale", "age_s"}``）：状态栏按需刷新，曲线按 ``ts`` 增量入点，
重复投递天然去重，和信道页曲线一个路子。

控件不碰服务：导出按钮只发 ``exported`` 信号，落盘与另存由页面处理，
保持"控件 → 页面 → 服务"的单向依赖。缺数据的字段一律显示 ``--``，
不猜、不补零——把"没数"和"是零"分清楚比填满格子重要。
"""

from __future__ import annotations

import time

from PyQt5.QtCore import QPointF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFontMetrics, QPainter, QPen, QPolygonF
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...protocol.telemetry_frames import TelemetrySample
from ..theme import label_px, mono_font, small_font

__all__ = ["TelemetryBar"]

_GREEN = QColor("#1b7a43")
_ORANGE = QColor("#b25a00")
_RED = QColor("#b3261e")
_BLUE = QColor("#0b57d0")
_PURPLE = QColor("#6a3fa0")
_DIM = QColor("#6b7482")
_FAINT = QColor("#9aa3ad")
_GRID = QColor("#d5dbe3")
_INK = QColor("#1f2937")

_TEXT_DIM = f"color:#6b7482; font-size:{label_px()}px;"
_TEXT_NORMAL = f"color:#1f2937; font-size:{label_px()}px;"
_TEXT_STALE = f"color:#9aa3ad; font-size:{label_px()}px;"
_TEXT_ALERT = f"color:#b3261e; font-size:{label_px()}px; font-weight:bold;"

_FIELD_STATUS = "状态"
_FIELD_VOLTAGE = "电压"
_FIELD_CURRENT = "电流"
_FIELD_SOC = "电量"
_FIELD_SPEED = "速度"
_FIELD_VX = "x向速度"
_FIELD_POS = "位置"
_FIELD_GPS = "定位"

_CELL_ORDER = (_FIELD_STATUS, _FIELD_VOLTAGE, _FIELD_CURRENT, _FIELD_SOC,
               _FIELD_SPEED, _FIELD_POS, _FIELD_GPS)

_SERIES_META = {
    "voltage": (_FIELD_VOLTAGE, _GREEN, "V", 1),
    "speed": (_FIELD_SPEED, _BLUE, "m/s", 2),
    "current": (_FIELD_CURRENT, _ORANGE, "A", 1),
    "soc": (_FIELD_SOC, _PURPLE, "%", 0),
    "vx": (_FIELD_VX, _BLUE, "m/s", 2),
}


def _fmt(value: float | None, digits: int, unit: str = "") -> str:
    if value is None:
        return "--"
    return f"{value:.{digits}f}{unit}"


def _legend_layout(fm: QFontMetrics, texts: list[str], x0: float, gap: float = 14.0) -> list[float]:
    """按文字实际渲染宽度排布图例左缘。

    旧实现按"每字符 8px"估宽，中文实际宽得多，两条图例直接叠在一起；
    改成 QFontMetrics 实测 + 固定间隔，中英文混排都不会碰。
    """
    xs: list[float] = []
    x = float(x0)
    for t in texts:
        xs.append(x)
        x += fm.horizontalAdvance(t) + gap
    return xs


def _scale_range(values: list[float], pad_frac: float = 0.08,
                 min_span: float = 0.0) -> tuple[float, float]:
    """序列独立量程：极值各撑 8% 余量；常量序列先张开 ±1，避免除零；
    整轴跨度不足 min_span 时绕中点对称张开。"""
    lo = min(values)
    hi = max(values)
    if hi - lo < 1e-9:
        lo, hi = lo - 1.0, hi + 1.0
    pad = (hi - lo) * pad_frac
    lo -= pad
    hi += pad
    if hi - lo < min_span:
        mid = (lo + hi) / 2
        lo, hi = mid - min_span / 2, mid + min_span / 2
    return lo, hi


#: 纵轴每格最小量程：电压刻度只有 1 位小数，量程太窄时上/中/下三档会印成同一个数
_MIN_AXIS_DIVISION = {"voltage": 0.2}


def _series_scale(key: str, values: list[float]) -> tuple[float, float]:
    """按序列取量程：电压轴每格不低于 0.2V（整轴不低于 0.4V），其余序列不设限。"""
    return _scale_range(values, min_span=2.0 * _MIN_AXIS_DIVISION.get(key, 0.0))


class _MiniCurve(QWidget):
    """两序列迷你曲线：各自独立量程，第一序列（电压）刻度画左轴、
    第二序列（x向速度）刻度画右轴——量程差太大的量不共用一根纵轴。"""

    def __init__(self, window_s: float, series: list[str],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._window_s = max(5.0, float(window_s))
        self._series = [s for s in series if s in _SERIES_META]
        self._max_points = int(max(20, min(1200, self._window_s * 20)))
        self._pts: dict[str, list[tuple[float, float]]] = {s: [] for s in self._series}
        self._t_last = 0.0
        self.setMinimumHeight(100)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    @property
    def enabled(self) -> bool:
        return bool(self._series)

    def reset(self, samples: list[TelemetrySample]) -> None:
        for key in self._pts:
            self._pts[key] = []
        self._t_last = 0.0
        for sample in samples:
            if isinstance(sample, TelemetrySample):
                self._append(sample)
        self.update()

    def clear(self) -> None:
        for key in self._pts:
            self._pts[key] = []
        self._t_last = 0.0
        self.update()

    def add(self, sample: TelemetrySample) -> bool:
        if not isinstance(sample, TelemetrySample):
            return False
        if not self._series or sample.ts <= self._t_last:
            return False
        self._append(sample)
        self.update()
        return True

    def _append(self, sample: TelemetrySample) -> None:
        self._t_last = sample.ts
        horizon = sample.ts - self._window_s
        for key, bucket in self._pts.items():
            value = getattr(sample, key, None)
            if value is not None:
                bucket.append((sample.ts, float(value)))
            if len(bucket) > self._max_points:
                del bucket[: len(bucket) - self._max_points]
            while bucket and bucket[0][0] < horizon:
                bucket.pop(0)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self._paint(painter, self.width(), self.height())

    def _paint(self, painter: QPainter, w: int, h: int) -> None:
        top, bottom = 4, h - 18
        font = small_font()
        painter.setFont(font)
        fm = QFontMetrics(font)

        # 先算每条序列的量程与上/中/下三档刻度，再按刻度文字实测宽度留轴区
        scales: list[tuple[float, float] | None] = []
        ticks: list[list[str]] = []
        labels: list[str] = []
        for key in self._series:
            label, _color, unit, digits = _SERIES_META[key]
            bucket = self._pts.get(key) or []
            last = bucket[-1][1] if bucket else None
            labels.append(f"{label} {_fmt(last, digits, unit)}")
            if len(bucket) >= 2:
                lo, hi = _series_scale(key, [v for _, v in bucket])
                scales.append((lo, hi))
                ticks.append([
                    f"{hi:.{digits}f}{unit}",
                    f"{(lo + hi) / 2:.{digits}f}{unit}",
                    f"{lo:.{digits}f}{unit}",
                ])
            else:
                scales.append(None)
                ticks.append([])

        def _edge(texts: list[str]) -> float:
            widths = [fm.horizontalAdvance(t) for t in texts]
            return (max(widths) + 6 if widths else 4)

        left = _edge(ticks[0]) if ticks else 4
        right = w - (_edge(ticks[1]) if len(ticks) > 1 else 4)
        painter.fillRect(painter.viewport(), QColor("#fbfcfe"))
        painter.setPen(QPen(_GRID, 1))
        for i in range(5):
            x = left + (right - left) * i / 4
            painter.drawLine(int(x), int(top), int(x), int(bottom))
        for i in range(3):
            y = top + (bottom - top) * i / 2
            painter.drawLine(int(left), int(y), int(right), int(y))

        # 双轴刻度：第一序列在左、第二序列在右，刻度用各自序列色呼应曲线
        for idx in (0, 1):
            texts = ticks[idx] if idx < len(ticks) else []
            if not texts:
                continue
            painter.setPen(_SERIES_META[self._series[idx]][1])
            for j, text in enumerate(texts):
                y = int(top + (bottom - top) * j / 2) + 4
                if idx == 0:
                    painter.drawText(int(left - fm.horizontalAdvance(text) - 4), y, text)
                else:
                    painter.drawText(int(right + 4), y, text)

        now = time.time()
        t0 = now - self._window_s
        span = max(1.0, now - t0)
        xs = _legend_layout(fm, labels, left + 6)
        for i, key in enumerate(self._series):
            bucket = self._pts.get(key) or []
            color = _SERIES_META[key][1]
            scale = scales[i] if i < len(scales) else None
            if len(bucket) >= 2 and scale is not None:
                lo, hi = scale
                painter.setPen(QPen(color, 2))
                poly = []
                for ts, value in bucket:
                    x = left + (min(max(ts, t0), now) - t0) / span * (right - left)
                    y = bottom - (value - lo) / (hi - lo) * (bottom - top)
                    poly.append(QPointF(x, y))
                painter.drawPolyline(QPolygonF(poly))
            painter.setPen(color)
            painter.drawText(int(xs[i]), int(bottom + 6), labels[i])
        if not any(self._pts.values()):
            painter.setPen(_FAINT)
            painter.drawText(int(left + 6), int((top + bottom) / 2) + 4, "遥测曲线（暂无数据）")


class TelemetryBar(QWidget):
    """一行数字 + 一条迷你曲线；``refresh(payload)`` 是桥侧唯一入口。"""

    exported = pyqtSignal()

    def __init__(self, cfg, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._sample: TelemetrySample | None = None
        self._stale = False
        self._age_s = 0.0

        self.cells: dict[str, QLabel] = {}
        row = QHBoxLayout()
        row.setSpacing(14)
        for name in _CELL_ORDER:
            cap = QLabel(f"{name} ")
            cap.setStyleSheet(_TEXT_DIM)
            value = QLabel("--")
            value.setStyleSheet(_TEXT_NORMAL)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            cell = QHBoxLayout()
            cell.setSpacing(0)
            cell.addWidget(cap)
            cell.addWidget(value)
            wrap = QWidget()
            wrap.setLayout(cell)
            row.addWidget(wrap)
            self.cells[name] = value
        row.addStretch(1)
        self.btn_export = QPushButton("导出CSV")
        self.btn_export.setStyleSheet(_TEXT_DIM)
        self.btn_export.clicked.connect(self.exported.emit)
        row.addWidget(self.btn_export)

        series = list(getattr(getattr(cfg, "curve", None), "series", []) or [])
        self.curve = _MiniCurve(
            float(getattr(getattr(cfg, "curve", None), "window_s", 60.0) or 60.0),
            series or ["voltage", "vx"],
        )

        box = QVBoxLayout(self)
        box.setContentsMargins(6, 2, 6, 2)
        box.setSpacing(2)
        box.addLayout(row)
        box.addWidget(self.curve, 1)
        self._render()

    # ------------------------------------------------------------------ 对外接口
    def refresh(self, payload: object) -> None:
        if isinstance(payload, dict):
            sample = payload.get("sample")
            if isinstance(sample, TelemetrySample):
                self._sample = sample
                self.curve.add(sample)
            self._stale = bool(payload.get("stale"))
            try:
                self._age_s = float(payload.get("age_s") or 0.0)
            except (TypeError, ValueError):
                self._age_s = 0.0
            self._render()

    def prefill(self, samples: list[TelemetrySample], *, stale: bool = False,
                age_s: float = 0.0) -> None:
        """进页回填：曲线整段重放，数字取最后一个样本。"""
        self.curve.reset(samples)
        if samples:
            self._sample = samples[-1]
        self._stale = bool(stale)
        self._age_s = float(age_s)
        self._render()

    # ------------------------------------------------------------------ 渲染
    def _render(self) -> None:
        sample = self._sample
        stale = self._stale
        status_text = "--"
        voltage = None
        low = False
        estop = False
        for name in _CELL_ORDER:
            self.cells[name].setText("--")
        if sample is None:
            self.cells[_FIELD_GPS].setText("暂无数据")
            self._apply_style(status_text, voltage, low, estop, stale)
            return
        if sample.flag_stop is not None:
            status_text = "失能" if sample.flag_stop != 0 else "使能"
            estop = sample.flag_stop != 0
        else:
            status_text = self._cfg.status_text(sample.status)
            estop = "急停" in status_text
        voltage = sample.voltage
        low = voltage is not None and voltage < float(self._cfg.battery.low_voltage_v)
        lat, lon = sample.latitude, sample.longitude
        if lat is not None and lon is not None:
            position = f"{lat:.6f}, {lon:.6f}"
        else:
            position = "--"
        gps = "有效" if sample.gps_valid else "无效"
        if sample.ts:
            gps += " · " + time.strftime("%H:%M:%S", time.localtime(sample.ts))
        if stale:
            gps += f"（滞后{self._age_s:.0f}s）"
        self.cells[_FIELD_STATUS].setText(status_text)
        self.cells[_FIELD_VOLTAGE].setText(_fmt(voltage, 1, " V"))
        self.cells[_FIELD_CURRENT].setText(_fmt(sample.current, 1, " A"))
        self.cells[_FIELD_SOC].setText(_fmt(sample.soc, 0, "%"))
        self.cells[_FIELD_SPEED].setText(_fmt(sample.speed, 2, " m/s"))
        speed_tip = "当前速度"
        if sample.distance is not None:
            speed_tip += f"；累计里程 {sample.distance:.1f} m（见导出CSV）"
        self.cells[_FIELD_SPEED].setToolTip(speed_tip)
        self.cells[_FIELD_POS].setText(position)
        self.cells[_FIELD_POS].setFont(mono_font())
        self.cells[_FIELD_GPS].setText(gps)
        self._apply_style(status_text, voltage, low, estop, stale)

    def _apply_style(self, status_text: str, voltage: float | None, low: bool,
                     estop: bool, stale: bool) -> None:
        for name, label in self.cells.items():
            if stale:
                label.setStyleSheet(_TEXT_STALE)
            elif name == _FIELD_VOLTAGE and low:
                label.setStyleSheet(_TEXT_ALERT)
            elif name == _FIELD_STATUS and estop:
                label.setStyleSheet(_TEXT_ALERT)
            else:
                label.setStyleSheet(_TEXT_NORMAL)
        hint = "" if self._sample is not None else "暂无遥测数据"
        if stale and self._sample is not None:
            hint = "遥测中断"
        elif hint == "" and (low or estop):
            hint = "电池低压" if low else status_text
        self.setToolTip(hint)
