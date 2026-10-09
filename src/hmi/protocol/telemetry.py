"""信道遥测：把一串 AT 查询组装成 ``LinkSnapshot``，并按阈值判健康度。

为什么单独一层：
- ``AtClient`` 只管"一问一答"，它不知道 RSSI 低于 -92 算告警，也不该知道；
- UI 信道页只认 ``LinkSnapshot`` / ``StaInfo``，翻页、画曲线都不碰串口；
- 阈值、轮询清单、掉线判定全部来自 ``LinkCfg``，改配置不改代码。

响应形态的脏活在这里收口：
- ``AT+CONN_STATE`` 回 ``+CONNECTED`` / ``+DISCONNECT``，既没有 OK 也没有冒号；
- ``AT+WNBCFG`` 回一行 ``mode=..,bw=..,chan=..``；
- ``AT+STA_INFO=<id>`` 手册响应是截图（文本层提不出来），字段顺序按"序号,MAC,RSSI,
  EVM,TX_MCS,RX_MCS,TX,RX,LOST"排，同时兼容 ``key=value`` 形态——现场固件与仿真不一致
  时，解析失败只是少一格数据，不会让整轮轮询崩掉。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from typing import Any

from ..core.config import LinkCfg
from ..core.events import EventBus, Events, get_bus
from ..core.logger import get_logger
from ..core.models import AlarmEvent, AlarmLevel, LinkSnapshot, RadioParams, StaInfo
from . import param_registry as registry
from .at_client import AtClient
from .at_codec import as_text, parse_float, parse_int, parse_kv_pairs

__all__ = [
    "LinkMonitor",
    "build_snapshot",
    "evaluate",
    "loss_percent",
    "parse_sta_rows",
    "params_view",
]

#: STA 明细里 MAC 的位置不固定（有的固件前面还带索引），先用正则认出来
_MAC_RE = re.compile(r"(?:[0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}")

#: ``+STA_INFO:`` 一行里 MAC 之后的字段顺序（手册 6.3.12）
_STA_ORDER = (
    "rssi_dbm",
    "evm_db",
    "tx_mcs",
    "rx_mcs",
    "tx_packets",
    "rx_packets",
    "lost_packets",
)


# --------------------------------------------------------------------- 解析
def _row_from_kv(chunk: str) -> StaInfo | None:
    """``id=1,mac=..,rssi=-78`` 这类 key=value 行。"""
    kv = parse_kv_pairs(chunk)
    if not kv:
        return None
    return StaInfo(
        index=parse_int(kv.get("id") or kv.get("index")) or 0,
        mac=str(kv.get("mac") or "").strip().lower(),
        rssi_dbm=parse_float(kv.get("rssi")),
        evm_db=parse_float(kv.get("evm")),
        tx_mcs=parse_int(kv.get("tx_mcs")),
        rx_mcs=parse_int(kv.get("rx_mcs")),
        tx_packets=parse_int(kv.get("tx")),
        rx_packets=parse_int(kv.get("rx")),
        lost_packets=parse_int(kv.get("lost")),
        raw=chunk,
    )


def _row_from_csv(chunk: str, index_hint: int) -> StaInfo | None:
    """``1,00:11:22:33:44:55,-78.0,-22.0,7,7,15234,18990,12`` 这类逗号行。"""
    parts = [p.strip() for p in chunk.split(",") if p.strip()]
    if not parts:
        return None
    mac_at = next((i for i, p in enumerate(parts) if _MAC_RE.fullmatch(p)), None)
    info = StaInfo(index=index_hint, raw=chunk)
    if mac_at is None:
        nums = [n for n in (parse_float(p) for p in parts) if n is not None]
        if not nums:
            return None
        info.rssi_dbm = nums[0]
        if len(nums) > 1:
            info.evm_db = nums[1]
        return info
    info.mac = parts[mac_at].lower().replace("-", ":")
    head = parts[:mac_at]
    if head:
        info.index = parse_int(head[0]) or index_hint
    for field_name, token in zip(_STA_ORDER, parts[mac_at + 1 :], strict=False):
        if field_name.endswith("_dbm") or field_name.endswith("_db"):
            setattr(info, field_name, parse_float(token))
        else:
            setattr(info, field_name, parse_int(token))
    return info


def parse_sta_rows(value: object) -> list[StaInfo]:
    """把 STA_INFO 响应正文解析成逐从机列表；无法识别的行直接跳过。"""
    text = as_text(value).strip().lstrip("+").strip()
    if not text:
        return []
    rows: list[StaInfo | None] = []
    for i, chunk in enumerate(re.split(r"[;\r\n]+", text), start=1):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk and not _MAC_RE.search(chunk):
            rows.append(_row_from_kv(chunk))
        else:
            rows.append(_row_from_csv(chunk, i))
    return [r for r in rows if r is not None]


def loss_percent(tx_packets: object, lost_packets: object) -> float | None:
    """丢包率（%）。

    LMAC 统计里 ``lost`` 是未确认帧数，所以分母取 ``tx + lost``；两个字段都缺时返回
    ``None`` —— UI 必须能区分"未知"和"0%"。
    """
    tx = parse_int(tx_packets)
    lost = parse_int(lost_packets)
    if tx is None and lost is None:
        return None
    total = (tx or 0) + (lost or 0)
    if total <= 0:
        return None
    return round(100.0 * (lost or 0) / total, 2)


def _hit(results: dict[str, tuple[bool, Any, str]], name: str) -> tuple[Any, str]:
    got = results.get(name)
    if not got or not got[0]:
        return None, ""
    return got[1], str(got[2] or "")


def _to_mhz(raw: object) -> float | None:
    """手册频点单位是 MHz×10（8680 → 868.0 MHz），比例取注册表不写死。"""
    num = parse_int(raw)
    if num is None:
        return None
    spec = registry.spec_for("FREQ_RANGE")
    scale = float(spec.scale) if spec and spec.scale else 10.0
    return round(num / scale, 1)


# --------------------------------------------------------------------- 组装
def build_snapshot(
    results: dict[str, tuple[bool, Any, str]],
    *,
    endpoint: str = "local",
    stats: dict[str, Any] | None = None,
) -> LinkSnapshot:
    """纯函数：查询结果 → 快照。不发事件、不读时钟，方便单测逐字段断言。"""
    params = RadioParams()
    errors: list[str] = []
    for name, (ok, value, err) in results.items():
        if not ok:
            errors.append(f"{name}: {err or '查询失败'}")
            continue
        registry.apply_to_params(params, name, value)
        params.raw[name] = str(value)

    snap = LinkSnapshot(endpoint=endpoint, errors=errors, stats=dict(stats or {}))
    snap.role = params.mode
    snap.bandwidth_mhz = params.bss_bw
    snap.freq_range = params.freq_range
    snap.chan_list = params.chan_list
    snap.ssid = params.ssid
    snap.keymgmt = params.keymgmt
    snap.tx_mcs = params.tx_mcs
    snap.acktmo_us = params.acktmo
    snap.heart_int_ms = params.heart_int
    snap.txpower_dbm = float(params.txpower) if params.txpower is not None else None

    conn, _ = _hit(results, "CONN_STATE")
    if isinstance(conn, bool):
        snap.connected = conn
    rssi, _ = _hit(results, "RSSI")
    if isinstance(rssi, (int, float)):
        snap.rssi_dbm = float(rssi)
    evm, _ = _hit(results, "EVM")
    if isinstance(evm, (int, float)):
        snap.evm_db = float(evm)

    kv = parse_kv_pairs(_hit(results, "WNBCFG")[1])
    if kv.get("mode") and not snap.role:
        snap.role = str(kv["mode"]).lower()
    if kv.get("bw"):
        snap.bandwidth_mhz = parse_int(kv["bw"]) or snap.bandwidth_mhz
    raw_chan = str(kv.get("chan") or "").replace(";", ",")
    chans = [part.strip() for part in raw_chan.split(",") if part.strip()]
    if chans:
        snap.freq_mhz = _to_mhz(chans[0])
    if snap.freq_mhz is None and params.chan_list:
        snap.freq_mhz = _to_mhz(params.chan_list[0])

    rows = parse_sta_rows(_hit(results, "STA_INFO")[1])
    snap.sta_list = rows
    snap.sta_count = len(rows)
    if rows:
        if snap.rssi_dbm is None:
            snap.rssi_dbm = rows[0].rssi_dbm
        if snap.evm_db is None:
            snap.evm_db = rows[0].evm_db
    return snap


def evaluate(snap: LinkSnapshot, cfg: LinkCfg | None = None) -> list[AlarmEvent]:
    """按 ``LinkCfg`` 阈值产告警，并就地把 ``snap.degraded`` 置位。"""
    cfg = cfg or LinkCfg()
    alarms: list[AlarmEvent] = []

    def emit(level: AlarmLevel, code: str, message: str) -> None:
        alarms.append(
            AlarmEvent(
                level=level.value,
                source=f"link/{snap.endpoint}",
                code=code,
                message=message,
            )
        )

    if snap.connected is False:
        emit(AlarmLevel.WARN, "LINK_DOWN", "电台未连接（AT+CONN_STATE 回 +DISCONNECT）")
    if snap.rssi_dbm is not None:
        if snap.rssi_dbm <= cfg.rssi_crit:
            emit(
                AlarmLevel.CRIT,
                "RSSI_CRIT",
                f"RSSI {snap.rssi_dbm:.1f} dBm ≤ {cfg.rssi_crit:.1f}，链路濒临中断",
            )
        elif snap.rssi_dbm <= cfg.rssi_warn:
            emit(
                AlarmLevel.WARN,
                "RSSI_LOW",
                f"RSSI {snap.rssi_dbm:.1f} dBm ≤ {cfg.rssi_warn:.1f}，建议降速或提功率",
            )
    if snap.evm_db is not None and snap.evm_db >= cfg.evm_warn:
        emit(
            AlarmLevel.WARN,
            "EVM_HIGH",
            f"EVM {snap.evm_db:.1f} dB ≥ {cfg.evm_warn:.1f}，调制质量差（越负越好）",
        )
    for row in snap.sta_list:
        pct = loss_percent(row.tx_packets, row.lost_packets)
        if pct is not None and pct > cfg.loss_warn_percent:
            emit(
                AlarmLevel.WARN,
                "PACKET_LOSS",
                f"STA {row.mac or row.index} 丢包 {pct:.2f}% > {cfg.loss_warn_percent:.1f}%",
            )
    if snap.errors:
        emit(AlarmLevel.INFO, "POLL_PARTIAL", f"{len(snap.errors)} 项查询失败")
    snap.degraded = any(a.level != AlarmLevel.INFO.value for a in alarms) or bool(snap.errors)
    return alarms


def params_view(snap: LinkSnapshot) -> dict[str, Any]:
    """快照里"可回填配置表单"的那部分，UI 不必反向 import 注册表。"""
    return {
        "endpoint": snap.endpoint,
        "ts": snap.ts,
        "role": snap.role,
        "connected": snap.connected,
        "ssid": snap.ssid,
        "keymgmt": snap.keymgmt,
        "bandwidth_mhz": snap.bandwidth_mhz,
        "freq_range": snap.freq_range,
        "chan_list": snap.chan_list,
        "txpower_dbm": snap.txpower_dbm,
        "tx_mcs": snap.tx_mcs,
        "acktmo_us": snap.acktmo_us,
        "heart_int_ms": snap.heart_int_ms,
        "rssi_dbm": snap.rssi_dbm,
        "evm_db": snap.evm_db,
    }


# --------------------------------------------------------------------- 轮询器
class LinkMonitor:
    """信道遥测轮询器：一条线程按节拍读参数、发快照、判告警。"""

    def __init__(
        self,
        at: AtClient,
        *,
        bus: EventBus | None = None,
        cfg: LinkCfg | None = None,
        log: logging.Logger | None = None,
        history_len: int = 1200,
    ) -> None:
        self.at = at
        self.bus = bus or get_bus()
        self.cfg = cfg or LinkCfg()
        self.log = log or get_logger(f"hmi.protocol.link.{self.at.endpoint}")
        self._lock = threading.Lock()
        self._names: list[str] = []
        #: 跨轮累积的"最近一次读到的结果"：首轮全量后只重读 volatile 项，
        #: 静态参数靠这里保留，避免快照每轮只剩易变字段。
        self._merged: dict[str, tuple[bool, Any, str]] = {}
        self._first_round_done = False
        self._history: deque[LinkSnapshot] = deque(maxlen=max(1, int(history_len)))
        self._last: LinkSnapshot | None = None
        self._last_mono = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._was_stale = False
        self.polls = 0
        self.skips = 0
        self.set_names(self.default_names())

    # ---------------------------------------------------------------- 轮询清单
    def default_names(self) -> list[str]:
        """AP 侧才追加 STA_INFO/WNBCFG；STA 侧这两个命令要么无效要么刷屏。"""
        names = list(self.cfg.read_order)
        role = str(self.at.cfg.local_role or "").lower()
        if role in ("ap", "apsta"):
            names += list(self.cfg.ap_extra)
        return names

    def set_names(self, names: list[str]) -> None:
        """去重保序。未知名字照发：宁可让固件回 ERROR，也不在本地悄悄吞参数。

        清单变了就作废累积结果并重回"首轮全量"，不然新旧清单混在一份快照里
        会出现"没读过的参数显示上一轮的旧值"这种误导性数据。
        """
        out: list[str] = []
        for name in names:
            key = str(name).strip().upper()
            if key and key not in out:
                out.append(key)
        with self._lock:
            self._names = out
            self._merged = {k: v for k, v in self._merged.items() if k in out}
            self._first_round_done = False

    @property
    def names(self) -> list[str]:
        with self._lock:
            return list(self._names)

    def reset(self) -> None:
        """换目标模块时调用：作废跨轮累积并把下一轮当首轮（全量重读）。

        多模块共用一条 AT 通道时，A 模块的静态参数对 B 毫无意义，留着累积
        结果会让 B 的快照混入 A 的旧值。历史曲线与快照本身不动，UI 自行决定
        是否清图。
        """
        with self._lock:
            self._merged.clear()
            self._first_round_done = False

    def _round_names(self) -> list[str]:
        """本轮实际要读的清单。

        首轮全量；之后只读 volatile（RSSI 等）+ 还没读到过或上轮失败的非易变
        项——失败项每轮补读一次，读到成功就不再出现，避免"一次失败永久挂红"。
        volatile 集为空（自定义清单全是静态参数）时退回全量。
        """
        with self._lock:
            names = list(self._names)
            if not (self.cfg.volatile_after_first and self._first_round_done):
                return names
            vol = [n for n in names if registry.is_volatile(n)]
            if not vol:
                return names
            seen = set(vol)
            for n in names:
                got = self._merged.get(n)
                if n not in seen and (got is None or not got[0]):
                    vol.append(n)
                    seen.add(n)
            return vol

    # ------------------------------------------------------------------ 一次轮询
    def poll_once(self, *, publish: bool = True) -> LinkSnapshot | None:
        """读一轮并返回快照；AT 通道被占用时跳过本轮（返回上一快照，不算失败）。

        首轮读全量清单；之后只读 volatile + 待补读项，静态参数从累积结果取值。
        """
        if self.at.busy():
            self.skips += 1
            self.log.debug("AT 通道忙，跳过本轮遥测")
            return self._last
        results = self.at.read_many(self._round_names())
        with self._lock:
            self._merged.update(results)
            merged = dict(self._merged)
            self._first_round_done = True
        snap = build_snapshot(
            merged,
            endpoint=self.at.endpoint,
            stats=dict(self.at.stats()),
        )
        alarms = evaluate(snap, self.cfg)
        with self._lock:
            self._last = snap
            self._last_mono = time.monotonic()
            self._history.append(snap)
            self.polls += 1
        self._was_stale = False
        if publish:
            self.bus.publish(Events.LINK_SNAPSHOT, snap)
            self.bus.publish(Events.RADIO_PARAMS, params_view(snap))
            for alarm in alarms:
                self.bus.publish(Events.ALARM, alarm)
        else:
            for alarm in alarms:
                self.log.debug("告警（未发布）%s: %s", alarm.code, alarm.message)
        return snap

    # ---------------------------------------------------------------- 后台节拍
    def start(self) -> bool:
        """启动轮询线程；``auto_poll`` 关闭时返回 False（"读取一次"按钮仍可用）。"""
        if not self.cfg.auto_poll:
            self.log.info("auto_poll 关闭，遥测只在手动触发时读取")
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="hmi-link", daemon=True)
            self._thread.start()
        return True

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def _loop(self) -> None:
        interval = max(0.5, float(self.cfg.poll_interval_s))
        while not self._stop.wait(interval):
            try:
                self.poll_once()
            except Exception as exc:
                self.log.exception("遥测轮询异常：%s", exc)

    # -------------------------------------------------------------------- 状态
    def snapshot(self) -> LinkSnapshot | None:
        with self._lock:
            return self._last

    def history(self) -> list[LinkSnapshot]:
        with self._lock:
            return list(self._history)

    def age_s(self) -> float:
        with self._lock:
            if not self._last_mono:
                return float("inf")
            return time.monotonic() - self._last_mono

    def is_stale(self) -> bool:
        """超过 ``stale_after_s`` 没有新快照即判过期，UI 据此把数值灰掉。"""
        return self.age_s() > max(1.0, float(self.cfg.stale_after_s))

    def check_stale(self) -> bool:
        """过期只在"刚变过期"时告警一次，避免每轮刷屏。"""
        stale = self.is_stale()
        if stale and not self._was_stale:
            self._was_stale = True
            self.bus.publish(
                Events.ALARM,
                AlarmEvent(
                    level=AlarmLevel.CRIT.value,
                    source=f"link/{self.at.endpoint}",
                    code="TELEMETRY_STALE",
                    message=f"遥测数据已过期（{self.age_s():.0f}s 未更新）",
                ),
            )
        elif not stale:
            self._was_stale = False
        return stale

    def stats(self) -> dict[str, Any]:
        age = self.age_s()
        with self._lock:
            names = len(self._names)
            first_done = self._first_round_done
        return {
            "endpoint": self.at.endpoint,
            "names": names,
            "polls": self.polls,
            "skips": self.skips,
            "full_round_done": first_done,
            "age_s": None if age == float("inf") else round(age, 1),
            "stale": self.is_stale(),
            "interval_s": self.cfg.poll_interval_s,
            "auto_poll": self.cfg.auto_poll,
            "running": bool(self._thread and self._thread.is_alive()),
        }

    def __repr__(self) -> str:
        return f"<LinkMonitor {self.at.endpoint} polls={self.polls}>"
