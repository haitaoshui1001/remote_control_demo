"""运动控制服务单元测试：直通限速、限幅、周期发帧、停止边沿与发布去重。

时间一律显式喂给 ``advance(dt)``，不 sleep、不起节拍线程（生命周期用例除外）——
把"算得对不对"和"调度准不准"分开验。发帧只盯注入的假透传门面，断言的就是
帧字节序列本身；空闲零占用和"失败只报一次"是方案里两条最容易被悄悄破坏的
承诺，各留一条用例钉住。运动不加减速：一拍到位，前后与转向可组合叠加。
"""

from __future__ import annotations

import logging

import pytest

from hmi.core.config import AppConfig, _validate, apply_overrides
from hmi.core.events import EventBus
from hmi.services.motion import DIRECTIONS, MotionService, s16_hex


class FakePT:
    """记录每一帧载荷的假透传；ok=False 模拟通道一直发不出去。"""

    def __init__(self, ok: bool = True) -> None:
        self.calls: list[bytes] = []
        self.notes: list[str] = []
        self.ok = ok

    def send(self, payload, **kwargs):
        self.calls.append(payload)
        self.notes.append(str(kwargs.get("note", "")))
        return {"ok": self.ok, "detail": "" if self.ok else "模拟通道失败"}


def _cfg(**kw) -> AppConfig:
    cfg = AppConfig()
    if kw:
        apply_overrides(cfg, kw)
    return cfg


def _svc(pt: FakePT, **kw) -> MotionService:
    return MotionService(pt, _cfg(**kw))


def _hexes(calls: list[bytes]) -> list[str]:
    return [c.hex().upper() for c in calls]


# ------------------------------------------------------------------ 编码
def test_s16_hex_encoding_and_clamp():
    assert s16_hex(0.0) == "0000"
    assert s16_hex(0.5) == "0032"
    assert s16_hex(-0.5) == "FFCE"
    assert s16_hex(1.0) == "0064"
    assert s16_hex(400.0) == "7FFF", "超界夹到 int16 上限"
    assert s16_hex(-400.0) == "8000", "超界夹到 int16 下限"


# ------------------------------------------------------------------ 按键输入
def test_press_sets_full_speed_target():
    pt = FakePT()
    svc = _svc(pt)
    assert svc.press("up")
    st = svc.snapshot()
    assert st["held"] == ["up"]
    assert st["target_vx"] == pytest.approx(1.0)
    assert st["vx"] == 0.0, "press 只改目标，当前速度下一拍直通到位"


def test_release_clears_target_keeps_current():
    pt = FakePT()
    svc = _svc(pt)
    svc.press("up")
    svc.advance(0.2)
    svc.release("up")
    st = svc.snapshot()
    assert st["target_vx"] == 0.0
    assert st["vx"] == pytest.approx(1.0), "松开瞬间当前值不变，下一拍归零并补停止帧"


def test_opposite_keys_cancel_out():
    pt = FakePT()
    svc = _svc(pt)
    svc.press("up")
    svc.press("down")
    assert svc.snapshot()["target_vx"] == 0.0
    svc2 = _svc(FakePT())
    svc2.press("left")
    svc2.press("right")
    assert svc2.snapshot()["target_vr"] == 0.0


def test_diagonal_combines_linear_and_angular():
    pt = FakePT()
    svc = _svc(pt)
    svc.press("up")
    svc.press("left")
    st = svc.snapshot()
    assert st["target_vx"] == pytest.approx(1.0)
    assert st["target_vr"] == pytest.approx(0.5)
    svc.advance(0.1)
    assert _hexes(pt.calls) == ["7B000003E8000001F4657D"], "前进+转向叠加进同一帧同时下发"


def test_press_directions_are_the_documented_four():
    assert DIRECTIONS == ("up", "down", "left", "right")
    svc = _svc(FakePT())
    assert not svc.press("backwards"), "未知方向必须被拒绝"


def test_release_all_clears_every_held_key():
    svc = _svc(FakePT())
    svc.press("up")
    svc.press("left")
    svc.release_all()
    st = svc.snapshot()
    assert st["held"] == [] and st["target_vx"] == 0.0 and st["target_vr"] == 0.0


def test_set_target_clamps_to_configured_limits():
    svc = _svc(FakePT())
    svc.set_target(5.0, -9.0)
    st = svc.snapshot()
    assert st["target_vx"] == pytest.approx(1.0)
    assert st["target_vr"] == pytest.approx(-0.5)


# ------------------------------------------------------------------ 直通与发帧
def test_speed_snaps_to_target_in_one_tick():
    pt = FakePT()
    svc = _svc(pt)
    svc.press("up")
    svc.advance(0.1)
    assert svc.snapshot()["vx"] == pytest.approx(1.0), "不加减速：一拍直接到满速"


def test_release_snaps_to_zero_next_tick():
    pt = FakePT()
    svc = _svc(pt)
    svc.press("up")
    svc.advance(0.1)
    assert svc.snapshot()["vx"] == pytest.approx(1.0)
    svc.release("up")
    svc.advance(0.1)
    assert svc.snapshot()["vx"] == 0.0, "松手下一拍直接归零"


def test_frame_sequence_snap_and_single_stop_frame():
    pt = FakePT()
    svc = _svc(pt)
    svc.press("up")
    svc.advance(0.1)
    svc.advance(0.1)
    svc.release("up")
    svc.advance(0.1)
    assert _hexes(pt.calls) == [
        "7B000003E800000000907D",
        "7B000003E800000000907D",
        "7B00000000000000007B7D",
    ], "活动期每拍一发，落到零只补一帧停止"
    n = len(pt.calls)
    for _ in range(5):
        svc.advance(0.1)
    assert len(pt.calls) == n, "停稳后空闲零占用"


