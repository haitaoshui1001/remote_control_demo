"""机器人遥测服务：从透传字节流旁路解析遥测帧，维护缓存、新鲜度与落盘。

字节入口是订阅 ``Events.PT_BYTES`` 而不是再挂一次链路分流器——"一条字节通道
只能有一个读者"，透传引擎已经消费了分流字节；每条 ``ByteRecord`` 本身就是按
空闲间隔切好段的数据块，拿它的 ``data`` 喂解析器即可。**匹配不到帧特征的字节
不受任何影响**，数据页与 hex/ASCII 流水照常显示，解析失败绝不吞数据。

三节拍 + 一个入口：

- ``feed``（串口线程回调）：解析 → 入缓冲 → 边沿告警 → 按 ``display_throttle_hz``
  节流发布 ``TELEMETRY_SAMPLE``（合并类事件，UI 只取最新一条）；
- 1 秒新鲜度节拍：超过 ``stale_after_s`` 无有效帧，"刚变过期"只告警一次（参照
  链路遥测的防刷屏规则），并强制推一次事件让界面立刻置灰；
- 1 秒 CSV 节拍：把缓冲出的样本批量冲刷到 ``data/telemetry_日期.csv``；
- 10Hz mock 节拍（``telemetry.mock.enabled`` 为 auto 时跟随 ``app.mock``）：
  按当前占位协议组假帧，走**同一解析路径**，界面与联调行为一致。
"""

from __future__ import annotations

import csv
import logging
import math
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.config import AppConfig, project_root
from ..core.events import EventBus, Events, Throttle, get_bus
from ..core.logger import get_logger
from ..core.models import AlarmEvent, AlarmLevel
from ..core.utils import parse_bool
from ..protocol.telemetry_frames import (
    TelemetrySample,
    create_parser,
    encode_ascii_line,
    encode_binary_frame,
    encode_wheeltec_frame,
)

__all__ = ["TelemetryService"]

CSV_HEADER = ["时间", "状态", "电压V", "电流A", "电量%", "速度m/s", "里程m", "纬度", "经度"]


