"""页面注册表与占位页的测试。

导航顺序、"页面在哪个模块"都以 ``PAGES`` 为唯一来源；占位降级只允许发生在
"本包内模块文件还没交付"这一种情况——把缺依赖、模块内报错这类真故障藏成
占位页，是现场最难排查的事，所以这里专门用 monkeypatch 把分支走全。
"""

from __future__ import annotations

import importlib

import pytest

from hmi.core.events import EventBus, Events
from hmi.ui.bridge import UiBridge
from hmi.ui.pages import (
    PAGES,
    PageSpec,
    PlaceholderPage,
    create_page,
    find_spec,
    page_keys,
)
from hmi.ui.pages.base import BasePage


@pytest.fixture()
def bridge():
    return UiBridge(EventBus())


def test_registry_keys_unique_and_ordered():
    keys = page_keys()
    assert len(keys) == len(PAGES) == 6
    assert len(set(keys)) == len(keys)
    assert keys == ("video", "console", "data", "radio", "channel", "log")


def test_module_naming_convention():
    """模块名恒为 key + "_page"：6.1/6.2/6.3 交付即自动接上，不用改注册表。"""
    for spec in PAGES:
        assert spec.module == f"{spec.key}_page"
        assert find_spec(spec.key) is spec


def test_find_spec_unknown_key():
    assert find_spec("nope") is None


def test_missing_module_becomes_placeholder(bridge):
    spec = PageSpec("tmp", "临时页", "no_such_page_module", "NoSuchPage", "还没交付")
    page = create_page(spec, None, bridge)
    assert isinstance(page, PlaceholderPage)
    assert (page.page_key, page.title, page.note) == ("tmp", "临时页", "还没交付")


def test_unrelated_import_error_propagates(bridge, monkeypatch):
    """第三方依赖缺失是真故障，必须往外抛。"""

    def fake_import(name, package=None):
        raise ModuleNotFoundError("No module named 'numpy'", name="numpy")

    monkeypatch.setattr(importlib, "import_module", fake_import)
    with pytest.raises(ModuleNotFoundError):
        create_page(find_spec("video"), None, bridge)


def test_non_import_error_propagates(bridge, monkeypatch):
    """模块导入时抛的其他异常（语法错、初始化炸了）同样不许降级。"""

    def fake_import(name, package=None):
        raise ValueError("模块内部错误")

    monkeypatch.setattr(importlib, "import_module", fake_import)
    with pytest.raises(ValueError):
        create_page(find_spec("log"), None, bridge)


def test_missing_class_becomes_placeholder(bridge):
    """模块在但注册的类不在：占位，且文案要说清"base 里没有 NoSuchPage"。"""
    spec = PageSpec("tmp2", "临时页", "base", "NoSuchPage")
    page = create_page(spec, None, bridge)
    assert isinstance(page, PlaceholderPage)
    assert "里没有" in page.note


def test_base_page_contains_slot_exceptions(qapp, bridge):
    """负载形状意外不许把进程带走：异常只记日志、进状态栏。"""

    class BoomPage(BasePage):
        def on_slot(self, topic):
            raise RuntimeError("bad payload")

    BoomPage(None, bridge)
    bridge.slot_updated.emit(Events.LINK_SNAPSHOT)


def test_placeholder_without_main_window_survives(qapp, bridge):
    page = PlaceholderPage("x", "测试页", "说明", None, bridge)
    page.say("没有主窗口挂状态栏也不该炸")
    assert page.service("video") is None
