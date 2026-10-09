"""透传服务：把引擎 + 模板库接上总线，并补齐"失败也要有结论"。

``PassthroughEngine`` 只管字节：发出去、收回来、配应答、记速率。它有个刻意的
不对称——``request()`` **只在成功路径**发布 ``CMD_RESULT``，超时、被拒、写失败
都只是把失败的 ``CommandResult`` 返回给调用方。这样引擎本身保持纯净（自检脚本
可以直接拿返回值断言），但界面上就会出现"点了按钮什么都没发生"的哑事故。

所以事件补发在这一层做，规则只有一条：**引擎没发的结论，服务发；引擎发过的，
服务不重复发**。判断依据是 ``ok``——成功路径必然已由引擎 publish 过。

另外 ``passthrough/stats``（``Events.PT_STATS``）也在这里按秒发布：引擎自己不
起线程，速率窗口需要有人定时读才会滚动。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from ..core.config import AppConfig, project_root
from ..core.events import EventBus, Events, get_bus
from ..core.logger import get_logger
from ..core.models import AlarmEvent, AlarmLevel, ByteRecord, CommandResult, PassthroughStats
from ..core.utils import bytes_to_hex
from ..protocol.passthrough import PassthroughEngine, build_frame
from ..protocol.telemetry_frames import describe_wheeltec_frame
from ..protocol.templates import TemplateLibrary
from .link import LinkService
from .periodic import Ticker

__all__ = ["PassthroughService"]


def _hex(frame: bytes) -> str:
    """失败收口用的十六进制显示串；空帧回空串。"""
    return bytes_to_hex(frame) if frame else ""


class PassthroughService:
    """一端（本端/远端）的 485 透传门面，UI 与自检脚本只认这一层。"""

    def __init__(
        self,
        link: LinkService,
        cfg: AppConfig,
        *,
        bus: EventBus | None = None,
        log: logging.Logger | None = None,
        stats_interval_s: float = 1.0,
        templates_path: str | Path | None = None,
    ) -> None:
        self.link = link
        self.cfg = cfg
        self.bus = bus if bus is not None else get_bus()
        self.log = log or get_logger(f"hmi.services.passthrough.{link.endpoint}")
        # 分线模式（AT 走网口、透传走独立串口）：引擎挂透传专用分流器，且
        # **不注入 busy_probe**——AT 从不在 pt 分流器上开事务窗口，默认探针
        # 恒为 False，半双工守卫自然失效，两条线互不阻塞。共线时保留原守卫。
        self._split = bool(getattr(link, "passthrough_split", False))
        engine_channel: Any = link.pt_demux if self._split else link.demux
        self.engine = PassthroughEngine(
            engine_channel,
            bus=self.bus,
            cfg=cfg.passthrough,
            log=get_logger(f"hmi.protocol.pt.{link.endpoint}"),
            endpoint=link.endpoint,
            name=f"485-{link.endpoint}",
            busy_probe=None if self._split else link.at.busy,
            annotate=describe_wheeltec_frame,
        )
        self.templates = TemplateLibrary(
            self.engine,
            path=templates_path or self._templates_path(cfg),
            cfg=cfg.passthrough,
            log=get_logger("hmi.protocol.templates"),
        )
        self._ticker = Ticker(
            f"pt-{link.endpoint}-stats",
            self.publish_stats,
            stats_interval_s,
            log=self.log,
        )
        self.sent = 0
        self.failed = 0

    @staticmethod
    def _templates_path(cfg: AppConfig) -> Path:
        raw = Path(cfg.app.templates_file)
        return raw if raw.is_absolute() else project_root() / raw

    # ------------------------------------------------------------------ 起停
    def start(self) -> bool:
        if self._split:
            # 透传线独立打开；失败不阻断（串口没插时界面仍可用，发送会给出
            # "串口未打开"的明确失败行）。
            self.link.open_passthrough()
        ok = self.engine.start()
        if ok:
            self._ticker.start()
        return ok

    def stop(self) -> None:
        self._ticker.stop()
        self.engine.cancel()
        self.engine.stop()
        if self._split:
            self.link.close_passthrough()

    @property
    def split(self) -> bool:
        """透传是否与 AT 分线（UI 用它决定"485 线"标签盯哪条通道）。"""
        return self._split

    @property
    def subscribed(self) -> bool:
        return self.engine.subscribed

    def configure(self, **changes: Any) -> None:
        """改透传默认编码/结束符/缓冲上限（UI 顶部下拉框直连）。"""
        self.engine.configure(**changes)

    # ------------------------------------------------------------------ 发送
    def send(self, payload: str | bytes, **kwargs: Any) -> dict[str, Any]:
        """只发不等。异常在这里收口成 ``{ok: False}``，按钮不该弹崩溃框。

        被拒/写失败的帧虽然没上线，但**组好的字节已存在**（挂在异常的
        ``frame_bytes`` 上）——失败记录照样带上它们，界面上能看到"本来要发什么"。
        """
        try:
            rec = self.engine.send(payload, **kwargs)
        except (ValueError, RuntimeError) as exc:
            self.failed += 1
            frame = bytes(getattr(exc, "frame_bytes", b"") or b"")
            self._publish_result(
                CommandResult(
                    ts=time.time(),
                    name=str(kwargs.get("note") or "raw"),
                    sent_bytes=frame,
                    ok=False,
                    error=str(exc),
                )
            )
            return {"ok": False, "detail": str(exc), "hex": _hex(frame)}
        except OSError as exc:
            self.failed += 1
            detail = f"写入失败：{exc}"
            frame = bytes(getattr(exc, "frame_bytes", b"") or b"")
            self._publish_result(
                CommandResult(
                    ts=time.time(),
                    name=str(kwargs.get("note") or "raw"),
                    sent_bytes=frame,
                    ok=False,
                    error=detail,
                )
            )
            self._alarm("PT_WRITE_FAILED", detail)
            return {"ok": False, "detail": detail, "hex": _hex(frame)}
        self.sent += 1
        return {
            "ok": True,
            "detail": f"已发送 {len(rec.data)} 字节",
            "hex": rec.hex(),
            "ascii": rec.ascii(),
            "ts": rec.ts,
        }

    def request(self, payload: str | bytes, **kwargs: Any) -> CommandResult:
        """发一条并等待应答；引擎没发事件的（失败）由这里补发。

        异常分支只负责把异常变成一个失败的 ``CommandResult``，计数与补发统一交给
        末尾那一段——否则"写失败"会同时被 ``except`` 和 ``else`` 记一次，
        界面上的失败数翻倍。
        """
        try:
            res = self.engine.request(payload, **kwargs)
        except OSError as exc:
            detail = f"写入失败：{exc}"
            res = CommandResult(
                ts=time.time(),
                name=str(kwargs.get("name") or "raw"),
                sent_bytes=bytes(getattr(exc, "frame_bytes", b"") or b""),
                ok=False,
                error=detail,
            )
            self._alarm("PT_WRITE_FAILED", detail)
        except (ValueError, RuntimeError) as exc:
            res = CommandResult(
                ts=time.time(),
                name=str(kwargs.get("name") or "raw"),
                sent_bytes=bytes(getattr(exc, "frame_bytes", b"") or b""),
                ok=False,
                error=str(exc),
            )
        if res.ok:
            self.sent += 1
        else:
            self.failed += 1
            self._publish_result(res)
        return res

    # ------------------------------------------------------------------ 模板
    def run_template(
        self,
        template_id: str,
        values: dict[str, Any] | None = None,
    ) -> CommandResult:
        """一键下发模板。不等应答的模板引擎不会发事件，这里统一补一条结论。"""
        res = self.templates.run(template_id, values=values)
        if res.ok:
            self.sent += 1
            if not res.matched:
                self._publish_result(res)
        else:
            self.failed += 1
            self._publish_result(res)
        return res

    def run_many(
        self,
        items: list[tuple[str, dict[str, Any] | None]],
        *,
        gap_s: float = 0.05,
    ) -> list[CommandResult]:
        """按顺序连发（"一键开闸 → 延时 → 到位"这类演示脚本用）。"""
        out: list[CommandResult] = []
        for tid, values in items:
            out.append(self.run_template(tid, values))
            if not out[-1].ok:
                break
            if gap_s > 0:
                time.sleep(gap_s)
        return out

    def preview(self, template_id: str, values: dict[str, Any] | None = None) -> dict[str, Any]:
        """不上线，只告诉用户将要发哪些字节；参数不全时返回校验问题。"""
        tpl = self.templates.get(template_id)
        if tpl is None:
            return {"ok": False, "detail": "模板不存在", "hex": ""}
        problems = self.templates.validate(tpl, values)
        if problems:
            return {"ok": False, "detail": "；".join(problems), "hex": ""}
        data = self.templates.preview(tpl, values)
        return {
            "ok": True,
            "detail": f"{len(data)} 字节",
            "hex": ByteRecord(data=data, direction="tx").hex(),
        }

    def preview_manual(
        self,
        payload: str,
        *,
        mode: str | None = None,
        suffix: str | None = None,
        crc16_append: bool = False,
    ) -> dict[str, Any]:
        """手动指令的上线字节预览：复用 build_frame，但一个字都不发。

        非法 Hex、未知编码这类校验错误在这里收口成 ``ok=False``，让控制台的
        预览标签可以直接显示问题而不用弹窗。默认值取当前透传配置。
        """
        cfg = self.cfg.passthrough
        try:
            data = build_frame(
                payload,
                mode=mode or cfg.mode,
                suffix=suffix if suffix is not None else cfg.suffix,
                crc16_append=crc16_append,
            )
        except ValueError as exc:
            return {"ok": False, "detail": str(exc), "hex": ""}
        if not data:
            return {"ok": False, "detail": "内容为空", "hex": ""}
        return {
            "ok": True,
            "detail": f"{len(data)} 字节",
            "hex": ByteRecord(data=data, direction="tx").hex(),
        }

    # ------------------------------------------------------------------ 查询
    def records(self, after_id: int = 0) -> tuple[list[ByteRecord], int]:
        return self.engine.records(after_id)

    def all_records(self) -> list[ByteRecord]:
        return self.engine.all_records()

    def clear(self) -> int:
        return self.engine.clear()

    def resolve(self, payload: str | bytes) -> bytes:
        return self.engine.resolve(payload)

    # ------------------------------------------------------------------ 事件
    def stats(self) -> PassthroughStats:
        return self.engine.stats()

    def publish_stats(self) -> PassthroughStats:
        st = self.stats()
        if self.bus is not None:
            self.bus.publish(Events.PT_STATS, st)
        return st

    def _publish_result(self, res: CommandResult) -> None:
        if self.bus is None:
            return
        self.bus.publish(Events.CMD_RESULT, res)

    def _alarm(self, code: str, message: str, level: str = AlarmLevel.WARN.value) -> None:
        if self.bus is None:
            return
        self.bus.publish(
            Events.ALARM,
            AlarmEvent(
                ts=time.time(),
                level=level,
                source=f"passthrough/{self.link.endpoint}",
                code=code,
                message=message,
            ),
        )

    # ------------------------------------------------------------------ 汇总
    def summary(self) -> dict[str, Any]:
        st = self.stats()
        return {
            "endpoint": self.link.endpoint,
            "subscribed": self.subscribed,
            "connected": st.connected,
            "tx_frames": st.tx_frames,
            "rx_frames": st.rx_frames,
            "tx_bytes": st.tx_bytes,
            "rx_bytes": st.rx_bytes,
            "timeouts": st.timeouts,
            "errors": st.errors,
            "records": len(self.engine.all_records()),
            "templates": len(self.templates),
            "sent": self.sent,
            "failed": self.failed,
            "ticker": self._ticker.stats(),
        }

    def __repr__(self) -> str:
        return (
            f"<PassthroughService {self.link.endpoint} tx={self.engine.tx_frames} "
            f"rx={self.engine.rx_frames} templates={len(self.templates)}>"
        )