class TelemetryService:
    """遥测旁路解析门面；UI 只读 ``latest``/``history``，不碰解析器。"""

    def __init__(
        self,
        passthrough: Any,
        cfg: AppConfig,
        *,
        bus: EventBus | None = None,
        log: logging.Logger | None = None,
        mock_interval_s: float = 0.1,
        tick_interval_s: float = 1.0,
    ) -> None:
        self.passthrough = passthrough
        self.cfg = cfg
        self.tcfg = cfg.telemetry
        self.bus = bus if bus is not None else get_bus()
        self.log = log or get_logger("hmi.services.telemetry")
        self.parser = create_parser(self.tcfg.frame)
        self._mock_interval_s = max(0.02, float(mock_interval_s))
        self._tick_interval_s = max(0.2, float(tick_interval_s))
        self._lock = threading.Lock()
        self._history: deque[TelemetrySample] = deque(
            maxlen=max(20, int(self.tcfg.curve.window_s * 20))
        )
        self._latest: TelemetrySample | None = None
        self._prev: TelemetrySample | None = None
        self._last_good_ts = 0.0
        self._distance_m = 0.0
        self._stale = False
        self._low_voltage = False
        self._estop = False
        self._throttle = Throttle(1.0 / max(0.5, float(self.tcfg.display_throttle_hz)))
        self._csv_pending: list[TelemetrySample] = []
        self._csv_dir = self._resolve_csv_dir(cfg)
        self.parsed = 0
        self.published = 0
        self._token: int | None = None
        from .periodic import Ticker

        self._mock_t = 0.0
        self._mock_soc = 88.0
        self._mock_ticker = Ticker(
            "telemetry-mock", self._mock_step, self._mock_interval_s, log=self.log
        )
        self._age_ticker = Ticker(
            "telemetry-age", self._freshness_tick, self._tick_interval_s, log=self.log
        )
        self._csv_ticker = Ticker(
            "telemetry-csv", self.flush_csv, self._tick_interval_s, log=self.log
        )
        self._mock_on = self._mock_wanted()

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _resolve_csv_dir(cfg: AppConfig) -> Path:
        raw = str(cfg.app.data_dir or "data")
        p = Path(raw)
        return p if p.is_absolute() else project_root() / p

    def _mock_wanted(self) -> bool:
        val = str(self.tcfg.mock.enabled or "auto").strip().lower()
        if val in ("auto", "", "follow"):
            return bool(self.cfg.app.mock)
        return parse_bool(val, default=bool(self.cfg.app.mock))

    # ------------------------------------------------------------------ 起停
    def start(self) -> bool:
        if not self.tcfg.enabled:
            return False
        if self._token is None:
            self._token = self.bus.subscribe(Events.PT_BYTES, self._on_pt)
        self._age_ticker.start()
        self._mock_on = self._mock_wanted()
        if self._mock_on:
            self._mock_ticker.start()
        if self.tcfg.log_csv:
            self._csv_ticker.start()
        self.log.info(
            "遥测服务启动：协议=%s 节流=%shz mock=%s 落盘=%s",
            self.tcfg.frame.protocol,
            self.tcfg.display_throttle_hz,
            self._mock_on,
            self.tcfg.log_csv,
        )
        return True

    def stop(self) -> None:
        self._mock_ticker.stop()
        self._csv_ticker.stop()
        self._age_ticker.stop()
        if self._token is not None:
            self.bus.unsubscribe(self._token)
            self._token = None
        self.flush_csv()

    @property
    def running(self) -> bool:
        return self._token is not None

    # ------------------------------------------------------------------ 入口
    def _on_pt(self, _topic: str, payload: object) -> None:
        rec = payload
        if getattr(rec, "direction", None) != "rx":
            return
        data = getattr(rec, "data", b"")
        ts = float(getattr(rec, "ts", 0.0) or time.time())
        if data:
            self.feed(bytes(data), ts)

    def feed(self, data: bytes, ts: float | None = None) -> list[TelemetrySample]:
        """喂一段透传字节，返回其中解析出的样本（测试与自检可直接调用）。"""
        if not self.tcfg.enabled or not data:
            return []
        ref = float(ts) if ts is not None else time.time()
        out: list[TelemetrySample] = []
        try:
            samples = self.parser.feed(bytes(data))
        except Exception as exc:
            self.log.exception("遥测解析异常：%s", exc)
            return []
        for sample in samples:
            sample.ts = ref
            self._ingest(sample)
            out.append(sample)
        return out

    def _ingest(self, sample: TelemetrySample) -> None:
        with self._lock:
            if self._prev is not None and sample.speed is not None:
                ps = self._prev.speed
                if ps is not None:
                    dt = sample.ts - self._prev.ts
                    if 0.0 < dt <= 5.0:
                        self._distance_m += 0.5 * (ps + sample.speed) * dt
            sample.distance = round(self._distance_m, 2)
            self._prev = sample
            self._latest = sample
            self._history.append(sample)
            self._last_good_ts = sample.ts
            self.parsed += 1
            if self.tcfg.log_csv:
                self._csv_pending.append(sample)
            recovered = self._stale
            if recovered:
                self._stale = False
        if recovered:
            self._publish_alarm(
                AlarmLevel.INFO, "telemetry_resumed", "遥测已恢复"
            )
        self._check_edges(sample)
        self._publish_sample(sample, stale=False, age_s=0.0, force=recovered)

    def _check_edges(self, sample: TelemetrySample) -> None:
        if sample.voltage is not None:
            low = sample.voltage < float(self.tcfg.battery.low_voltage_v)
            if low != self._low_voltage:
                self._low_voltage = low
                if low:
                    self._publish_alarm(
                        AlarmLevel.WARN,
                        "battery_low_voltage",
                        f"电池电压 {sample.voltage:.2f}V 低于阈值 "
                        f"{self.tcfg.battery.low_voltage_v:.1f}V",
                    )
                else:
                    self._publish_alarm(
                        AlarmLevel.INFO,
                        "battery_low_voltage_cleared",
                        f"电池电压回升至 {sample.voltage:.2f}V",
                    )
        if sample.flag_stop is not None:
            estop = sample.flag_stop != 0
        else:
            estop = "急停" in self.tcfg.status_text(sample.status)
        if estop != self._estop:
            self._estop = estop
            if estop:
                self._publish_alarm(AlarmLevel.CRIT, "robot_estop", "机器人状态：急停")
            else:
                self._publish_alarm(AlarmLevel.INFO, "robot_estop_released", "急停已解除")

    # ------------------------------------------------------------------ 节拍
    def _freshness_tick(self) -> None:
        self.check_freshness()

    def check_freshness(self, now: float | None = None) -> bool:
        """新鲜度判定：返回判定后的 stale 状态（``now`` 供测试注入时钟）。"""
        ref = float(now) if now is not None else time.time()
        with self._lock:
            if self._latest is None or self._last_good_ts <= 0.0:
                return self._stale
            age = ref - self._last_good_ts
            becoming = (not self._stale) and age > float(self.tcfg.stale_after_s)
            if becoming:
                self._stale = True
        if becoming:
            self._publish_alarm(
                AlarmLevel.WARN,
                "telemetry_stale",
                f"遥测中断：超过 {self.tcfg.stale_after_s:.0f}s 未收到有效帧",
            )
            with self._lock:
                sample = self._latest
            self._publish_sample(sample, stale=True, age_s=age, force=True)
        return self._stale

    def flush_csv(self) -> int:
        """把积压样本写入 CSV，返回写入行数（节拍与 stop 都会调）。"""
        with self._lock:
            rows, self._csv_pending = self._csv_pending, []
        if not rows:
            return 0
        try:
            self._csv_dir.mkdir(parents=True, exist_ok=True)
            path = self._csv_path(ref=rows[0].ts)
            is_new = not path.exists()
            with path.open("a", newline="", encoding="utf-8-sig") as fh:
                writer = csv.writer(fh)
                if is_new:
                    writer.writerow(CSV_HEADER)
                for sample in rows:
                    writer.writerow(self._csv_row(sample))
            return len(rows)
        except OSError as exc:
            self.log.warning("遥测 CSV 落盘失败（继续运行）：%s", exc)
            with self._lock:
                merged = rows + self._csv_pending
                self._csv_pending = merged[-5000:]
            return 0

    def _csv_path(self, ref: float | None = None) -> Path:
        stamp = datetime.fromtimestamp(ref if ref else time.time()).strftime("%Y%m%d")
        return self._csv_dir / f"telemetry_{stamp}.csv"

    def _csv_row(self, sample: TelemetrySample) -> list[str]:
        dt = datetime.fromtimestamp(sample.ts) if sample.ts else datetime.now()
        if sample.flag_stop is not None:
            state = "使能" if sample.flag_stop == 0 else "失能"
        else:
            state = self.tcfg.status_text(sample.status)
        return [
            f"{dt.strftime('%Y-%m-%d %H:%M:%S')}.{int((sample.ts % 1) * 1000):03d}",
            state,
            self._num(sample.voltage, 2),
            self._num(sample.current, 2),
            self._num(sample.soc, 0),
            self._num(sample.speed, 3),
            self._num(sample.distance, 2),
            self._num(sample.latitude, 6),
            self._num(sample.longitude, 6),
        ]

    @staticmethod
    def _num(value: float | None, digits: int) -> str:
        if value is None:
            return ""
        return f"{value:.{digits}f}"

    # ------------------------------------------------------------------ mock 源
    def _mock_step(self) -> None:
        self._mock_t += self._mock_interval_s
        t = self._mock_t
        status = int((t / 30.0) % 3)
        voltage = 23.2 + 2.6 * math.sin(t / 38.0)
        current = 2.4 + 1.1 * math.sin(t / 7.0)
        self._mock_soc = max(8.0, self._mock_soc - self._mock_interval_s * 0.05)
        speed = max(0.0, 0.55 + 0.45 * math.sin(t / 9.0))
        latitude = 39.909236 + 0.000006 * t
        longitude = 116.397428 + 0.000008 * t
        protocol = str(self.tcfg.frame.protocol or "wheeltec").strip().lower()
        if protocol == "ascii":
            raw = encode_ascii_line(
                status, voltage, current, self._mock_soc, speed, latitude, longitude
            )
        elif protocol == "wheeltec":
            raw = encode_wheeltec_frame(
                flag_stop=0,
                voltage=voltage,
                vx=speed,
                vy=0.12 * math.sin(t / 17.0),
                wz=0.30 * math.sin(t / 13.0),
                ax=0.4 * math.sin(t / 5.0),
                ay=0.0,
                az=9.8,
                gx=0.0,
                gy=0.0,
                gz=0.30 * math.sin(t / 13.0),
            )
        else:
            raw = encode_binary_frame(
                status,
                voltage,
                current,
                self._mock_soc,
                speed,
                latitude,
                longitude,
                header_hex=self.tcfg.frame.header_hex,
            )
        self.feed(raw, time.time())

    # ------------------------------------------------------------------ 发布
    def _publish_sample(
        self,
        sample: TelemetrySample | None,
        *,
        stale: bool,
        age_s: float,
        force: bool = False,
    ) -> None:
        if sample is None:
            return
        if not force and not self._throttle.allow():
            return
        payload = {"sample": sample, "stale": bool(stale), "age_s": float(age_s)}
        self.bus.publish(Events.TELEMETRY_SAMPLE, payload)
        self.published += 1

    def _publish_alarm(self, level: AlarmLevel, code: str, message: str) -> None:
        try:
            self.bus.publish(
                Events.ALARM,
                AlarmEvent(
                    ts=time.time(),
                    level=level,
                    source="telemetry",
                    code=code,
                    message=message,
                ),
            )
        except Exception as exc:
            self.log.warning("遥测告警发布失败：%s", exc)

    # ------------------------------------------------------------------ 取值
    def latest(self) -> TelemetrySample | None:
        with self._lock:
            return self._latest

    def history(self) -> list[TelemetrySample]:
        with self._lock:
            return list(self._history)

    def age_s(self, now: float | None = None) -> float:
        ref = float(now) if now is not None else time.time()
        with self._lock:
            if self._last_good_ts <= 0.0:
                return float("inf")
            return max(0.0, ref - self._last_good_ts)

    @property
    def is_stale(self) -> bool:
        with self._lock:
            return self._stale

    @property
    def is_low_voltage(self) -> bool:
        with self._lock:
            return self._low_voltage

    @property
    def distance_m(self) -> float:
        with self._lock:
            return self._distance_m

    # ------------------------------------------------------------------ 汇总
    def summary(self) -> dict[str, Any]:
        with self._lock:
            age = (
                float("inf")
                if self._last_good_ts <= 0.0
                else max(0.0, time.time() - self._last_good_ts)
            )
            return {
                "enabled": bool(self.tcfg.enabled),
                "running": self._token is not None,
                "protocol": self.tcfg.frame.protocol,
                "mock": bool(self._mock_on),
                "parsed": self.parsed,
                "published": self.published,
                "history": len(self._history),
                "stale": self._stale,
                "age_s": None if age == float("inf") else round(age, 1),
                "low_voltage": self._low_voltage,
                "estop": self._estop,
                "distance_m": round(self._distance_m, 1),
                "csv": str(self._csv_path()) if self.tcfg.log_csv else "",
            }
