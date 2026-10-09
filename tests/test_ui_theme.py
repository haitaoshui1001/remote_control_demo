"""观感主题层测试：一个字号基准派生全套，锚住默认值一致性与向后兼容。

关键锚点有两类：一是三处默认值必须同数（theme、UiCfg、config/app.yaml），
二是旧基准 13px 下所有派生值必须精确等于改造前散落的硬编码——保证把旋钮
拧回旧值时界面和从前一模一样。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hmi.core.config import AppConfig, load_config
from hmi.ui.theme import (
    DARK_COLORS,
    DEFAULT_FONT_PX,
    body_pt,
    font_px,
    head_px,
    label_px,
    mono_font,
    mono_pt,
    nav_width_px,
    set_font_px,
    small_font,
    style_sheet,
    ui_font,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _restore_base_font():
    """基准字号是模块态，用例之间必须互不污染。"""
    prev = font_px()
    yield
    set_font_px(prev)


# ------------------------------------------------------------------ 默认值一致性
def test_default_matches_config_and_yaml():
    assert DEFAULT_FONT_PX == AppConfig().ui.font_size
    data = yaml.safe_load((ROOT / "config" / "app.yaml").read_text(encoding="utf-8"))
    assert data["ui"]["font_size"] == DEFAULT_FONT_PX


# ------------------------------------------------------------------ 旋钮行为
def test_set_font_px_accepts_text_and_returns_effective():
    assert set_font_px("18") == 18
    assert font_px() == 18


def test_set_font_px_clamps_out_of_range():
    assert set_font_px(5) == 11
    assert set_font_px(100) == 28


def test_set_font_px_falls_back_on_garbage():
    assert set_font_px(None) == DEFAULT_FONT_PX
    assert set_font_px("abc") == DEFAULT_FONT_PX


def test_config_warns_out_of_range_font_size(tmp_path):
    p = tmp_path / "app.yaml"
    p.write_text("ui:\n  font_size: 99\n", encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.ui.font_size == 99
    assert any("超出" in w for w in cfg.meta.warnings)


# ------------------------------------------------------------------ 派生换算
def test_legacy_13px_base_reproduces_old_hardcoded_sizes():
    """旧基准下每个派生值都必须等于改造前写死的数字。"""
    set_font_px(13)
    assert body_pt() == 10  # 原 app.setFont(QFont("Microsoft YaHei", 10))
    assert mono_pt() == 9  # 原五页 QFont("Consolas", 9)
    assert label_px() == 12  # 原 data_page 内联 font-size:12px
    # 大标题旧版写死 22px，新公式随基准联动（基准+11），13 基准下为 24，属有意放大
    assert nav_width_px() == 176  # 原 setFixedWidth(176)


def test_small_font_follows_base():
    set_font_px(13)
    assert small_font().pointSize() == 8  # 原曲线轴 QFont("Consolas", 8)
    set_font_px(15)
    assert small_font().pointSize() > 8


def test_derivations_grow_with_larger_base():
    set_font_px(15)
    assert label_px() == 14
    assert head_px() == 26
    assert nav_width_px() == 180
    assert body_pt() == 11
    assert mono_pt() == 11


# ------------------------------------------------------------------ 样式表
def test_style_sheet_tracks_current_base():
    set_font_px(16)
    assert "font-size: 16px" in style_sheet()
    set_font_px(20)
    assert "font-size: 20px" in style_sheet()
    assert "font-size: 16px" not in style_sheet()


def test_style_sheet_legacy_values_at_13():
    set_font_px(13)
    sheet = style_sheet()
    assert "padding: 9px 14px" in sheet  # nav 条目内边距（原写死值）
    assert "margin-top: 12px" in sheet  # 分组标题留白（原写死值）


def _rule(sheet: str, anchor: str) -> str:
    """截取 sheet 里从 anchor 开始的那一条规则，避免断言串到别的规则上去。"""
    body = sheet[sheet.index(anchor):]
    return body[: body.index("}") + 1]


def test_input_controls_have_min_height():
    """输入类控件必须自报最小高度。

    实机截图上过一招：只给 padding 不给 min-height，字号一大文字就被上下裁掉
    半截（"主码流"显示成碎片），下拉框更夸张——整块空白。
    """
    set_font_px(16)
    rule = _rule(style_sheet(), "QLineEdit, QTextEdit")
    for sel in ("QComboBox", "QSpinBox", "QDoubleSpinBox", "QLineEdit"):
        assert sel in rule, f"{sel} 得吃到同一条 min-height 规则"
    assert "min-height: 16px" in rule, "字段高度要跟着基准字号走"


def test_combo_popup_rows_are_tall_enough_to_see():
    """下拉弹层要有配色和行高，否则点开是一片白/一行行挤成一条线。"""
    set_font_px(16)
    sheet = style_sheet()
    assert "QComboBox QAbstractItemView {" in sheet
    popup = _rule(sheet, "QComboBox QAbstractItemView {")
    assert "background: #ffffff" in popup, "弹层没配色就是一片灰，看不见选中项"
    assert "selection-color" in popup
    item = _rule(sheet, "QComboBox QAbstractItemView::item")
    assert "min-height: 20px" in item, "弹层行高=字号+4，得够点"
    set_font_px(20)
    item = _rule(style_sheet(), "QComboBox QAbstractItemView::item")
    assert "min-height: 24px" in item


# ------------------------------------------------------------------ 字体缓存
def test_font_objects_are_cached_per_base():
    set_font_px(15)
    f1 = mono_font()
    f2 = mono_font()
    assert f1 is f2
    set_font_px(20)
    assert mono_font() is not f1


def test_ui_font_family_and_size():
    f = ui_font()
    assert f.family() == "Microsoft YaHei"
    assert f.pointSize() == body_pt()
    assert DARK_COLORS["window"].startswith("#")
