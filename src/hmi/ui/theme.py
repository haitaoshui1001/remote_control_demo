"""界面观感集中定义：一个字号基准，派生出全套字体与样式表。

以前字号散落在各处——样式表里写死 ``QWidget { font-size: 13px; }``，五个页面各自
``QFont("Consolas", 9)``，占位页又是一句 ``font-size:22px``。样式表那条会盖住 Qt 的
默认字体，于是一处写小、满界面跟着小，想调也调不全。这里只留一个旋钮：配置项
``ui.font_size``（正文像素字号），其余字号都从它换算：

- 正文 = 基准；次要标签 = 基准 - 1；占位页大标题 = 基准 + 11；
- 等宽流水（Consolas）用「点」，按 96DPI 换算（1pt = 4/3px），取基准的 0.72 倍；
- 曲线坐标轴那一类次要标注再小一档（0.6 倍），但仍随基准联动，不会单独掉回旧尺寸；
- 左侧导航的宽度与条目内边距也按基准走，字一大不至于挤掉汉字。

字体对象按（用途, 基准）缓存复用：表格的 ``data()`` 会为每个可见单元格取字体，
不能每次新建。
"""

from __future__ import annotations

from string import Template

from PyQt5.QtGui import QFont

__all__ = [
    "DEFAULT_FONT_PX",
    "DARK_COLORS",
    "body_pt",
    "font_px",
    "head_px",
    "label_px",
    "mono_font",
    "mono_pt",
    "nav_width_px",
    "set_font_px",
    "small_font",
    "style_sheet",
    "ui_font",
]

#: 正文基准像素字号。与 core.config.UiCfg.font_size 的默认值对齐（有测试盯住）
DEFAULT_FONT_PX = 20

#: 可调上下限：再小看不清，再大会把 1280×800 的窗口撑爆
_MIN_PX = 11
_MAX_PX = 28

_font_px = DEFAULT_FONT_PX
_fonts: dict[str, QFont] = {}

#: 深色主题的调色板，交给 QPalette 用；样式表本身只管浅色
DARK_COLORS = {
    "window": "#262d38",
    "base": "#1f2530",
    "text": "#e6ebf2",
    "button": "#2d3542",
    "highlight": "#1a7fd4",
}

#: 浅色主题样式表。刻意只写通用控件，具体页面不靠样式表排布，方便各自调整。
#: ``${body}`` 等占位由 style_sheet() 按当前基准字号填。
_STYLE = Template("""
QWidget { font-size: ${body}px; }
QMainWindow, QWidget#Central { background: #f5f7fa; }
QListWidget#Nav { background: #ffffff; border: none; border-right: 1px solid #dfe3e8;
                  outline: none; padding-top: 6px; }
QListWidget#Nav::item { padding: ${nav_y}px ${nav_x}px; color: #3b4451;
                        border-left: 3px solid transparent; }
QListWidget#Nav::item:hover { background: #f2f6fb; }
QListWidget#Nav::item:selected { background: #e8f1fb; color: #0b6bbf;
                                 border-left: 3px solid #1a7fd4; font-weight: bold; }
QStatusBar { background: #eef1f5; color: #4a5568; }
QStatusBar QLabel { color: #4a5568; padding: 0 6px; }
QGroupBox { background: #ffffff; border: 1px solid #dfe3e8; border-radius: 4px;
            margin-top: ${group_top}px; padding-top: 6px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #4a5568; }
QPushButton { background: #ffffff; padding: ${btn_y}px ${btn_x}px; border: 1px solid #cbd2da;
              border-radius: 3px; }
QPushButton:hover { background: #e6f0fa; border-color: #1a7fd4; }
QPushButton:pressed { background: #cfe3f7; }
QPushButton:disabled { color: #a7afba; background: #f2f4f7; }
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #ffffff; border: 1px solid #cbd2da; border-radius: 3px;
    min-height: ${field_h}px; padding: ${input_y}px 6px; }
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus {
    border-color: #1a7fd4; }
QComboBox QAbstractItemView {
    background: #ffffff; color: #1f2733; border: 1px solid #1a7fd4;
    border-radius: 3px; outline: none;
    selection-background-color: #e8f1fb; selection-color: #0b6bbf; }
QComboBox QAbstractItemView::item {
    min-height: ${item_h}px; padding: 2px 8px; }
QTableWidget, QTreeView, QListWidget { background: #ffffff; border: 1px solid #dfe3e8; }
QHeaderView::section { background: #eef1f5; padding: ${head_y}px; border: none;
                       border-right: 1px solid #dfe3e8; color: #4a5568; }
QScrollBar:vertical { width: ${bar_w}px; background: transparent; }
QScrollBar::handle:vertical { background: #c8d0da; border-radius: ${bar_r}px;
                              min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
""")


def set_font_px(px: int | str | None) -> int:
    """定下正文基准字号，越界自动夹到 11~28，返回真正生效的值。

    接受字符串是因为配置和 CLI 覆盖一路传下来的可能是文本（``--set ui.font_size=16``）。
    """
    global _font_px
    try:
        value = int(str(px).strip())  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = DEFAULT_FONT_PX
    _font_px = max(_MIN_PX, min(_MAX_PX, value))
    return _font_px


def font_px() -> int:
    """当前正文像素字号。"""
    return _font_px


def body_pt() -> int:
    """Qt 默认字体用「点」，像素按 96DPI 折算成点。"""
    return max(9, round(_font_px * 0.75))


def mono_pt() -> int:
    """等宽流水字号（点）。"""
    return max(8, round(_font_px * 0.72))


def _cached(role: str, family: str, pt: int) -> QFont:
    key = f"{role}:{_font_px}"
    font = _fonts.get(key)
    if font is None:
        font = QFont(family, pt)
        _fonts[key] = font
    return font


def ui_font() -> QFont:
    """界面默认字体（中文优先雅黑）。"""
    return _cached("ui", "Microsoft YaHei", body_pt())


def mono_font() -> QFont:
    """十六进制 / ASCII 流水、AT 预览等一律等宽显示的地方。"""
    return _cached("mono", "Consolas", mono_pt())


def small_font() -> QFont:
    """曲线坐标轴刻度那类次要标注。"""
    return _cached("small", "Consolas", max(8, round(_font_px * 0.6)))


def label_px() -> int:
    """次要说明文字（比正文小一号，仍跟得上基准）。"""
    return max(10, _font_px - 1)


def head_px() -> int:
    """占位页大标题。"""
    return _font_px + 11


def nav_width_px() -> int:
    """左侧导航宽度：不窄于原先调好的 176，字大就跟着宽。"""
    return max(176, round(_font_px * 12))


def style_sheet() -> str:
    """按当前基准字号生成浅色主题样式表。"""
    px = _font_px
    return _STYLE.substitute(
        body=px,
        nav_y=max(9, px - 5),
        nav_x=14,
        group_top=max(12, px - 3),
        btn_y=max(5, round(px * 0.4)),
        btn_x=14,
        input_y=max(3, round(px * 0.25)),
        field_h=px,
        item_h=px + 4,
        head_y=max(4, round(px * 0.3)),
        bar_w=max(10, round(px * 0.75)),
        bar_r=max(5, round(px * 0.375)),
    )
