"""AT 客户端：把"发一行、收几行、判成败"的时序固化成可复用组件。

为什么必须封装：
- 模块的响应形态不统一（``+NAME:value``、``+CONNECTED`` 无 OK、
  ``+AT+CHAN_LIST:...`` 响应名自带 ``AT+`` 前缀、``+TXPOWER:20dbm`` 带单位后缀），
  判定逻辑只写在这一处，上层不再各自拼字符串；
- 本机 AT 与 485 透传共用一根线，对话只能在 ``ChannelDemux`` 的请求窗口内进行：
  begin →（可选唤醒）→ 静默 → 写 → 收 → release。窗口期间所有字节判给 AT，
  窗口结束后残留字节由通道退回透传，不丢数据；
- 同一时刻只允许一个对话（``self._lock``），否则两条指令的响应会交织在一起。

关于 ``pause_passthrough``：这里不做"是否占用通道"的分支（不占窗口就拿不到
可靠响应），而是把它实现成"对话期间通道被占用"的对外可见状态——透传服务用
``busy()`` 查询并在 UI 上显示等待，语义直观且两层仍然解耦。

所有对话（含被拒绝的下发、以及通道没打开而未能上线的尝试）都会以
``AtExchange`` 发到 ``Events.AT_EXCHANGE``，UI 的 AT 日志页、信道遥测轮询、
自检脚本消费同一份数据——日志页空白只可能是"没点"，不可能是"点了但没留痕"。
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from typing import Any, NoReturn

from ..core.config import AtCfg
from ..core.events import EventBus, Events, get_bus
from ..core.models import AtChannel, AtExchange, Endpoint
from . import param_registry as registry
from .at_codec import (
    KIND_ECHO,
    KIND_STATUS,
    KIND_VALUE,
    LineSplitter,
    classify,
    ending_bytes,
    format_at,
    normalize_name,
    split_value_line,
)
from .channel import ChannelDemux

__all__ = ["AtClient", "AtError", "response_value"]

_seq = itertools.count(1)


class AtError(RuntimeError):
    """AT 通道不可用（未绑定传输层、未打开、或写入失败）。"""


def _has_delimiter(line: str) -> bool:
    """响应行中是否存在"名字 / 正文"的分隔符（冒号或空格）。

    有分隔符就说明模块已经在"名字: 正文"的位置上给了正文，哪怕它是空的；没有分隔符
    才是 ``+CONNECTED`` 这种名字即值的形态。
    """
    body = str(line or "").strip()
    if body.startswith("+"):
        body = body[1:]
    return ":" in body or " " in body


def response_value(exchange: AtExchange, name: str = "") -> str:
    """从一次交互里挑出目标参数的响应文本。

    先按参数名精确匹配（``+AT+CHAN_LIST:9080,9160`` 归一化后就是 ``CHAN_LIST``）；
    匹配不到时退回第一个 ``+`` 行的正文，兼容 ``AT+CONN_STATE`` 回 ``+CONNECTED``
    这种"响应名和指令名对不上"的形态。
    """
    want = normalize_name(name) if name else ""
    fallback: str | None = None
    for line in exchange.response:
        if not line.startswith("+"):
            continue
        got, text = split_value_line(line)
        if not text and not _has_delimiter(line):
            # ``+CONNECTED`` 这种整行只有名字、连分隔符都没有的形态：去掉前缀就是值。
            # 但 ``+R_PSK:`` 不一样——冒号在、正文为空，那是"这个值真的是空的"（密码留白、
            # 还没加入上级网络）。按"正文为空"一律回退到行名，会把 ``R_PSK:`` 当成值存下来，
            # 配置页据此再下发时就把上级网络名改成了字面量 ``R_SSID:``。
            text = line[1:].strip()
        if want and got == want:
            return text
        if fallback is None:
            fallback = text
    return fallback or ""


#: 这些失败回显既不以 ``+`` 开头，也不在 ``err_tokens`` 里（``ERROR`` 已由 classify 处理），
#: 单列出来，避免"收到一行就当成成功"。
_FAILURE_WORDS = ("NOT EXIST", "NOT FOUND", "INVALID", "FAIL", "DENIED", "REFUSED", "BUSY")


def failure_line(line: str) -> str:
    """这一行是否是失败回显；是则返回可读原因，否则返回空串。"""
    upper = str(line).upper()
    for word in _FAILURE_WORDS:
        if word in upper:
            return str(line).strip()
    return ""


class AtClient:
    """一次一问一答的 AT 对话执行器（不做重试之外的任何业务判断）。"""

    def __init__(
        self,
        cfg: AtCfg,
        demux: ChannelDemux,
        *,
        channel: str = AtChannel.SERIAL.value,
        endpoint: str = Endpoint.LOCAL.value,
        bus: EventBus | None = None,
        log: logging.Logger | None = None,
        value_grace_ms: float = 250.0,
    ) -> None:
        self.cfg = cfg
        self.demux = demux
        self.channel = str(channel or cfg.channel)
        self.endpoint = str(endpoint)
        self.bus = bus or get_bus()
        self.log = log or logging.getLogger(f"hmi.protocol.at.{self.endpoint}")
        self.value_grace_ms = max(0.0, float(value_grace_ms))

        self._ok_tokens = tuple(cfg.ok_tokens or ("OK",))
        self._err_tokens = tuple(cfg.err_tokens or ("ERROR", "ERR", "FAIL"))
        self._ending = ending_bytes(cfg.line_ending)
        self._lock = threading.Lock()
        self._awake = False
        self._busy = 0
        #: 多模块定向：非空时所有对话（含唤醒）都发给该 MAC（仅 UDP-AT 生效）。
        self.target_mac: bytes = b""
        self._counters: dict[str, float] = {
            "sent": 0,
            "ok": 0,
            "error": 0,
            "timeout": 0,
            "rejected": 0,
            "retry": 0,
            "unsolicited": 0,
            "rtt_sum_ms": 0.0,
        }
        self.demux.configure(ok_tokens=self._ok_tokens, err_tokens=self._err_tokens)

    # ------------------------------------------------------------------ 装配
    def configure(self, cfg: AtCfg | None = None, **overrides: Any) -> None:
        """运行时改超时/行尾/回显抑制等（UI 的 AT 面板与自检用）。"""
        if cfg is not None:
            self.cfg = cfg
        for key, value in overrides.items():
            if hasattr(self.cfg, key):
                setattr(self.cfg, key, value)
        self._ok_tokens = tuple(self.cfg.ok_tokens or ("OK",))
        self._err_tokens = tuple(self.cfg.err_tokens or ("ERROR", "ERR", "FAIL"))
        self._ending = ending_bytes(self.cfg.line_ending)
        self._awake = False
        self.demux.configure(ok_tokens=self._ok_tokens, err_tokens=self._err_tokens)

    def mark_channel(self, channel: str, endpoint: str | None = None) -> None:
        """切换通道后更新标记（串口/UDP、本端/远端），只影响事件归属字段。"""
        self.channel = str(channel or self.channel)
        if endpoint:
            self.endpoint = str(endpoint)
        self._awake = False

    def set_target_mac(self, mac: bytes | str) -> bytes:
        """设定多模块场景的目标模块（6 字节或 ``96-a1-3e-...`` 文本）。

        置空回退传输层自动兜底（配置固定目标 → 在线模块 → 握手）。切换目标后
        重新发唤醒序列，保证新模块也处于唤醒模式。返回清洗后的字节。
        """
        raw = b""
        if isinstance(mac, (bytes, bytearray)):
            raw = bytes(mac) if len(bytes(mac)) == 6 else b""
        else:
            text = str(mac or "").strip()
            if text:
                from ..transport.udp_at import parse_mac

                try:
                    raw = parse_mac(text)
                except ValueError:
                    self.log.warning("目标 MAC 非法，已按清空处理：%r", mac)
                    raw = b""
        self.target_mac = raw
        self._awake = False
        return raw

    def attach_unsolicited(self) -> None:
        """订阅空闲期模块主动上报（``+CONNECTED`` / ``+DISCONNECT`` / 配对结果）。"""
        self.demux.subscribe_at(self._on_unsolicited)

    def busy(self) -> bool:
        """是否正在对话中（透传服务据此显示"通道占用"）。"""
        return self._busy > 0

    # ------------------------------------------------------------------ 事件
    def _publish(self, exchange: AtExchange) -> None:
        self._counters["rtt_sum_ms"] += exchange.rtt_ms
        if self.bus is not None:
            try:
                self.bus.publish(Events.AT_EXCHANGE, exchange)
            except Exception:
                self.log.exception("AT 事件投递失败: %s", exchange.line[:40])

    def _fail_unsent(self, payload: str, *, name: str, kind: str, reason: str) -> NoReturn:
        """指令没能上线就失败：先记一条 ok=False 的交互，再抛 AtError。

        只抛错不留痕，AT 日志页就会一片空白——"点了没反应、日志也没记录"是现场
        最难查的一种失败，所以通道未绑定、未打开、写入异常都必须在这里留下证据。
        """
        self._counters["error"] += 1
        self._publish(
            AtExchange(
                line=payload,
                kind=kind,
                name=name or normalize_name(payload),
                ts=time.time(),
                ok=False,
                channel=self.channel,
                endpoint=self.endpoint,
                error=reason,
            )
        )
        self.log.warning("AT 未上线 %s -> %s", payload, reason)
        raise AtError(reason)

    def _on_unsolicited(self, line: str) -> None:
        parsed = classify(line, self._ok_tokens, self._err_tokens)
        if parsed.kind != KIND_VALUE:
            return
        self._counters["unsolicited"] += 1
        self._publish(
            AtExchange(
                kind="unsolicited",
                name=parsed.name,
                response=[line],
                channel=self.channel,
                endpoint=self.endpoint,
            )
        )

    # ------------------------------------------------------------------ 核心
    def request(
        self,
        text: str,
        *,
        name: str = "",
        kind: str = "exec",
        timeout_ms: int | None = None,
        bare: bool = False,
        retries: int | None = None,
    ) -> AtExchange:
        """发送一条 AT 行并等待响应，超时按 ``cfg.retries`` 自动重试。"""
        payload = (text or "").strip()
        if not payload:
            raise AtError("AT 指令为空")
        timeout = int(timeout_ms or self.cfg.timeout_ms)
        attempts = 1 + (max(0, int(self.cfg.retries)) if retries is None else max(0, int(retries)))
        last: AtExchange | None = None
        for attempt in range(attempts):
            last = self._once(payload, name=name, kind=kind, timeout_ms=timeout, bare=bare)
            if last.ok or last.error != "超时未响应":
                break
            if attempt + 1 < attempts:
                self._counters["retry"] += 1
                self.log.info("%s 无响应，重试 %d/%d", payload, attempt + 2, attempts)
                time.sleep(0.05)
        assert last is not None
        return last

    def _once(
        self,
        payload: str,
        *,
        name: str,
        kind: str,
        timeout_ms: int,
        bare: bool,
    ) -> AtExchange:
        transport = self.demux.transport
        if transport is None:
            self._fail_unsent(payload, name=name, kind=kind, reason="AT 通道未绑定传输层")
        if hasattr(transport, "is_open") and not transport.is_open():
            reason = f"AT 通道未打开（{self.channel}）"
            detail = str(getattr(transport, "state_detail", "") or "").strip()
            if detail:
                reason = f"{reason}：{detail}"
            self._fail_unsent(payload, name=name, kind=kind, reason=reason)

        token = f"at{next(_seq)}"
        lines: list[str] = []
        status = ""
        error = ""
        started = time.perf_counter()
        deadline = started + max(0.05, timeout_ms / 1000.0)
        last_response_at = 0.0

        self._busy += 1
        with self._lock:
            self.demux.begin(token)
            try:
                self._wake(token, transport)
                gap = max(0, int(self.cfg.pre_silence_ms))
                if gap:
                    time.sleep(gap / 1000.0)
                raw = payload.encode("utf-8", "replace") + self._ending
                try:
                    self._write_raw(transport, raw)
                except Exception as exc:
                    self._fail_unsent(
                        payload, name=name, kind=kind, reason=f"发送失败：{exc}"
                    )
                self._counters["sent"] += 1

                splitter = LineSplitter()
                while True:
                    remain_ms = (deadline - time.perf_counter()) * 1000.0
                    if remain_ms <= 0:
                        break
                    chunk = self.demux.collect(token, int(min(remain_ms, 50.0)))
                    if chunk:
                        for line in splitter.feed(chunk):
                            verdict = classify(
                                line,
                                self._ok_tokens,
                                self._err_tokens,
                                payload if self.cfg.echo_suppress else None,
                            )
                            if verdict.kind == KIND_ECHO:
                                continue
                            lines.append(line)
                            if verdict.kind == KIND_STATUS:
                                status = "ok" if verdict.is_ok else "error"
                                if status == "error":
                                    error = line
                            elif failure_line(line):
                                status = "error"
                                error = failure_line(line)
                            elif verdict.kind == KIND_VALUE and bare:
                                status = "ok"
                            last_response_at = time.perf_counter()
                    if status:
                        break
                    if last_response_at and not status:
                        # 没有 OK 的响应形态：以"最后一行之后静默够久"作为对话结束，
                        # 而不是收到首行就走，否则多行响应（STA_INFO/BSSLIST）会被截断。
                        idle = (time.perf_counter() - last_response_at) * 1000.0
                        if idle >= self.value_grace_ms:
                            status = "ok"
                            break
                if not status:
                    error = "超时未响应"
            finally:
                self.demux.release(token)
                self._busy -= 1

        ok = status == "ok"
        if ok:
            self._counters["ok"] += 1
        elif error == "超时未响应":
            self._counters["timeout"] += 1
        else:
            self._counters["error"] += 1
        exchange = AtExchange(
            line=payload,
            kind=kind,
            name=name or normalize_name(payload),
            ts=time.time(),
            rtt_ms=round((time.perf_counter() - started) * 1000.0, 1),
            ok=ok,
            response=lines,
            channel=self.channel,
            endpoint=self.endpoint,
            error="" if ok else (error or "未知错误"),
        )
        self._publish(exchange)
        if not ok:
            self.log.warning("AT 失败 %s -> %s", payload, exchange.error)
        return exchange

    def _write_raw(self, transport: Any, raw: bytes) -> int:
        """写字节：设了目标 MAC 且通道支持应用层定向时透传 target_mac。"""
        if self.target_mac and getattr(transport, "supports_target", False):
            return int(transport.write(raw, target_mac=self.target_mac))
        return int(transport.write(raw))

    def _wake(self, token: str, transport: Any) -> None:
        """手册要求"首先保证电台处于唤醒模式"，发一次唤醒序列并丢弃其响应。"""
        seq = str(self.cfg.wake_sequence or "").strip()
        if not seq or self._awake:
            return
        try:
            self._write_raw(transport, seq.encode("utf-8", "replace") + self._ending)
            time.sleep(max(0.0, self.cfg.wake_gap_ms / 1000.0))
            self.demux.collect(token, 80)
        except Exception as exc:
            self.log.debug("唤醒序列发送失败：%s", exc)
            return
        self._awake = True

    # ------------------------------------------------------------------ 参数级 API
    def query(self, name: str, *, timeout_ms: int | None = None) -> AtExchange:
        """查询一个注册表参数。

        ``Q_BARE`` 的意思是"响应没有 OK"，终止规则改成"值行即成功"。手册对这类
        参数（``AT+CONN_STATE`` / ``AT+WNBCFG``）写的是不带问号的形态，所以先按
        手册发裸命令；个别固件只认 ``AT+NAME?``，被直接回 ERROR 时补发一次带问号
        的写法，避免现场因为一行命令的方言差异卡住。
        """
        key = registry.resolve_key(name) or str(name).strip().upper()
        spec = registry.spec_for(key)
        if spec is None:
            return self._reject(str(name), f"未知参数 {name}", kind="query")
        if not spec.readable:
            return self._reject(key, f"{spec.title} 不可查询", kind="query")
        if spec.query != registry.Q_BARE:
            return self.request(
                format_at(spec.wire_name, query=True),
                name=key,
                kind="query",
                timeout_ms=timeout_ms,
            )
        first = self.request(
            format_at(spec.wire_name),
            name=key,
            kind="query",
            timeout_ms=timeout_ms,
            bare=True,
        )
        if not first.error.upper().startswith("ERROR"):
            return first
        self.log.info("%s 不接受裸查询，改用 %s", spec.wire_name, format_at(spec.wire_name, True))
        return self.request(
            format_at(spec.wire_name, query=True),
            name=key,
            kind="query",
            timeout_ms=timeout_ms,
            bare=True,
        )

    def query_value(self, name: str, *, timeout_ms: int | None = None) -> tuple[bool, Any, str]:
        """查询并解析成 Python 值，返回 (是否成功, 值, 原始响应文本)。"""
        ex = self.query(name, timeout_ms=timeout_ms)
        if not ex.ok:
            return False, None, ex.error
        raw = response_value(ex, ex.name)
        return True, registry.decode(ex.name, raw), raw

    def set(self, name: str, value: Any, *, timeout_ms: int | None = None) -> AtExchange:
        """校验并下发一个参数；校验不通过时不占用通道，直接返回失败记录。"""
        key = registry.resolve_key(name) or str(name).strip().upper()
        spec = registry.spec_for(key)
        if spec is None:
            return self._reject(str(name), f"未知参数 {name}", kind="set")
        if not spec.writable:
            return self._reject(key, f"{spec.title} 是只读参数", kind="set")
        ok, text, err = registry.encode(key, value)
        if not ok:
            return self._reject(key, err, kind="set")
        return self.request(
            format_at(spec.wire_name, text), name=key, kind="set", timeout_ms=timeout_ms
        )

    def action(self, name: str, value: Any = "", *, timeout_ms: int | None = None) -> AtExchange:
        """执行一条动作指令（PAIR / UNPAIR / LOADDEF / SCAN_AP …）。"""
        key = registry.resolve_key(name) or str(name).strip().upper()
        spec = registry.spec_for(key)
        if spec is None:
            return self._reject(str(name), f"未知指令 {name}", kind="action")
        ok, text, err = registry.encode(key, value)
        if not ok:
            return self._reject(key, err, kind="action")
        return self.request(
            format_at(spec.wire_name, text),
            name=key,
            kind="action",
            timeout_ms=timeout_ms,
        )

    def apply(
        self,
        params: Any,
        *,
        stop_on_error: bool = False,
        timeout_ms: int | None = None,
    ) -> list[AtExchange]:
        """把 RadioParams 里被触碰过的字段按注册表 order 顺序逐条下发。"""
        try:
            steps = registry.plan(params)
        except registry.ParamError as exc:
            return [self._reject("PLAN", str(exc), kind="apply")]
        out: list[AtExchange] = []
        for key, text in steps:
            spec = registry.spec_for(key)
            wire = spec.wire_name if spec else key
            ex = self.request(format_at(wire, text), name=key, kind="set", timeout_ms=timeout_ms)
            out.append(ex)
            if stop_on_error and not ex.ok:
                break
        return out

    def read_many(
        self, names: list[str], *, timeout_ms: int | None = None
    ) -> dict[str, tuple[bool, Any, str]]:
        """批量查询，给信道遥测轮询用；单个失败不影响其余。"""
        out: dict[str, tuple[bool, Any, str]] = {}
        for name in names:
            try:
                out[name] = self.query_value(name, timeout_ms=timeout_ms)
            except AtError as exc:
                self.log.debug("读取 %s 失败：%s", name, exc)
                out[name] = (False, None, str(exc))
        return out

    def _reject(self, name: str, error: str, *, kind: str) -> AtExchange:
        """校验类失败：记录事件但完全不碰通道。"""
        self._counters["rejected"] += 1
        ex = AtExchange(
            line=f"AT+{name}",
            kind=kind,
            name=str(name),
            ok=False,
            error=error,
            channel=self.channel,
            endpoint=self.endpoint,
        )
        self._publish(ex)
        self.log.warning("拒绝下发 %s：%s", name, error)
        return ex

    # ------------------------------------------------------------------ 状态
    def stats(self) -> dict[str, Any]:
        sent = int(self._counters["sent"])
        ok = int(self._counters["ok"])
        return {
            "channel": self.channel,
            "endpoint": self.endpoint,
            "sent": sent,
            "ok": ok,
            "error": int(self._counters["error"]),
            "timeout": int(self._counters["timeout"]),
            "rejected": int(self._counters["rejected"]),
            "retry": int(self._counters["retry"]),
            "unsolicited": int(self._counters["unsolicited"]),
            "busy": self._busy,
            "success_rate": round(ok / sent, 3) if sent else 0.0,
            "avg_rtt_ms": round(self._counters["rtt_sum_ms"] / max(1, ok), 1),
            "awake": self._awake,
        }

    def __repr__(self) -> str:
        return f"<AtClient {self.channel}/{self.endpoint} busy={self._busy}>"
