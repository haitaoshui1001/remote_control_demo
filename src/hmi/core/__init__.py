"""契约层：数据模型、事件总线、配置与工具，被上层共同引用。"""

from __future__ import annotations

from . import events, logger, models, utils
from .config import (
    AppCfg,
    AppConfig,
    AtCfg,
    LinkCfg,
    PassthroughCfg,
    RecordCfg,
    SerialCfg,
    UdpAtCfg,
    UiCfg,
    VideoCfg,
    apply_overrides,
    load_config,
    project_root,
)
from .events import EventBus, Events, LatestSlot, Throttle, get_bus, set_bus
from .logger import BusLogHandler, get_logger, setup_logging

__all__ = [
    "models",
    "utils",
    "events",
    "logger",
    "AppConfig",
    "AppCfg",
    "VideoCfg",
    "SerialCfg",
    "AtCfg",
    "UdpAtCfg",
    "LinkCfg",
    "PassthroughCfg",
    "RecordCfg",
    "UiCfg",
    "load_config",
    "apply_overrides",
    "project_root",
    "EventBus",
    "Events",
    "Throttle",
    "LatestSlot",
    "get_bus",
    "set_bus",
    "setup_logging",
    "get_logger",
    "BusLogHandler",
]
