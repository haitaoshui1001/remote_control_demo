"""485 透传引擎：把"发一段字节 / 收一段字节"变成可审计的报文与请求-应答配对。

层次位置
--------
    ui / services
        |
    protocol.PassthroughEngine     <- 本文件
        |
    protocol.ChannelDemux          <- AT 与透传已经在这里分好流
        |
    transport.ByteTransport

为什么引擎不自己 read
--------------------
本机只有一个 485 转 USB 口，AT 指令、AT 响应、远端设备透传过来的数据全混在
同一条字节流里。分流必须由 ``ChannelDemux`` 这个**唯一读者**完成，引擎只订阅
"已判定为透传"的帧，于是：

* 它不需要知道 AT 长什么样，也不会把 ``+RSSI:-72`` 当成设备回复；
* AT 请求窗口打开期间字节全被窗口收走，引擎自然安静，不会误配应答。

它也不懂协议
------------
Modbus RTU、私有帧、纯 ASCII 文本，引擎一律只做三件事：**编码（hex/ascii）、
可选追加 CRC16、可选追加结束符**，然后原样上线；收侧只做**记时间戳 + 环形
存档 + 按前缀/长度判定应答**。设备协议语义留给模板库（``templates.py``）和
使用者，这样换一台设备不必改这一层。想让收进的帧在流水表里带上人话备注，
通过构造参数 ``annotate`` 注入一个"字节→一句话"的纯函数即可——引擎照旧
不认识任何协议，回调抛异常也只是备注留空，绝不影响收包。

半双工纪律
----------
485 是半双工总线，AT 正在一问一答时插入透传数据会让两边都解析失败。默认在
AT 窗口开启时拒绝发送，并把探测函数做成注入项（``busy_probe``）：想改成
"透传优先"的现场不必动这里。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from ..core.config import PassthroughCfg
from ..core.events import EventBus, Events, get_bus
from ..core.logger import get_logger
from ..core.models import ByteRecord, CommandResult, Direction, PassthroughStats
from ..core.utils import (
    RateMeter,
    RingBuffer,
    add_crlf,
    append_crc16,
    bytes_to_hex,
    to_bytes,
)

__all__ = ["PassthroughEngine", "build_frame", "reply_matches"]

BusyProbe = Callable[[], bool]


def build_frame(
    payload: str | bytes,
    *,
    mode: str = "hex",
    suffix: str = "none",
    crc16_append: bool = False,
) -> bytes:
    """纯函数：文本指令 -> 上线字节。

    顺序固定为 **先 CRC 后结束符**：Modbus 一类协议把 CRC 算在正文上，若先补
    结束符再算 CRC，校验值就错了。
    """
    raw = to_bytes(payload, mode)
    if crc16_append:
        raw = append_crc16(raw)
    return add_crlf(raw, suffix)


def reply_matches(
    frame: bytes,
    *,
    prefix: str = "",
    prefix_mode: str = "hex",
    min_bytes: int = 0,
) -> bool:
    """应答判定：长度够 + 前缀对得上。

    ``prefix`` 用与请求相同的编码模式解释，这样 hex 模板写 ``0103``、ASCII
    模板写 ``OK`` 都能自然表达；留空表示"来什么算什么"。
    """
    if len(frame) < max(0, int(min_bytes)):
        return False
    if not prefix:
        return True
    try:
        want = to_bytes(prefix, prefix_mode)
    except ValueError:
        return False
    return frame.startswith(want)


def _probe_demux(channel: Any) -> BusyProbe:
    """默认忙探测：借用 ``ChannelDemux.stats()["requester"]`` 判断 AT 窗口是否开着。"""

    def probe() -> bool:
        try:
            return bool(channel.stats().get("requester"))
        except Exception:
            return False

    return probe


class _Pending:
    """一次 request() 的等待状态。放在内部类外面是为了让 ``_on_frame`` 保持简单。"""

    __slots__ = ("cv", "frame", "prefix", "prefix_mode", "min_bytes", "deadline")

    def __init__(
        self,
        *,
        prefix: str,
        prefix_mode: str,
        min_bytes: int,
        timeout_ms: int,
    ) -> None:
        self.cv = threading.Condition()
        self.prefix = prefix
        self.prefix_mode = prefix_mode
        self.min_bytes = min_bytes
        self.deadline = time.monotonic() + max(0.0, timeout_ms / 1000.0)
        self.frame: bytes | None = None

    def offer(self, frame: bytes) -> bool:
        """收到一帧，若匹配则唤醒等待者。返回是否被认领。"""
        if self.frame is not None:
            return False
        if not reply_matches(
            frame, prefix=self.prefix, prefix_mode=self.prefix_mode, min_bytes=self.min_bytes
        ):
            return False
        with self.cv:
            if self.frame is None:
                self.frame = frame
                self.cv.notify_all()
                return True
        return False


class PassthroughEngine:
    """485 透传：发送、接收、存档、应答配对、速率统计。"""

    def __init__(
        self,
        channel: Any,
        *,
        bus: EventBus | None = None,
        cfg: PassthroughCfg | None = None,
        log: logging.Logger | None = None,
        endpoint: str = "local",
        name: str = "485",
        busy_probe: BusyProbe | None = None,
        allow_unstarted: bool = True,
        annotate: Callable[[bytes], str] | None = None,
    ) -> None:
        self.channel = channel
        self.bus = bus or get_bus()
        self.cfg = cfg or PassthroughCfg()
        self.log = log or get_logger(f"hmi.protocol.pt.{endpoint}")
        self.endpoint = endpoint
        self.name = name
        self._busy_probe = busy_probe or _probe_demux(channel)
        self._allow_unstarted = allow_unstarted
        self._annotate = annotate

        self._lock = threading.Lock()
        self._records: RingBuffer = RingBuffer(maxlen=self.cfg.max_records)
        self._pending: _Pending | None = None
        self._sink: Callable[[bytes, float], None] | None = None
        self._subscribed = False

        self._tx_meter = RateMeter(5.0)
        self._rx_meter = RateMeter(5.0)
        self._rx_frame_meter = RateMeter(5.0)
        self.tx_bytes = 0
        self.rx_bytes = 0
        self.tx_frames = 0
        self.rx_frames = 0
        self.timeouts = 0
        self.refusals = 0
        self.errors = 0
        self.last_tx_ts = 0.0
        self.last_rx_ts = 0.0

    # ------------------------------------------------------------------ 装配
    def start(self) -> bool:
        """订阅分流器的透传帧；重复调用无副作用。"""
        with self._lock:
            if self._subscribed:
                return True
            if self._sink is None:
                self._sink = self._on_frame
            sink = self._sink
        try:
            self.channel.subscribe_passthrough(sink)
        except AttributeError:
            self.log.warning("%s: 通道不支持透传订阅", self.name)
            return False
        with self._lock:
            self._subscribed = True
        self.log.debug("%s: 已订阅透传帧", self.name)
        return True

    def stop(self) -> bool:
        with self._lock:
            if not self._subscribed:
                return False
            self._subscribed = False
            sink = self._sink
        try:
            self.channel.unsubscribe(sink)
        except AttributeError:
            pass
        self._abandon_pending("引擎已停止")
        return True

    @property
    def subscribed(self) -> bool:
        with self._lock:
            return self._subscribed

    def configure(self, **changes: Any) -> None:
        """运行时改默认编码/结束符/缓冲上限，供 UI 顶部下拉框直接调用。"""
        with self._lock:
            for key, value in changes.items():
                if not hasattr(self.cfg, key):
                    raise AttributeError(f"未知透传配置项: {key}")
                setattr(self.cfg, key, value)
            if "max_records" in changes:
                self._records = RingBuffer(maxlen=self.cfg.max_records)

    # ------------------------------------------------------------------ 编码
    def resolve(self, payload: str | bytes) -> bytes:
        """按当前 cfg 默认值编码，供 UI 实时预览"将要上线的字节"。"""
        return build_frame(
            payload,
            mode=self.cfg.mode,
            suffix=self.cfg.suffix,
        )

    # ------------------------------------------------------------------ 发送
    def send(
        self,
        payload: str | bytes,
        *,
        mode: str | None = None,
        suffix: str | None = None,
        crc16_append: bool | None = None,
        note: str = "",
        kind: str = "manual",
        force: bool = False,
    ) -> ByteRecord:
        """把一段指令发到线上并记账。返回记录不代表设备已收到（485 无链路层确认）。"""
        frame = build_frame(
            payload,
            mode=mode or self.cfg.mode,
            suffix=suffix if suffix is not None else self.cfg.suffix,
            crc16_append=bool(crc16_append),
        )
        if not frame:
            raise ValueError("空指令：编码后没有任何字节")
        if not force and self._busy_probe():
            self.refusals += 1
            exc = RuntimeError("AT 通道忙（半双工线），指令未发送；稍后重试或先停轮询")
            exc.frame_bytes = frame  # type: ignore[attr-defined]
            raise exc

        ts = time.time()
        try:
            self.channel.write(frame)
        except Exception as exc:
            self.errors += 1
            self.log.warning("%s: 发送失败: %s", self.name, exc)
            exc.frame_bytes = frame  # type: ignore[attr-defined]
            raise
        with self._lock:
            self.tx_bytes += len(frame)
            self.tx_frames += 1
            self.last_tx_ts = ts
            self._tx_meter.add(len(frame), ts)
        rec = ByteRecord(
            ts=ts,
            direction=Direction.TX.value,
            data=frame,
            channel=self.name,
            endpoint=self.endpoint,
            note=note,
            kind=kind,
        )
        if self.cfg.echo_tx:
            self._store(rec)
        return rec

    def request(
        self,
        payload: str | bytes,
        *,
        mode: str | None = None,
        suffix: str | None = None,
        crc16_append: bool | None = None,
        timeout_ms: int | None = None,
        match_prefix: str = "",
        min_reply_bytes: int = 0,
        template_id: str = "",
        name: str = "",
        note: str = "",
    ) -> CommandResult:
        """发一条指令并等待匹配的应答帧，给出 RTT 与判定结论。

        没有配对窗口时收到帧也照常存档——只是这一条请求判超时。
        """
        timeout = int(timeout_ms if timeout_ms is not None else self.cfg.reply_timeout_ms)
        enc_mode = mode or self.cfg.mode
        pending = _Pending(
            prefix=match_prefix,
            prefix_mode=enc_mode,
            min_bytes=min_reply_bytes,
            timeout_ms=timeout,
        )
        # 配对窗口必须先于发送登记：485 从机可能在 ``write()`` 返回前就把应答送上总线，
        # 此时若 ``_pending`` 还是 None，这一帧只会被存档，本次请求白白判超时。
        with self._lock:
            prev = self._pending
            if prev is not None:
                self.log.debug("%s: 上一个请求仍未结束，本次不配对", self.name)
            self._pending = pending
        try:
            rec = self.send(
                payload,
                mode=enc_mode,
                suffix=suffix,
                crc16_append=crc16_append,
                note=note,
                kind="command" if template_id else "manual",
            )
        except (ValueError, RuntimeError) as exc:
            self._unset_pending(pending, prev)
            return CommandResult(
                template_id=template_id,
                name=name or template_id,
                sent_bytes=bytes(getattr(exc, "frame_bytes", b"") or b""),
                ok=False,
                error=str(exc),
            )
        except Exception:
            self._unset_pending(pending, prev)
            raise
        sent_mono = time.monotonic()
        try:
            with pending.cv:
                while pending.frame is None:
                    remain = pending.deadline - time.monotonic()
                    if remain <= 0:
                        break
                    pending.cv.wait(remain)
                reply = pending.frame
        finally:
            with self._lock:
                if self._pending is pending:
                    self._pending = None

        rtt_ms = (time.monotonic() - sent_mono) * 1000.0
        if reply is None:
            self.timeouts += 1
            return CommandResult(
                ts=rec.ts,
                template_id=template_id,
                name=name or f"raw:{bytes_to_hex(rec.data)}",
                sent_bytes=rec.data,
                reply=b"",
                ok=False,
                rtt_ms=round(rtt_ms, 1),
                error=f"应答超时（{timeout} ms）",
                matched=False,
            )
        result = CommandResult(
            ts=rec.ts,
            template_id=template_id,
            name=name,
            sent_bytes=rec.data,
            reply=reply,
            ok=True,
            rtt_ms=round(rtt_ms, 1),
            matched=True,
        )
        self._publish(Events.CMD_RESULT, result)
        return result

    def cancel(self) -> bool:
        """UI 关页/切通道时放弃当前配对等待。"""
        return self._abandon_pending("已取消等待")

    def _unset_pending(self, pending: _Pending, prev: _Pending | None) -> None:
        with self._lock:
            if self._pending is pending:
                self._pending = prev

    def _abandon_pending(self, reason: str) -> bool:
        with self._lock:
            pending = self._pending
            self._pending = None
        if pending is None:
            return False
        with pending.cv:
            pending.deadline = 0.0
            pending.cv.notify_all()
        self.log.debug("%s: 放弃配对等待（%s）", self.name, reason)
        return True

    # ------------------------------------------------------------------ 接收
    def _on_frame(self, data: bytes, ts: float) -> None:
        """``ChannelDemux`` 的透传帧回调（在该线程里执行，只做记账，不阻塞）。"""
        if not data:
            return
        pending: _Pending | None = None
        with self._lock:
            self.rx_bytes += len(data)
            self.rx_frames += 1
            self.last_rx_ts = ts
            self._rx_meter.add(len(data), ts)
            self._rx_frame_meter.add(1, ts)
            pending = self._pending
        note = self._describe(data)
        rec = ByteRecord(
            ts=ts,
            direction=Direction.RX.value,
            data=bytes(data),
            channel=self.name,
            endpoint=self.endpoint,
            kind="reply" if pending else "rx",
            note=note,
        )
        self._store(rec)
        if pending is not None and pending.offer(rec.data):
            return

    def _describe(self, data: bytes) -> str:
        """注入的"字节→一句话"备注；回调缺席或出错都只得到空备注。"""
        if self._annotate is None:
            return ""
        try:
            return str(self._annotate(bytes(data)) or "")
        except Exception as exc:
            self.log.debug("%s: 备注回调失败: %s", self.name, exc)
            return ""

    def _store(self, rec: ByteRecord) -> None:
        with self._lock:
            self._records.push(rec)
        self._publish(Events.PT_BYTES, rec)

    # ------------------------------------------------------------------ 查询
    def records(self, after_id: int = 0) -> tuple[list[ByteRecord], int]:
        """增量取报文，供 UI 表格按游标拉取。返回 (记录, 新游标)。"""
        with self._lock:
            buf = self._records
        out: list[ByteRecord] = []
        cursor = after_id
        for rid, item in list(buf.iter_new(after_id)):
            out.append(item)  # type: ignore[arg-type]
            cursor = rid
        return out, cursor

    def all_records(self) -> list[ByteRecord]:
        with self._lock:
            return list(self._records.snapshot())  # type: ignore[arg-type]

    def clear(self) -> int:
        """清空存档（不影响计数），返回清除条数。"""
        with self._lock:
            old = len(self._records)
            self._records.clear()
        return old

    def stats(self) -> PassthroughStats:
        now = time.monotonic()
        with self._lock:
            pending = 1 if self._pending is not None else 0
            return PassthroughStats(
                tx_bytes=self.tx_bytes,
                rx_bytes=self.rx_bytes,
                tx_frames=self.tx_frames,
                rx_frames=self.rx_frames,
                tx_rate_bps=round(self._tx_meter.rate(now), 1),
                rx_rate_bps=round(self._rx_meter.rate(now), 1),
                rx_rate_fps=round(self._rx_frame_meter.rate(now), 2),
                last_tx_ts=self.last_tx_ts,
                last_rx_ts=self.last_rx_ts,
                pending=pending,
                timeouts=self.timeouts,
                errors=self.errors + self.refusals,
                connected=bool(getattr(self.channel, "is_attached", lambda: True)()),
                port=str(getattr(self.channel, "name", self.name)),
            )

    def summary(self) -> dict[str, Any]:
        st = self.stats()
        return {
            "endpoint": self.endpoint,
            "tx_frames": st.tx_frames,
            "rx_frames": st.rx_frames,
            "tx_bytes": st.tx_bytes,
            "rx_bytes": st.rx_bytes,
            "tx_rate_bps": st.tx_rate_bps,
            "rx_rate_bps": st.rx_rate_bps,
            "timeouts": st.timeouts,
            "errors": st.errors,
            "records": len(self.all_records()),
        }

    def _publish(self, topic: str, payload: object) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(topic, payload)
        except Exception:
            self.log.exception("%s: 事件发布异常 %s", self.name, topic)

    def __repr__(self) -> str:
        return (
            f"PassthroughEngine(name={self.name!r}, endpoint={self.endpoint!r}, "
            f"tx={self.tx_frames}, rx={self.rx_frames}, subscribed={self._subscribed})"
        )
