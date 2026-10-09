"""守住一类必崩错误：把 PyQt5 的方法当属性拿去比较。

PyQt5 的 ``QComboBox.currentIndex`` 在 PySide 里是 property，在 PyQt5 里是方法。
写成 ``combo.currentIndex < 0`` 语法没毛病，运行时抛 TypeError；而它发生在 Qt
回调栈里，进程直接以 0xC0000409 硬崩、一行 traceback 都不吐——本项目真上过这招，
排查成本极高，所以用静态扫描钉死这一条。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "hmi" / "ui"

# 只盯 Qt 控件上"易被当属性用"的方法名：出现在比较/布尔语境里必然是漏了括号
_QT_METHODS = {
    "currentIndex",
    "count",
    "currentText",
    "itemText",
    "itemData",
    "isChecked",
    "isEnabled",
    "findText",
    "findData",
}


def _scan(tree: ast.AST) -> list[str]:
    """找出被当值用的 Qt 方法：出现在比较两侧、或裸用在 if/while 条件里。"""
    bad: list[str] = []
    calls: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            calls.add(id(node.func))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _QT_METHODS:
            if id(node) in calls:
                continue
            bad.append(f"L{node.lineno}: .{node.attr} 要写成 .{node.attr}()")
    return bad


def test_ui_sources_never_use_qt_method_as_value() -> None:
    offenders: dict[str, list[str]] = {}
    files = sorted(p for p in SRC.rglob("*.py") if p.is_file())
    assert files, f"没扫到任何 UI 源文件：{SRC}"
    for path in files:
        hits = _scan(ast.parse(path.read_text(encoding="utf-8")))
        if hits:
            offenders[path.name] = hits
    assert not offenders, f"把 Qt 方法当值用了：{offenders}"


def test_scanner_actually_catches_the_bug() -> None:
    """自证守卫不是空的：当年那行喂给它必须报警。"""
    buggy = (
        "class D:\n"
        "    def f(self):\n"
        "        if self.combo_encoding.currentIndex < 0:\n"
        "            self.combo_encoding.setCurrentIndex(0)\n"
    )
    hits = _scan(ast.parse(buggy))
    assert hits == ["L3: .currentIndex 要写成 .currentIndex()"]

    fixed = (
        "class D:\n"
        "    def f(self):\n"
        "        if self.combo_encoding.currentIndex() < 0:\n"
        "            self.combo_encoding.setCurrentIndex(0)\n"
    )
    assert _scan(ast.parse(fixed)) == []


@pytest.mark.parametrize("expr", ["a.count", "a.currentIndex", "a.findText"])
def test_scanner_reports_each_shape(expr: str) -> None:
    """名字对上就报警，不管出现在哪种表达式里。"""
    name = expr.split(".", 1)[1]
    assert _scan(ast.parse(expr)) == [f"L1: .{name} 要写成 .{name}()"]


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
