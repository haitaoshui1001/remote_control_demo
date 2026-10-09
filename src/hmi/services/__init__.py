"""服务层：把协议对象装配成"界面能直接按按钮"的能力。

这一层只做三件事：按 ``cfg.app.mock`` 选真实源还是仿真源、把协议对象的变化转成事件、
管线程与生命周期。它不解析协议（protocol 层的事），不碰 Qt（ui 层的事），
也不直接写业务判断以外的字节。

``Session`` 是装配根：六类服务之间有硬依赖（电台与透传共用一条字节通道的分流器），
起停顺序错了就会丢日志或制造假 IO 错误，所以只允许从 Session 出发。
"""

from __future__ import annotations

from .link import CHANNELS, LinkService
from .passthrough import PassthroughService
from .periodic import Ticker
from .radio import RadioService
from .record import CSV_FIELDS, CSV_TOPICS, SINK_DIRS, SINK_TOPICS, RecordService
from .session import Session, build_session
from .telemetry import CSV_HEADER as TELEMETRY_CSV_HEADER
from .telemetry import TelemetryService
from .video import VideoService, as_rgb_array, save_frame

__all__ = [
    # periodic
    "Ticker",
    # link
    "LinkService",
    "CHANNELS",
    # radio
    "RadioService",
    # passthrough
    "PassthroughService",
    # video
    "VideoService",
    "as_rgb_array",
    "save_frame",
    # record
    "RecordService",
    "CSV_FIELDS",
    "SINK_TOPICS",
    "CSV_TOPICS",
    "SINK_DIRS",
    # telemetry
    "TelemetryService",
    "TELEMETRY_CSV_HEADER",
    # session
    "Session",
    "build_session",
]
