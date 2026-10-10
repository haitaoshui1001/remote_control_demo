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
from ..protocol.at_codec import format_at, parse_float
from ..transport.udp_at import format_mac
from .link import LinkService

__all__ = ["RadioService"]

#: 这些都是"帧根本没上线"的失败（通道没打开 / 目标模块离线被取消发送），
#: 批量读取时第一条命中就早停，剩下的标记跳过，不再排队等一串超时。
_CHANNEL_DOWN_MARKS = ("未绑定传输层", "通道未打开", "目标模块离线")

#: 写入后自动回读校验只对"两侧都走同一套编解码"的结构化标量做。
#: list/range（频点列表顺序不稳定）、pair_mac_int、文本类（SSID/KEY 模块常打码）
#: 都不可靠，一律不校验——校验的目的是防"模块回 OK 但值没生效"，不是制造误报。
_VERIFY_KINDS = ("int", "enum", "float", "state")


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

    # ------------------------------------------------------------------ 目标模块
    def target(self) -> dict[str, Any]:
        """当前 AT 定向目标 + 在线模块表：界面用它显示"这条指令发给了谁"。

        ``mac`` 为空表示还没定向（传输层自动兜底）。多模块场景下这一项必须显式
        显示出来：实机故障"写 8 读回 6"的根因就是写和读命中了不同的模块，而界面
        从头到尾没告诉用户当前是哪台。
        """
        raw = getattr(self.at, "target_mac", b"") or b""
        if isinstance(raw, (bytes, bytearray)) and len(raw) == 6:
            mac = format_mac(bytes(raw))
        else:
            mac = str(raw or "")
        getter = getattr(self.link, "at_modules", None)
        modules = list(getter()) if callable(getter) else []
        return {
            "mac": mac,
            "auto": not mac,
            "total": len(modules),
            "online": sum(1 for m in modules if m.get("online")),
        }

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
                    "confirm": spec.confirm,
                }
            )
        return rows

    def _absorb(self, key: str, value: Any) -> None:
        """把 AT 上刚拿到的新鲜值回灌进遥测累积（``LinkMonitor._merged``）。

        配置页的"当前值"列由 1Hz 快照驱动，而快照里静态参数只有首轮读过：不回灌
        的话，手动读取/写入确认的新值最多存活一秒就被旧快照刷回去——实机报障
        "下发 8 且回读 8、表格里还是 1"就是这么来的。清单外的键 absorb 会自己忽略。
        """
        monitor = getattr(self.link, "monitor", None)
        absorb = getattr(monitor, "absorb", None)
        if not callable(absorb) or value is None:
            return
        try:
            absorb(key, value)
        except Exception as exc:  # noqa: BLE001 回灌失败只影响显示，不影响读写结论
            self.log.debug("回灌遥测累积 %s 失败：%s", key, exc)

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
        self._absorb(key, value)
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
    def _norm(self, key: str, value: Any) -> str | None:
        """把"下发的值"和"回读的值"归一到可比较的文本；无法解释返回 None。"""
        ok, text, _typed = registry.validate(key, value)
        if ok:
            return str(text).strip().lower()
        return None

    def _out_of_range(self, spec: Any, value: Any) -> float | None:
        """回读值"明确超出注册表范围"时返回该数值，其余情况返回 None。

        注册表按手册收紧（TXPOWER 6~20 dBm），但实机模块会答出规格外的数
        （``+TXPOWER:1dbm``）。这类值 ``registry.validate()`` 不认，若因此判成
        "说不清"，写入就照时报成功——正是用户看到的"下发 8 显示成功、表格还是 1"。
        """
        if spec.kind not in ("int", "float") or (spec.vmin is None and spec.vmax is None):
            return None
        num = parse_float(value)
        if num is None:
            return None
        if spec.vmin is not None and num < float(spec.vmin):
            return num
        if spec.vmax is not None and num > float(spec.vmax):
            return num
        return None

    def _verify(self, key: str, spec: Any, sent: Any) -> dict[str, Any]:
        """SET 成功后定向回读一次，判断"模块是不是真把值收下了"。

        ``verified``：True 已确认 / False 值不符（写入未生效）/ None 说不清
        （回读超时或响应无法解析——例如改频点后模块正在重启）。
        """
        want = self._norm(key, sent)
        try:
            ok, got, raw = self.at.query_value(key)
        except AtError as exc:
            return {"verified": None, "readback": None,
                    "detail": f"已应答但回读失败：{exc}"}
        if not ok:
            return {"verified": None, "readback": None,
                    "detail": f"已应答但回读未成功：{raw}"}
        have = self._norm(key, got)
        if have is None:
            num = self._out_of_range(spec, got)
            if num is not None:
                # 规格外的读数照样能比数值：不能因为"解释不了"就放过不符。
                extra = f"，模块返回值超出规格范围（{spec.bound_text()}）"
                sent_num = parse_float(sent)
                if sent_num is not None and abs(num - sent_num) < 1e-9:
                    return {"verified": True, "readback": got, "raw": raw,
                            "detail": f"{spec.title} 已下发并回读确认为 {got}{extra}"}
                return {"verified": False, "readback": got, "raw": raw,
                        "detail": f"写入未生效：下发 {sent}，回读 {got}{extra}"}
        if want is None or have is None:
            return {"verified": None, "readback": got,
                    "detail": f"已应答，回读值 {got} 无法与下发值对齐校验"}
        if want != have:
            return {"verified": False, "readback": got, "raw": raw,
                    "detail": f"写入未生效：下发 {want}，回读 {have}（{raw}）"}
        return {"verified": True, "readback": got, "raw": raw,
                "detail": f"{spec.title} 已下发并回读确认为 {have}"}

    def write(self, name: str, value: Any, *, verify: bool | None = None) -> dict[str, Any]:
        """写一个参数（本地校验不过则不占用通道）。

        ``verify``：SET 被模块接受后是否立刻定向回读比对。默认对结构化标量开启
        ——实机反馈"设置功率 8 显示成功、重新读取还是 6"，光看 SET 的 OK 根本发现
        不了，必须把"写"和"写进去没有"分成两句话讲。
        """
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
        res: dict[str, Any] = {
            "ok": True,
            "name": key,
            "detail": f"{spec.title} 已下发",
            "rtt_ms": ex.rtt_ms,
            "response": list(ex.response),
            "target": self.target()["mac"],
        }
        do_verify = (spec.readable and spec.kind in _VERIFY_KINDS) if verify is None else verify
        if do_verify:
            v = self._verify(key, spec, value)
            res.update(v)
            state = v.get("verified")
            if state is False:
                # 回读到的才是真相：把缓存改回落读值，界面当前值列不会显示一个
                # 模块里并不存在的数字。但规格外的值不能回填——apply_to_params 不做
                # 校验，一旦把 1 写进 txpower，之后批量下发会在 plan() 直接抛错。
                if spec.fields and self._norm(key, v.get("readback")) is not None:
                    registry.apply_to_params(self.params, key, v.get("readback"))
                self.failures += 1
                res["ok"] = False
                self._alarm("AT_WRITE_UNVERIFIED", f"{spec.title}：{v['detail']}")
            res["detail"] = v["detail"]
        # 写入后同步遥测累积：回读到过就用回读值（那是模块里真实的答案），
        # 没回读成用下发值。不回灌的话一秒后快照会把"当前值"刷回首轮旧读数。
        truth = res.get("readback") if do_verify and res.get("readback") is not None else value
        self._absorb(key, truth)
        return res

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
    def action(self, name: str, value: Any = "", *, confirmed: bool = False) -> dict[str, Any]:
        """执行 PAIR / UNPAIR / LOADDEF / SCAN_AP / JOIN 一类动作指令。

        注册表里标了 ``confirm`` 的危险动作，必须由调用方显式传 ``confirmed=True``
        （即界面已经弹过二次确认）。这道闸门放在服务层而不是页面里：批量下发、
        脚本、快捷键都绕不过去，避免"恢复出厂"被一次误点发上线。
        """
        key = registry.resolve_key(name) or str(name).strip().upper()
        spec = registry.spec_for(key)
        if spec is not None and spec.needs_confirm and not confirmed:
            return {
                "ok": False,
                "name": key,
                "detail": f"需二次确认：{spec.confirm}",
                "confirm": spec.confirm,
                "need_confirm": True,
                "response": [],
            }
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
