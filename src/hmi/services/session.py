"""装配根：把 services 层的八个服务接成一个能统一起停的运行时会话。

顺序里藏了三条约束，写死在这里，省得每个入口（GUI、自检脚本、pytest）各排一遍：

1. ``LinkService`` 最先装配。电台配置挂在它的 ``link.demux`` 上；485 透传默认走
   独立串口（``passthrough.channel=serial`` 分线模式，挂 ``link.pt_demux``），
   仅在与 AT 共线时两者才共享同一个 demux——一条字节通道只能有一个读者。
2. ``RecordService`` 最后启动。反过来就会前面几步的启动事件全落不进流水，
   日志文件看起来像"半路才开始记录"。
3. 停止走逆序，且**先停节拍再关通道**。刷新线程在句柄关闭后继续写，会制造一批
   根本不存在的 IO 错误，排查起来非常费劲。

这里不做任何协议解析，也不碰 Qt：它只是把对象接起来，再把状态汇总成 dict 给上层。
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.config import AppConfig, load_config
from ..core.events import EventBus, get_bus
from ..core.logger import get_logger
from .camera_config import CameraConfigService
from .link import LinkService
from .motion import MotionService
from .passthrough import PassthroughService
from .radio import RadioService
from .record import RecordService
from .telemetry import TelemetryService
from .video import VideoService

__all__ = ["Session", "build_session"]


class Session:
    """一套硬件 = 一个 Session：持有全部服务，负责装配、起停、汇总。"""

    def __init__(
        self,
        cfg: AppConfig | None = None,
        *,
        bus: EventBus | None = None,
        log: logging.Logger | None = None,
        endpoint: str = "local",
        video: bool = True,
        record: bool = True,
        telemetry: bool = True,
        motion: bool = True,
    ) -> None:
        self.cfg = cfg if cfg is not None else load_config()
        self.bus = bus if bus is not None else get_bus()
        self.log = log or get_logger(f"hmi.session.{endpoint}")
        self.endpoint = str(endpoint)

        self.link = LinkService(self.cfg, bus=self.bus, endpoint=endpoint)
        self.radio = RadioService(self.link, self.cfg, bus=self.bus)
        self.passthrough = PassthroughService(self.link, self.cfg, bus=self.bus)
        self.telemetry = (
            TelemetryService(self.passthrough, self.cfg, bus=self.bus) if telemetry else None
        )
        self.motion = (
            MotionService(self.passthrough, self.cfg, bus=self.bus) if motion else None
        )
        self.video = VideoService(self.cfg, bus=self.bus) if video else None
        self.camera = CameraConfigService(self.cfg, bus=self.bus, video=self.video)
        self.record = (
            RecordService(self.cfg, bus=self.bus, video=self.video) if record else None
        )
        self.started = False

    # ------------------------------------------------------------------ 起停
    def start(
        self,
        *,
        link: bool = True,
        video: bool | None = None,
        record: bool | None = None,
    ) -> dict[str, Any]:
        """按依赖顺序拉起会话；每步返回结果而不抛异常。

        串口被占用、摄像头离线都是**常态**而不是异常：界面要能带着这些失败继续跑，
        所以这里把结果收进 dict，由上层决定怎么显示。
        """
        out: dict[str, Any] = {
            "link": None,
            "passthrough": False,
            "telemetry": None,
            "motion": None,
            "video": None,
            "record": None,
            "detail": [],
        }
        if link:
            out["link"] = self.link.open()
            if not out["link"]:
                out["detail"].append(f"控制线打不开（{self.link.channel}）")
        out["passthrough"] = self.passthrough.start()
        if self.telemetry is not None:
            out["telemetry"] = self.telemetry.start()
        if self.motion is not None:
            out["motion"] = self.motion.start()
        want_video = self.cfg.video.auto_connect if video is None else video
        if self.video is not None and want_video:
            out["video"] = self.video.start()
            if not out["video"]:
                out["detail"].append("视频取流启动失败")
        if self.record is not None and (record is None or record):
            out["record"] = self.record.start()
            if not out["record"]:
                out["detail"].append("落盘目录不可写，本次不记录流水")
        self.started = bool(out["link"])
        out["polling"] = self.link.polling
        out["ok"] = bool(out["link"])
        return out

    def stop(self) -> None:
        """逆序停：先断数据源，再停消费者，最后关通道。"""
        if self.record is not None:
            self.record.stop()
        if self.video is not None:
            self.video.stop()
        if self.telemetry is not None:
            self.telemetry.stop()
        if self.motion is not None:
            # 节拍必须先于通道关闭停掉；stop() 内部会趁通道还开着补发停止帧。
            self.motion.stop()
        self.passthrough.stop()
        self.link.close()
        self.started = False

    # ------------------------------------------------------------------ 汇总
    @property
    def mock(self) -> bool:
        return bool(self.cfg.app.mock)

    def summary(self) -> dict[str, Any]:
        """一屏状态：给标题栏、状态栏和自检脚本共用。"""
        return {
            "endpoint": self.endpoint,
            "mock": self.mock,
            "started": self.started,
            "link": self.link.summary(),
            "radio": self.radio.summary(),
            "passthrough": self.passthrough.summary(),
            "telemetry": self.telemetry.summary() if self.telemetry is not None else None,
            "motion": self.motion.summary() if self.motion is not None else None,
            "video": self.video.summary() if self.video is not None else None,
            "camera": self.camera.summary(),
            "record": self.record.summary() if self.record is not None else None,
        }

    def __enter__(self) -> Session:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def __repr__(self) -> str:
        return (
            f"<Session {self.endpoint} channel={self.link.channel} "
            f"open={self.link.is_open} mock={self.mock}>"
        )


def build_session(
    path: str | None = None,
    *,
    mock: bool | None = None,
    endpoint: str = "local",
    video: bool = True,
    record: bool = True,
    telemetry: bool = True,
    motion: bool = True,
    **overrides: Any,
) -> Session:
    """读配置并装配会话，给 CLI/GUI 一个统一入口。

    ``mock`` 单独开参数是因为它太常用了（无硬件自检、演示、CI），其余覆盖都写点号路径，
    例如 ``build_session(None, **{"serial.port": "COM7"})``。

    ``video`` / ``record`` / ``telemetry`` / ``motion`` 控制**装不装这几个服务**，与
    ``Session.start()`` 上同名的参数（控制这**一次**起不起）是两回事，所以必须显式
    收下并转发给构造函数；否则它们会掉进 ``overrides``，被 ``load_config`` 当成
    ``video`` / ``record`` 几个点号路径，把整个配置段覆盖掉。
    """
    if mock is not None:
        overrides["app.mock"] = bool(mock)
    return Session(
        load_config(path, **overrides),
        endpoint=endpoint,
        video=video,
        record=record,
        telemetry=telemetry,
        motion=motion,
    )