def test_angular_frame_carries_vr_slot():
    pt = FakePT()
    svc = _svc(pt)
    svc.press("left")
    svc.advance(0.1)
    assert _hexes(pt.calls) == ["7B00000000000001F48E7D"], "左转满速 0.5 rad/s→Z 槽 01F4"


def test_idle_ticks_send_nothing():
    pt = FakePT()
    svc = _svc(pt)
    for _ in range(5):
        svc.advance(0.1)
    assert pt.calls == []


def test_emergency_stop_zeroes_immediately_and_sends_stop_frame():
    pt = FakePT()
    svc = _svc(pt)
    svc.press("up")
    svc.press("left")
    svc.advance(0.2)
    svc.emergency_stop()
    st = svc.snapshot()
    assert st["vx"] == 0.0 and st["vr"] == 0.0
    assert st["held"] == [] and not st["active"]
    assert pt.calls[-1] == bytes.fromhex("7B00000000000000007B7D")
    n = len(pt.calls)
    svc.advance(0.1)
    assert len(pt.calls) == n, "急停后空闲不再发帧"


def test_invert_angular_flips_z_slot():
    pt = FakePT()
    svc = _svc(pt, **{"motion.invert_angular": "true"})
    svc.press("left")
    svc.advance(0.1)
    assert _hexes(pt.calls) == ["7B000000000000FE0C897D"], "翻转后 +0.5→-0.5→FE0C"


def test_custom_frame_template_is_rendered():
    pt = FakePT()
    svc = _svc(pt, **{"motion.frame_protocol": "template", "motion.frame_hex": "BB CC {vx} {vr}"})
    svc.press("up")
    svc.advance(0.1)
    assert _hexes(pt.calls) == ["BBCC00640000"], "满速 1.0 → ×100 = 0064"


# ------------------------------------------------------------------ 开关与异常收口
def test_disabled_config_blocks_start_and_press():
    pt = FakePT()
    svc = _svc(pt, **{"motion.enabled": "false"})
    assert svc.start() is False
    assert not svc.running
    assert svc.press("up") is False
    svc.advance(0.1)
    assert pt.calls == []


def test_send_failure_warns_only_once(caplog):
    pt = FakePT(ok=False)
    svc = _svc(pt)
    svc.press("up")
    with caplog.at_level(logging.WARNING, logger="hmi.services.motion"):
        for _ in range(3):
            svc.advance(0.1)
    assert svc.send_failures == 3
    warned = [r for r in caplog.records if "发送失败" in r.getMessage()]
    assert len(warned) == 1, "持续失败只报一次，防刷屏"


def test_bad_frame_hex_fails_counted_not_raised():
    pt = FakePT()
    svc = _svc(pt, **{"motion.frame_protocol": "template", "motion.frame_hex": "ZZ {vx} {vr}"})
    svc.press("up")
    svc.advance(0.1)
    assert pt.calls == []
    assert svc.send_failures >= 1


# ------------------------------------------------------------------ 发布去重
def test_publish_events_dedup_on_identical_state():
    bus = EventBus()
    seen: list[dict] = []
    bus.subscribe("robot/motion", lambda _t, payload: seen.append(payload))
    svc = MotionService(FakePT(), _cfg(), bus=bus)
    svc.press("up")
    assert len(seen) == 1
    svc.advance(0.1)
    assert len(seen) == 2, "当前值变了要发"
    svc.release("up")
    svc.advance(1.0)
    assert len(seen) == 4, "松开一拍 + 归零各发一条"
    for _ in range(5):
        svc.advance(0.1)
    assert len(seen) == 4, "停稳后的空闲拍全部去重"


# ------------------------------------------------------------------ 生命周期
def test_stop_sends_pending_stop_frame_and_resets():
    pt = FakePT()
    svc = _svc(pt)
    svc.press("up")
    svc.advance(0.1)
    svc.stop()
    st = svc.snapshot()
    assert not svc.running
    assert st["vx"] == 0.0 and not st["active"]
    assert pt.calls[-1] == bytes.fromhex("7B00000000000000007B7D"), "趁通道未关补停止帧"


def test_start_stop_round_trip_with_ticker():
    svc = _svc(FakePT())
    assert svc.start() is True and svc.running
    svc.press("up")
    svc.release("up")
    svc.stop()
    assert not svc.running and svc.snapshot()["vx"] == 0.0


# ------------------------------------------------------------------ 配置夹取
def test_validate_clamps_nonsense_motion_values():
    cfg = _cfg(
        **{
            "motion.max_linear_speed": "0",
            "motion.max_angular_speed": "-1",
            "motion.send_period_ms": "10",
        }
    )
    _validate(cfg)
    assert cfg.motion.max_linear_speed == pytest.approx(1.0)
    assert cfg.motion.max_angular_speed == pytest.approx(0.5)
    assert cfg.motion.send_period_ms == 50
    assert any("运动" in w for w in cfg.meta.warnings)


# ------------------------------------------------------------------ 备注翻译成人话
def test_wheeltec_tx_note_shows_speeds():
    pt = FakePT()
    svc = _svc(pt)
    svc.press("up")
    svc.advance(0.2)
    assert pt.calls, "按住期间必须每拍发帧"
    assert "速度指令" in pt.notes[-1]
    assert "前向速度 1.000 m/s" in pt.notes[-1]
    assert "角速度 0.000 rad/s" in pt.notes[-1]


def test_template_tx_note_falls_back_to_motion():
    pt = FakePT()
    svc = _svc(pt, **{"motion.frame_protocol": "template", "motion.frame_hex": "BB CC {vx} {vr}"})
    svc.press("up")
    svc.advance(0.2)
    assert pt.notes == ["motion"], "非 Wheeltec 帧解不动就退回短备注，不许编造数值"
