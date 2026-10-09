"""电台配置服务：参数读写编排 + 动作指令 + 下发前预览。

这一层刻意**不发** ``radio/params`` 事件。``LinkMonitor.poll_once()`` 每轮已经
发布过分组的参数视图，若这里再发一次，同一主题就会有两种 payload 形状，UI 必须
猜来源。手动读取的结果直接返回给调用方（UI 拿到就填表），周期数据一律以快照为准。

值语义沿用 ``RadioParams``：``None`` 是"没读过 / 不下发"，不是"下发空值"。所以
``apply()`` 只发 ``touched()`` 出来的字段，用户在配置页没碰过的框绝不会被偷偷写回模块。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..core.config import AppConfig
from ..core.events import EventBus, Events, get_bus
from ..core.logger import get_logger
from ..core.models import AlarmEvent, AlarmLevel, LinkSnapshot, RadioParams
from ..protocol import param_registry as registry
from ..protocol.at_client import AtError
from ..protocol.at_codec import format_at
from .link import LinkService

__all__ = ["RadioService"]

_CHANNEL_DOWN_MARKS = ("未绑定传输层", "通道未打开")


def _channel_down(detail: str) -> bool:
    """读取失败的 detail 是否说明"通道根本没法发"（而不是模块回了 ERROR）。"""
    return any(mark in detail for mark in _CHANNEL_DOWN_MARKS)


class RadioService:
    """一个电台（本端或远端）的配置面板后端。"""

    def __init__(
        self,
        link: LinkService,
        cfg: AppConfig,
        *,
        bus: EventBus | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self.link = link
        self.cfg = cfg
        self.bus = bus if bus is not None else get_bus()
        self.log = log or get_logger(f"hmi.services.radio.{link.endpoint}")
        self.at = link.at
        self.params = RadioParams()
        #: 多模块共用一条 AT 线：按目标 MAC 各存一份参数缓存，切换时各自保存/恢复。
        self._caches: dict[str, RadioParams] = {}
        self.device_key: str = ""
        self.last_read_ts = 0.0
        self.reads = 0
        self.writes = 0
        self.failures = 0

    # ------------------------------------------------------------------ 设备选择
    def select_device(self, mac: str) -> dict[str, Any]:
        """切换 AT 目标模块并换用该 MAC 自己的参数缓存（无缓存则从空开始）。

        ok=False 时（非 UDP 通道等）不动缓存结构，当前面板继续对原目标工作。
        """
        picker = getattr(self.link, "select_device", None)
        if not callable(picker):
            return {"ok": False, "mac": "", "detail": "当前链路服务不支持选择模块"}
        res = dict(picker(str(mac or "")))
        if not res.get("ok"):
            return res
        key = str(res.get("mac") or "").strip().lower()
        self._caches[self.device_key] = self.params
        self.device_key = key
        cached = self._caches.get(key)
        if cached is None:
            cached = RadioParams()
            self._caches[key] = cached
            self.last_read_ts = 0.0
        self.params = cached
        return res

    # ------------------------------------------------------------------ 目录
    @property
    def groups(self) -> tuple[str, ...]:
        return registry.GROUPS

    def catalog(self, *, writable_only: bool = False) -> list[dict[str, Any]]:
        """给配置页铺表格：注册表元信息 + 当前缓存值 + 是否被改动。"""
        rows: list[dict[str, Any]] = []
        for spec in registry.all_specs():
            if writable_only and not spec.writable:
                continue
            value = self._cached(spec)
            rows.append(
                {
                    "name": spec.name,
                    "at": spec.wire_name,
                    "title": spec.title,
                    "group": spec.group,
                    "rw": spec.rw,
                    "kind": spec.kind,
                    "bound": spec.bound_text(),
                    "persist": spec.persist,
                    "desc": spec.desc,
                    "value": value,
                    "options": list(spec.options or []),
                    "unit": spec.unit,
                }
            )
        return rows

    def _cached(self, spec: Any) -> Any:
        for field in spec.fields or ():
            hit = getattr(self.params, field, None)
            if hit is not None:
                return hit
        return self.params.raw.get(spec.name)

    def spec_info(self, name: str) -> dict[str, Any] | None:
        spec = registry.spec_for(name)
        if spec is None:
            return None
        return {
            "name": spec.name,
            "at": spec.wire_name,
            "title": spec.title,
            "readable": spec.readable,
            "writable": spec.writable,
            "kind": spec.kind,
            "options": list(spec.options or []),
            "bound": spec.bound_text(),
            "unit": spec.unit,
        }

    # ------------------------------------------------------------------ 读取
    def read(self, name: str) -> dict[str, Any]:
        """读一个参数并写进缓存；失败返回 ``{ok: False, detail}`` 而不抛异常。"""
        key = registry.resolve_key(name) or str(name).strip().upper()
        try:
            ok, value, raw = self.at.query_value(key)
        except AtError as exc:
            self.failures += 1
            return {"ok": False, "name": key, "detail": str(exc)}
        if not ok:
            self.failures += 1
            return {"ok": False, "name": key, "detail": raw or "读取失败"}
        self.reads += 1
        self.last_read_ts = time.time()
        spec = registry.spec_for(key)
        if spec is not None:
            registry.apply_to_params(self.params, key, value)
        self.params.raw[key] = str(raw)
        return {
            "ok": True,
            "name": key,
            "title": spec.title if spec else key,
            "value": value,
            "raw": raw,
        }

    def read_many(self, names: list[str] | None = None) -> dict[str, Any]:
        """批量读取；默认读"配置类"可写参数（不含遥测/动作），给配置页的刷新按钮用。

        通道本身不可用（未绑定/未打开）时第一条失败就收手，其余直接标记跳过：
        再串行问下去也全是同一个错，白等一堆超时。
        """
        keys = names or [s.name for s in registry.all_specs() if s.writable and s.readable]
        out: dict[str, Any] = {}
        down = ""
        for key in keys:
            if down:
                out[key] = {"ok": False, "name": key, "detail": down}
                continue
            one = self.read(key)
            out[key] = one
            if not one["ok"] and _channel_down(str(one.get("detail") or "")):
                down = f"通道不可用，已跳过其余读取：{one['detail']}"
        ok = sum(1 for v in out.values() if v["ok"])
        return {
            "ok": ok > 0,
            "values": out,
            "detail": f"成功 {ok}/{len(keys)}",
            "count": ok,
            "total": len(keys),
        }

    def read_line(self, name: str) -> dict[str, Any]:
        """读原始响应行（WNBCFG / SCAN_AP 这类多行结果）。"""
        key = registry.resolve_key(name) or str(name).strip().upper()
        ex = self.at.query(key)
        return {
            "ok": ex.ok,
            "name": key,
            "line": ex.line,
            "response": list(ex.response),
            "text": ex.text(),
            "detail": ex.error,
        }

    # ------------------------------------------------------------------ 写入
    def write(self, name: str, value: Any) -> dict[str, Any]:
        """写一个参数（本地校验不过则不占用通道）。"""
        key = registry.resolve_key(name) or str(name).strip().upper()
        spec = registry.spec_for(key)
        if spec is None:
            self.failures += 1
            return {"ok": False, "name": key, "detail": f"未知参数 {name}"}
        try:
            ex = self.at.set(key, value)
        except AtError as exc:
            self.failures += 1
            self._alarm("AT_WRITE_FAILED", f"{spec.title} 写入失败：{exc}")
            return {"ok": False, "name": key, "detail": str(exc)}
        if not ex.ok:
            self.failures += 1
            return {"ok": False, "name": key, "detail": ex.error or "模块未接受"}
        self.writes += 1
        if spec.fields:
            registry.apply_to_params(self.params, key, value)
        return {
            "ok": True,
            "name": key,
            "detail": f"{spec.title} 已下发",
            "rtt_ms": ex.rtt_ms,
            "response": list(ex.response),
        }

    def plan(self, params: RadioParams | None = None) -> dict[str, Any]:
        """下发前预览：把改动翻译成将要上线的 AT 行。校验不过直接把错误抛给调用方。"""
        p = params if params is not None else self.params
        try:
            steps = registry.plan(p)
        except registry.ParamError as exc:
            self.failures += 1
            return {"ok": False, "detail": str(exc), "lines": []}
        lines = []
        for key, text in steps:
            spec = registry.spec_for(key)
            lines.append(format_at(spec.wire_name if spec else key, text))
        return {
            "ok": True,
            "detail": f"{len(steps)} 项待下发",
            "lines": lines,
            "steps": steps,
        }

    def apply(self, params: RadioParams | None = None, *, stop_on_error: bool = False) -> dict:
        """按注册表顺序逐条下发被改动过的字段。"""
        p = params if params is not None else self.params
        if not p.touched():
            return {"ok": True, "detail": "没有任何改动", "results": [], "count": 0}
        try:
            exchanges = self.at.apply(p, stop_on_error=stop_on_error)
        except AtError as exc:
            self.failures += 1
            self._alarm("AT_APPLY_FAILED", str(exc))
            return {"ok": False, "detail": str(exc), "results": [], "count": 0}
        results = [
            {
                "name": ex.name,
                "line": ex.line,
                "ok": ex.ok,
                "rtt_ms": ex.rtt_ms,
                "detail": ex.error,
            }
            for ex in exchanges
        ]
        bad = [r for r in results if not r["ok"]]
        self.writes += len(results) - len(bad)
        self.failures += len(bad)
        if bad:
            self._alarm(
                "PARAM_PARTIAL",
                f"{len(bad)}/{len(results)} 项下发失败：" + "；".join(r["detail"] for r in bad[:3]),
            )
        return {
            "ok": not bad,
            "detail": f"{len(results) - len(bad)}/{len(results)} 项成功",
            "results": results,
            "count": len(results),
            "failed": [r["name"] for r in bad],
        }

    def load_defaults(self) -> dict[str, Any]:
        """丢弃当前设备缓存，下次刷新完全以模块为准。"""
        self.params = RadioParams()
        self._caches[self.device_key] = self.params
        self.last_read_ts = 0.0
        return {"ok": True, "detail": "本地参数缓存已清空"}

    # ------------------------------------------------------------------ 动作
    def action(self, name: str, value: Any = "") -> dict[str, Any]:
        """执行 PAIR / UNPAIR / LOADDEF / SCAN_AP / JOIN 一类动作指令。"""
        key = registry.resolve_key(name) or str(name).strip().upper()
        spec = registry.spec_for(key)
        try:
            ex = self.at.action(key, value)
        except AtError as exc:
            self.failures += 1
            self._alarm("AT_ACTION_FAILED", f"{key} 执行失败：{exc}", AlarmLevel.CRIT.value)
            return {"ok": False, "name": key, "detail": str(exc), "response": []}
        if not ex.ok:
            self.failures += 1
            return {
                "ok": False,
                "name": key,
                "detail": ex.error or "模块拒绝",
                "response": list(ex.response),
            }
        self.writes += 1
        # SYSDBG 一类"动作即开关"的指令仍会回填配置缓存，避免界面显示与实际不一致。
        if spec is not None and spec.fields and value != "":
            registry.apply_to_params(self.params, key, value)
        return {
            "ok": True,
            "name": key,
            "detail": ex.response[-1] if ex.response else "已执行",
            "response": list(ex.response),
            "text": ex.text(),
            "rtt_ms": ex.rtt_ms,
        }

    # ------------------------------------------------------------------ 遥测
    def poll_once(self) -> LinkSnapshot | None:
        return self.link.poll_once()

    def snapshot(self) -> LinkSnapshot | None:
        return self.link.monitor.snapshot()

    def history(self) -> list[LinkSnapshot]:
        return self.link.monitor.history()

    def link_summary(self) -> dict[str, Any]:
        snap = self.snapshot()
        if snap is None:
            return {"available": False, "detail": "还没有成功轮询"}
        return {
            "available": True,
            "endpoint": snap.endpoint,
            "role": snap.role,
            "connected": snap.connected,
            "rssi_dbm": snap.rssi_dbm,
            "evm_db": snap.evm_db,
            "tx_mcs": snap.tx_mcs,
            "bandwidth_mhz": snap.bandwidth_mhz,
            "freq_mhz": snap.freq_mhz,
            "sta_count": snap.sta_count,
            "degraded": snap.degraded,
            "errors": list(snap.errors),
            "ts": snap.ts,
            "stale": self.link.monitor.is_stale(),
        }

    def summary(self) -> dict[str, Any]:
        snap = self.snapshot()
        return {
            "endpoint": self.link.endpoint,
            "reads": self.reads,
            "writes": self.writes,
            "failures": self.failures,
            "touched": len(self.params.touched()),
            "last_read_ts": self.last_read_ts,
            "snapshot": bool(snap),
            "at": self.at.stats(),
        }

    # ------------------------------------------------------------------ 事件
    def _alarm(
        self, code: str, message: str, level: str = AlarmLevel.WARN.value
    ) -> None:
        if self.bus is None:
            return
        self.bus.publish(
            Events.ALARM,
            AlarmEvent(
                ts=time.time(),
                level=level,
                source=f"radio/{self.link.endpoint}",
                code=code,
                message=message,
            ),
        )

    def __repr__(self) -> str:
        return f"<RadioService {self.link.endpoint} reads={self.reads} writes={self.writes}>"
