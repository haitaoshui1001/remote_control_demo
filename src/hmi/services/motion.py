"""运动控制服务：vx/vr 双通道斜坡 + 按住周期重发 + 松开滑停补发停止帧。

与透传/遥测服务同一套收口纪律：
- ``advance(dt)`` 是纯计算，时间由调用方显式给出，单测无需 sleep 也无时钟依赖；
- 唯一 IO 点是 ``passthrough.send()``，该门面已把异常收口成 ``{ok: False}``，
  本服务对外永不抛异常，发送失败只在边沿上告警一次防刷屏；
- 空闲（目标 0 且当前 0 且停止帧已补发）时一拍都不发，零链路占用；
- 周期重发是抗丢包设计：远端哪怕丢两三帧，按住期间速度指令仍是连续的。

方向约定（与界面键盘一致）：up/down 改 vx，left/right 改 vr，左转为正。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from ..core.config import AppConfig
from ..core.events import EventBus, Events
from ..core.utils import hex_to_bytes
from ..protocol.telemetry_frames import build_speed_frame, describe_wheeltec_frame
from .passthrough import PassthroughService
from .periodic import Ticker

DIRECTIONS = ("up", "down", "left", "right")


def s16_hex(value: float) -> str:
    """速度值 ×100 编成 2 字节有符号大端 hex：0.5 -> 0032，-0.5 -> FFCE。"""
    raw = int(round(value * 100.0))
    raw = max(-32768, min(32767, raw))
    return f"{raw & 0xFFFF:04X}"


class MotionService:
    """运动状态机：目标值由按键决定，当前值每拍沿斜率逼近目标。"""

    def __init__(
        self,
        passthrough: PassthroughService,
        cfg: AppConfig,
        *,
        bus: EventBus | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self.passthrough = passthrough
        self.cfg = cfg
        self.mcfg = cfg.motion
        self._wheeltec = (
            str(getattr(cfg.motion, "frame_protocol", "wheeltec") or "wheeltec")
            .strip()
            .lower()
            != "template"
        )
        self.bus = bus
        self.log = log or logging.getLogger("hmi.services.motion")
        self._lock = threading.RLock()
        self._vx = 0.0
        self._vr = 0.0
        self._tvx = 0.0
        self._tvr = 0.0
        self._held: set[str] = set()
        self._active = False
        self._last_dt_ref = 0.0
        self._last_key: tuple | None = None
        self.sent_frames = 0
        self.send_failures = 0
        self._failed_edge = False
        self._ticker = Ticker(
            "motion-send",
            self._tick,
            self.mcfg.send_period_ms / 1000.0,
            log=self.log,
        )
        self.running = False

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        if not self.mcfg.enabled:
            self.log.info("motion.enabled=False，运动控制不启动")
            return False
        self._last_dt_ref = 0.0
        self._ticker.start()
        self.running = True
        return True

    def stop(self) -> None:
        """先停节拍再复位；若仍在活动态，趁通道未关补发一次停止帧。"""
        self._ticker.stop()
        self.running = False
        with self._lock:
            if self.mcfg.enabled and (self._active or self._vx or self._vr):
                self._send(0.0, 0.0)
            self._held.clear()
            self._vx = self._vr = self._tvx = self._tvr = 0.0
            self._active = False
        self._publish()

    # ------------------------------------------------------------------ 按键输入
    def press(self, direction: str) -> bool:
        if not self.mcfg.enabled or direction not in DIRECTIONS:
            return False
        with self._lock:
            self._held.add(direction)
            self._recompute_target()
        self._publish()
        return True

    def release(self, direction: str) -> bool:
        if direction not in DIRECTIONS:
            return False
        with self._lock:
            self._held.discard(direction)
            self._recompute_target()
        self._publish()
        return True

    def release_all(self) -> None:
        with self._lock:
            self._held.clear()
            self._recompute_target()
        self._publish()

    def set_target(self, vx: float, vr: float) -> None:
        """直接给目标速度（限幅后），供脚本/摇杆等非键盘入口使用。"""
        m = self.mcfg
        vx = max(-m.max_linear_speed, min(m.max_linear_speed, float(vx)))
        vr = max(-m.max_angular_speed, min(m.max_angular_speed, float(vr)))
        with self._lock:
            self._tvx = vx
            self._tvr = vr
        self._publish()

    def emergency_stop(self) -> None:
        """失焦/切页急停：当前值直接清零并立即补停止帧，不走斜坡。"""
        with self._lock:
            self._held.clear()
            self._tvx = self._tvr = 0.0
            self._vx = self._vr = 0.0
            self._active = False
            if self.mcfg.enabled:
                self._send(0.0, 0.0)
        self._publish()

    # ------------------------------------------------------------------ 斜坡与发帧
    def _recompute_target(self) -> None:
        m = self.mcfg
        lin = (1 if "up" in self._held else 0) - (1 if "down" in self._held else 0)
        ang = (1 if "left" in self._held else 0) - (1 if "right" in self._held else 0)
        self._tvx = lin * m.max_linear_speed
        self._tvr = ang * m.max_angular_speed

    @staticmethod
    def _ramp(cur: float, target: float, accel: float, decel: float, dt: float) -> float:
        if cur == target:
            return cur
        slope = decel if abs(target) < abs(cur) else accel
        delta = slope * max(0.0, float(dt))
        diff = target - cur
        if abs(diff) <= delta:
            return target
        return cur + (delta if diff > 0 else -delta)

    def advance(self, dt: float) -> dict[str, Any]:
        """推进一拍：斜坡更新 + 该发则发（活动期每拍发，落零边沿补一次停止帧）。"""
        with self._lock:
            m = self.mcfg
            self._vx = self._ramp(self._vx, self._tvx, m.linear_accel, m.linear_decel, dt)
            self._vr = self._ramp(self._vr, self._tvr, m.angular_accel, m.angular_decel, dt)
            moving = self._vx != 0.0 or self._vr != 0.0
            if moving:
                self._send(self._vx, self._vr)
                self._active = True
            elif self._active:
                self._send(0.0, 0.0)
                self._active = False
        self._publish()
        return self.snapshot()

    def _tick(self) -> None:
        now = time.monotonic()
        if self._last_dt_ref <= 0.0:
            dt = self._ticker.interval_s
        else:
            dt = now - self._last_dt_ref
        self._last_dt_ref = now
        # 进程挂起/节流恢复后 dt 可能巨大，限一拍上限防止速度直接跳变。
        self.advance(min(dt, 1.0))

    def _render(self, vx: float, vr: float) -> bytes:
        if self._wheeltec:
            return build_speed_frame(
                vx, 0.0, vr, invert_angular=bool(self.mcfg.invert_angular)
            )
        body = (self.mcfg.frame_hex or "").replace("{vx}", s16_hex(vx)).replace(
            "{vr}", s16_hex(vr)
        )
        return hex_to_bytes(body)

    def _send(self, vx: float, vr: float) -> bool:
        try:
            frame = self._render(vx, vr)
        except ValueError as exc:
            self.send_failures += 1
            self.log.error("运动帧模板渲染失败（检查 motion.frame_hex）：%s", exc)
            return False
        note = describe_wheeltec_frame(frame) or "motion"
        res = self.passthrough.send(frame, kind="motion", note=note)
        if res.get("ok"):
            self.sent_frames += 1
            self._failed_edge = False
            return True
        self.send_failures += 1
        if not self._failed_edge:
            self._failed_edge = True
            self.log.warning("运动帧发送失败：%s", res.get("detail", ""))
        return False

    # ------------------------------------------------------------------ 状态快照
    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "vx": round(self._vx, 4),
            "vr": round(self._vr, 4),
            "target_vx": round(self._tvx, 4),
            "target_vr": round(self._tvr, 4),
            "moving": self._vx != 0.0 or self._vr != 0.0,
            "held": sorted(self._held),
            "active": self._active,
            "sent_frames": self.sent_frames,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def _publish(self) -> None:
        if self.bus is None:
            return
        with self._lock:
            snap = self._snapshot_locked()
        key = (
            snap["vx"],
            snap["vr"],
            snap["target_vx"],
            snap["target_vr"],
            snap["active"],
            tuple(snap["held"]),
        )
        if key == self._last_key:
            return
        self._last_key = key
        self.bus.publish(Events.MOTION_STATE, snap)

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": self.mcfg.enabled,
            "running": self.running,
            "send_period_ms": self.mcfg.send_period_ms,
            "sent_frames": self.sent_frames,
            "send_failures": self.send_failures,
            "state": self.snapshot(),
            "ticker": self._ticker.stats(),
        }

    def __repr__(self) -> str:
        s = self.snapshot()
        return f"<MotionService vx={s['vx']} vr={s['vr']} sent={self.sent_frames}>"
