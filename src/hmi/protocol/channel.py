"""共享 485 通道的分流器（AT 指令 与 设备透传 走同一根线）。

本机这一侧只有一个 485 转 USB 串口：AT 指令、AT 响应、以及远端设备通过
无线透传过来的数据，全部混在同一条字节流里。如果让 AtClient 和透传服务
各自去 read，就会互相抢字节。因此这里做成**唯一读者**：

    transport.read_once()            ← 只有 Demux 调用
        ├─ classify_chunk() == "at"  → AT 队列（请求窗口 / 主动上报回调）
        └─ 否则                       → 透传帧（按 rx_idle_gap_ms 切帧）后回调

请求窗口（begin/collect）内只有**纯可打印 ASCII 块**才判给 AT；含二进制
字节的块一定是 485 从机的透传应答——遥询与透传共用一根线，从机应答随时
可能插进窗口，若一并吞进请求缓冲就会造成"应答超时"的假故障。空闲期间
的判定规则更严格："整块可打印 ASCII **且**任一非空行以 + / AT+ / OK /
ERROR 开头"，因此：

  ``+MODE:ap`` / ``OK`` / ``ERROR`` / ``AT+MODE?``(回显) → AT
  ``@DEV rssi=-71 tx=100``（仿真设备的数据）             → 透传
  ``01 03 00 00 00 01``（Modbus 二进制帧）               → 透传

判定词由上层从配置（ok_tokens/err_tokens + 固定 ``+``/``AT+``）传入，
便于换成"设备也会回 OK"这类特殊现场时调整。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from ..core.utils import RateMeter
from .at_codec import CHUNK_AT, CHUNK_PASS, classify_chunk, is_at_text_bytes
from .at_codec import classify as classify_line

FrameSink = Callable[[bytes, float], None]
LineSink = Callable[[str], None]

#: 与设备数据无关、一定是电台自身说话的行的前缀
DEFAULT_AT_MARKERS: tuple[str, ...] = ("+", "AT+")


class ChannelDemux:
    """一条 ByteTransport 的唯一读者 + AT/透传分流器。"""

    def __init__(
        self,
        name: str = "demux",
        *,
        idle_gap_ms: float = 30.0,
        read_timeout_ms: int = 50,
        max_chunk: int = 4096,
        log: logging.Logger | None = None,
    ) -> None:
        self.name = name
        self.log = log or logging.getLogger(f"hmi.channel.{name}")
        self.idle_gap_ms = max(1.0, float(idle_gap_ms))
        self.read_timeout_ms = int(read_timeout_ms)
        self.max_chunk = int(max_chunk)
        self.at_markers = list(DEFAULT_AT_MARKERS)
        self.ok_tokens: tuple[str, ...] = ("OK",)
        self.err_tokens: tuple[str, ...] = ("ERROR", "ERR", "FAIL")

        self._transport: Any = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)

        self._request_buf = bytearray()
        self._requester: str | None = None
        self._at_sinks: list[LineSink] = []
        self._pt_sinks: list[FrameSink] = []
        self._state_sinks: list[Callable[[object, str], None]] = []

        self._pt_buf = bytearray()
        self._pt_last_ts = 0.0

        self._at_meter = RateMeter(5.0)
        self._pt_meter = RateMeter(5.0)
        self.bytes_at = 0
        self.bytes_pt = 0
        self.frames_pt = 0
        self.ambiguous = 0
        self.errors = 0

    # ------------------------------------------------------------------ 装配
    def attach(self, transport: Any, *, start: bool = True) -> None:
        """绑定通道；重复 attach 会先停掉旧线程。"""
        with self._lock:
            if self._transport is transport and self._thread and self._thread.is_alive():
                return
            old = self._transport
        self.stop()
        if old is not None and old is not transport:
            try:
                old.remove_state_listener(self._on_state)
            except (AttributeError, ValueError):
                pass
        self._transport = transport
        try:
            transport.add_state_listener(self._on_state)
        except AttributeError:
            self.log.debug("%s: 通道不支持状态监听", name_of(transport))
        if start:
            self.start()

    def detach(self) -> None:
        self.stop()
        with self._lock:
            transport = self._transport
            self._transport = None
        if transport is not None:
            try:
                transport.remove_state_listener(self._on_state)
            except (AttributeError, ValueError):
                pass

    @property
    def transport(self) -> Any:
        return self._transport

    def is_attached(self) -> bool:
        with self._lock:
            return self._transport is not None

    def configure(
        self,
        *,
        ok_tokens: tuple[str, ...] | None = None,
        err_tokens: tuple[str, ...] | None = None,
        extra_markers: tuple[str, ...] | None = None,
        idle_gap_ms: float | None = None,
    ) -> None:
        with self._lock:
            if ok_tokens:
                self.ok_tokens = tuple(ok_tokens)
            if err_tokens:
                self.err_tokens = tuple(err_tokens)
            if extra_markers:
                merged = list(DEFAULT_AT_MARKERS)
                for m in extra_markers:
                    if m not in merged:
                        merged.append(m)
                self.at_markers = merged
            if idle_gap_ms:
                self.idle_gap_ms = max(1.0, float(idle_gap_ms))

    # ------------------------------------------------------------------ 订阅
    def subscribe_at(self, sink: LineSink) -> None:
        """空闲期收到 AT 行（模块主动上报，如 +CONNECTED / PAIR SUCCESS）。"""
        with self._lock:
            self._at_sinks.append(sink)

    def subscribe_passthrough(self, sink: FrameSink) -> None:
        """收到一帧透传数据：(data, ts)。"""
        with self._lock:
            self._pt_sinks.append(sink)

    def subscribe_state(self, sink: Callable[[object, str], None]) -> None:
        with self._lock:
            self._state_sinks.append(sink)

    def unsubscribe(self, sink: object) -> None:
        with self._lock:
            for bucket in (self._at_sinks, self._pt_sinks, self._state_sinks):
                if sink in bucket:
                    bucket.remove(sink)

    # ------------------------------------------------------------------ 线程
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name=f"demux-{self.name}")
            self._thread.daemon = True
            self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        thread = self._thread
        self._thread = None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        self.log.debug("%s: 分流线程启动", self.name)
        while not self._stop.is_set():
            transport = self._transport
            if transport is None:
                time.sleep(0.05)
                continue
            try:
                data = transport.read_once(self.max_chunk, self.read_timeout_ms)
            except Exception as exc:
                self.errors += 1
                self.log.warning("%s: read_once 异常: %s", self.name, exc)
                time.sleep(0.05)
                continue
            if data:
                self._dispatch(data)
            self._maybe_flush_passthrough()
        self._flush_passthrough()
        self.log.debug("%s: 分流线程退出", self.name)

    # ------------------------------------------------------------------ 分流
    def _dispatch(self, data: bytes) -> None:
        ts = time.time()
        with self._lock:
            token = self._requester
            markers = tuple(self.at_markers) + self.ok_tokens + self.err_tokens
            if token is not None:
                # 窗口内只有纯 ASCII 才可能是 AT 应答/回显；二进制块是 485
                # 从机的透传应答，必须照常投递，否则透传会假性"应答超时"。
                kind = CHUNK_AT if is_at_text_bytes(data) else CHUNK_PASS
            else:
                kind = classify_chunk(data, markers)
            if kind == CHUNK_AT:
                self.bytes_at += len(data)
                self._at_meter.add(len(data))
                if token is not None:
                    self._request_buf.extend(data)
                    self._cv.notify_all()
                    return
                sinks = list(self._at_sinks)
            else:
                self.bytes_pt += len(data)
                self._pt_meter.add(len(data))
                self._pt_buf.extend(data)
                self._pt_last_ts = ts
                return
        self._emit_lines(data.decode("utf-8", "replace"), sinks)

    def _emit_lines(self, text: str, sinks: list[LineSink]) -> None:
        """空闲 AT 行：跳过空行与回显，只把真正的新息交给订阅者。"""
        for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = raw.strip()
            if not line:
                continue
            parsed = classify_line(line, self.ok_tokens, self.err_tokens)
            if parsed.kind == "other" and not parsed.name:
                self.ambiguous += 1
            for sink in sinks:
                try:
                    sink(line)
                except Exception:
                    self.log.exception("%s: AT 行回调异常: %s", self.name, line[:60])

    def _maybe_flush_passthrough(self) -> None:
        if not self._pt_buf:
            return
        if (time.time() - self._pt_last_ts) * 1000.0 < self.idle_gap_ms:
            return
        self._flush_passthrough()

    def _flush_passthrough(self) -> None:
        with self._lock:
            if not self._pt_buf:
                return
            frame = bytes(self._pt_buf)
            ts = self._pt_last_ts
            self._pt_buf.clear()
            self.frames_pt += 1
            sinks = list(self._pt_sinks)
        for sink in sinks:
            try:
                sink(frame, ts)
            except Exception:
                self.log.exception("%s: 透传帧回调异常 len=%s", self.name, len(frame))

    def flush_now(self) -> None:
        """让即将空闲的一帧立刻上屏（UI 切换、自检收尾用）。"""
        self._flush_passthrough()

    # ------------------------------------------------------------------ AT 窗口
    def begin(self, token: str) -> None:
        with self._cv:
            self._requester = token
            self._request_buf.clear()

    def abort(self, token: str) -> None:
        with self._cv:
            if self._requester == token:
                self._requester = None
                self._request_buf.clear()

    def collect(self, token: str, timeout_ms: int) -> bytes:
        """在请求窗口内等待字节；返回本次收到的原始字节（可能为空）。"""
        deadline = time.monotonic() + max(0.0, timeout_ms / 1000.0)
        with self._cv:
            if self._requester != token:
                return b""
            while not self._request_buf:
                remain = deadline - time.monotonic()
                if remain <= 0 or self._stop.is_set():
                    return b""
                self._cv.wait(remain)
            if self._requester != token:
                return b""
            data = bytes(self._request_buf)
            self._request_buf.clear()
            return data

    def release(self, token: str) -> bytes:
        """结束窗口，把窗口内残留字节退回透传（避免丢数据）。"""
        with self._cv:
            leftover = bytes(self._request_buf) if self._requester == token else b""
            if self._requester == token:
                self._requester = None
                self._request_buf.clear()
            self._cv.notify_all()
        if leftover:
            self._requeue(leftover)
        return b""

    def _requeue(self, data: bytes) -> None:
        """窗口结束后仍有字节到达：按透传处理（保守，宁可少判 AT）。"""
        self.bytes_pt += len(data)
        self._pt_meter.add(len(data))
        with self._lock:
            self._pt_buf.extend(data)
            self._pt_last_ts = time.time()

    # ------------------------------------------------------------------ 状态
    def _on_state(self, state: object, detail: str) -> None:
        with self._lock:
            sinks = list(self._state_sinks)
        for sink in sinks:
            try:
                sink(state, detail)
            except Exception:
                self.log.exception("%s: 状态回调异常", self.name)

    def write(self, data: bytes) -> int:
        transport = self._transport
        if transport is None:
            raise RuntimeError("通道未绑定，无法发送")
        return int(transport.write(data))

    def stats(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            return {
                "name": self.name,
                "attached": self._transport is not None,
                "running": self.running(),
                "requester": self._requester or "",
                "bytes_at": self.bytes_at,
                "bytes_pt": self.bytes_pt,
                "frames_pt": self.frames_pt,
                "pending_pt_bytes": len(self._pt_buf),
                "at_rate_bps": round(self._at_meter.rate(now), 1),
                "pt_rate_bps": round(self._pt_meter.rate(now), 1),
                "ambiguous": self.ambiguous,
                "errors": self.errors,
                "idle_gap_ms": self.idle_gap_ms,
            }

    def __enter__(self) -> ChannelDemux:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.stop()
        return False


def name_of(obj: object) -> str:
    return str(getattr(obj, "name", type(obj).__name__))
