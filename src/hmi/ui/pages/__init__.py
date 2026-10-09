"""页面注册表：导航顺序与"页面在哪个模块"的唯一来源。

主窗口只遍历这张表建导航，不关心具体页面存不存在。这样步骤 6 可以一页一页交付，
每页写完只要模块存在就被自动接上，不用改主窗口；没交付的页显示占位说明。

``create_page`` 只对"本包内的模块文件还没有"这种情况降级为占位页；要是模块本身
导入时抛错（缺依赖、语法错误），一定往外抛——把真故障藏成占位页是最糟糕的事。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

from .base import BasePage, PlaceholderPage

__all__ = ["PageSpec", "PAGES", "page_keys", "create_page", "BasePage", "PlaceholderPage"]


@dataclass(frozen=True)
class PageSpec:
    key: str
    title: str
    module: str
    cls: str
    note: str = ""


PAGES: tuple[PageSpec, ...] = (
    PageSpec(
        "video",
        "视频",
        "video_page",
        "VideoPage",
        "步骤 6.1：取流显示 / 抓拍 / 录像 / 码流切换",
    ),
    PageSpec(
        "console",
        "指令控制台",
        "console_page",
        "ConsolePage",
        "步骤 6.2：Hex/ASCII 透传发送 + 指令模板库",
    ),
    PageSpec(
        "data",
        "数据",
        "data_page",
        "DataPage",
        "步骤 6.2：485 双向字节流水与统计",
    ),
    PageSpec(
        "radio",
        "电台配置",
        "radio_page",
        "RadioPage",
        "步骤 6.3：表单由参数注册表生成（读取 / 编辑 / 预览 / 下发）",
    ),
    PageSpec(
        "channel",
        "信道监控",
        "channel_page",
        "ChannelPage",
        "步骤 6.3：RSSI / EVM 曲线与链路遥测",
    ),
    PageSpec("log", "日志", "log_page", "LogPage", "步骤 6.3：AT 交互、告警与运行日志"),
)


def page_keys() -> tuple[str, ...]:
    return tuple(spec.key for spec in PAGES)


def find_spec(key: str) -> PageSpec | None:
    for spec in PAGES:
        if spec.key == str(key):
            return spec
    return None


def create_page(spec: PageSpec, session: Any, bridge: Any) -> BasePage:
    """按注册表实例化页面，未交付的页返回占位页。"""
    target = f".{spec.module}"
    try:
        module = importlib.import_module(target, __name__)
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.endswith(spec.module):
            return PlaceholderPage(spec.key, spec.title, spec.note, session, bridge)
        raise
    page_cls = getattr(module, spec.cls, None)
    if page_cls is None:
        return PlaceholderPage(
            spec.key, spec.title, f"{spec.module} 里没有 {spec.cls}", session, bridge
        )
    return page_cls(session, bridge)
