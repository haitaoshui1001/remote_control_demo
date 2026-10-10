"""十字方向盘：四方向按住按钮 + 中央当前/目标速度读数。

与 TelemetryBar 同一纪律——控件不碰服务：按住/松开只发方向字符串信号，
限幅、直通、发帧全在运动服务里，页面只做连线。按钮一律 NoFocus：
方向键要留给页面的 keyPressEvent（键盘控制），不能被按钮抢走焦点。

按住 = 该方向目标取最大速度（不加减速，下一拍即满速），松开 = 目标归零；
落零时补发停止帧由服务执行。鼠标一次只能按一个按钮，前进+转向的组合动作
请用键盘方向键（如 ↑+←），控件只负责把"按住/松开"这两件事说清楚。
"""

from __future__ import annotations

from typing import Any

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QDoubleSpinBox,
    QGridLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..theme import label_px, mono_font

__all__ = ["MotionPad"]

_CELLS = (
    ("up", "↑", 0, 1),
    ("left", "←", 1, 0),
    ("right", "→", 1, 2),
    ("down", "↓", 2, 1),
)

_TEXT_NORMAL = f"color:#1f2937; font-size:{label_px()}px;"
_TEXT_MOVING = f"color:#b25a00; font-size:{label_px()}px; font-weight:bold;"
_TEXT_DIM = f"color:#9aa3ad; font-size:{label_px()}px;"
_BTN_HELD = "background:#cfe3f7; border:1px solid #1a7fd4; font-weight:bold;"


class MotionPad(QWidget):
    """四方向按住按钮 + 速度读数 + vx/vr 限速设置行；只发信号，不做运动学计算。"""

    pressed = pyqtSignal(str)
    released = pyqtSignal(str)
    limits_changed = pyqtSignal(float, float)

    def __init__(self, cfg: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self._btns: dict[str, QPushButton] = {}
        btn_w = max(120, label_px() * 6)
        btn_h = max(96, (label_px() + 12) * 3)
        self._arrow_px = int(btn_h * 0.5)
        self._btn_css_normal = f"font-size:{self._arrow_px}px;"
        self._btn_css_held = f"font-size:{self._arrow_px}px; {_BTN_HELD}"

        grid = QGridLayout()
        grid.setSpacing(4)
        for key, arrow, row, col in _CELLS:
            btn = QPushButton(arrow)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setFixedSize(btn_w, btn_h)
            btn.setFont(QFont(btn.font().family(), -1, QFont.Bold))
            btn.setStyleSheet(self._btn_css_normal)
            btn.pressed.connect(lambda k=key: self._on_btn_press(k))
            btn.released.connect(lambda k=key: self._on_btn_release(k))
            grid.addWidget(btn, row, col)
            self._btns[key] = btn

        self.center_label = QLabel("停")
        self.center_label.setAlignment(Qt.AlignCenter)
        self.center_label.setStyleSheet(_TEXT_DIM)
        grid.addWidget(self.center_label, 1, 1)

        self.readout = QLabel("vx --  vr --")
        self.readout.setAlignment(Qt.AlignCenter)
        self.readout.setFont(mono_font())
        self.readout.setStyleSheet(_TEXT_DIM)

        spin_css = f"font-size:{label_px()}px;"
        self.spin_vx = QDoubleSpinBox()
        self.spin_vx.setRange(0.05, 5.0)
        self.spin_vx.setSingleStep(0.05)
        self.spin_vx.setDecimals(2)
        self.spin_vx.setSuffix(" m/s")
        self.spin_vx.setValue(float(getattr(cfg, "max_linear_speed", 1.0)))
        self.spin_vx.setStyleSheet(spin_css)
        self.spin_vr = QDoubleSpinBox()
        self.spin_vr.setRange(0.05, 3.0)
        self.spin_vr.setSingleStep(0.05)
        self.spin_vr.setDecimals(2)
        self.spin_vr.setSuffix(" rad/s")
        self.spin_vr.setValue(float(getattr(cfg, "max_angular_speed", 0.5)))
        self.spin_vr.setStyleSheet(spin_css)
        self.btn_limits = QPushButton("应用限速")
        self.btn_limits.setFocusPolicy(Qt.NoFocus)
        self.btn_limits.setStyleSheet(spin_css)
        self.btn_limits.clicked.connect(self._on_limits_clicked)
        lbl_vx = QLabel("vx 最大")
        lbl_vr = QLabel("vr 最大")
        lbl_vx.setStyleSheet(_TEXT_DIM)
        lbl_vr.setStyleSheet(_TEXT_DIM)
        limits = QGridLayout()
        limits.setSpacing(2)
        limits.addWidget(lbl_vx, 0, 0)
        limits.addWidget(self.spin_vx, 0, 1)
        limits.addWidget(lbl_vr, 1, 0)
        limits.addWidget(self.spin_vr, 1, 1)

        box = QVBoxLayout(self)
        box.setContentsMargins(6, 2, 6, 2)
        box.setSpacing(2)
        box.addLayout(grid)
        box.addWidget(self.readout)
        box.addLayout(limits)
        box.addWidget(self.btn_limits)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        if not getattr(cfg, "enabled", True):
            self.set_unavailable("运动控制已禁用")

    # ------------------------------------------------------------------ 按钮事件
    def _on_btn_press(self, direction: str) -> None:
        if self.isEnabled() and getattr(self.cfg, "enabled", True):
            self.pressed.emit(direction)

    def _on_btn_release(self, direction: str) -> None:
        self.released.emit(direction)

    def _on_limits_clicked(self) -> None:
        self.limits_changed.emit(float(self.spin_vx.value()), float(self.spin_vr.value()))

    # ------------------------------------------------------------------ 状态回显
    def set_unavailable(self, text: str) -> None:
        """服务缺失或被禁用时整体停用并写明原因（与页面降级文案同思路）。"""
        self.setEnabled(False)
        self.readout.setText(text)
        self.readout.setStyleSheet(_TEXT_DIM)

    def refresh(self, snap: dict | None) -> None:
        """喂 ``MotionService.snapshot()``（或桥上的同构载荷）刷读数与按住高亮。"""
        if snap is None:
            return
        held = snap.get("held") or []
        for key, btn in self._btns.items():
            btn.setStyleSheet(self._btn_css_held if key in held else self._btn_css_normal)
        vx = float(snap.get("vx") or 0.0)
        vr = float(snap.get("vr") or 0.0)
        tvx = float(snap.get("target_vx") or 0.0)
        tvr = float(snap.get("target_vr") or 0.0)
        moving = bool(snap.get("moving"))
        self.center_label.setText("行" if moving or held else "停")
        self.center_label.setStyleSheet(_TEXT_MOVING if moving else _TEXT_DIM)
        target = f"目标 {tvx:+.2f}/{tvr:+.2f}" if (tvx or tvr) else "目标 停止"
        self.readout.setText(f"vx {vx:+.2f} vr {vr:+.2f}\n{target}")
        self.readout.setStyleSheet(_TEXT_MOVING if moving else _TEXT_NORMAL)
