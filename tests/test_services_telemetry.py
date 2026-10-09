"""遥测服务单元测试：旁路入口、新鲜度、边沿告警、节流、里程与落盘。

一律直接 ``feed()`` 或手动 ``check_freshness(now)``，把时间压成确定性输入；
mock 关闭、CSV 目录指向 ``tmp_path``，不碰工程 ``data/``。重点验证"只发一次"
的边沿规则（刷屏是野外最烦人的失败模式）和"节流不影响数据完整"（历史与
落盘要全量，界面才允许丢帧）。
"""

from __future__ import annotations

import csv
import time
from pathlib import Path

import pytest

from hmi.core.config import load_config
from hmi.core.events import EventBus, Events
from hmi.core.models import AlarmEvent, AlarmLevel, ByteRecord
from hmi.protocol.telemetry_frames import (
    encode_ascii_line,
    encode_binary_frame,
    encode_wheeltec_frame,
)
from hmi.services.telemetry import CSV_HEADER, TelemetryService

FIELDS = dict(status=2, voltage=24.61, current=3.21, soc=78.0, speed=0.45,
              latitude=39.909236, longitude=116.397428)


def _binary(**kw) -> bytes:
    args = {**FIELDS, **kw}
    return encode_binary_frame(
        args["status"], args["voltage"], args["current"], args["soc"],
        args["speed"], args["latitude"], args["longitude"],
    )


def _ascii(**kw) -> bytes:
    args = {**FIELDS, **kw}
    return encode_ascii_line(
        args["status"], args["voltage"], args["current"], args["soc"],
        args["speed"], args["latitude"], args["longitude"],
    )


def _cfg(tmp_path: Path, **kw) -> object:
    base = {
        "app.mock": False,
        "app.data_dir": str(tmp_path / "data"),
        "telemetry.mock.enabled": "false",
        "telemetry.frame.protocol": "binary",
    }
    base.update(kw)
    return load_config(None, **base)


@pytest.fixture()
def bus() -> EventBus:
    return EventBus()


@pytest.fixture()
def alarms(bus: EventBus) -> list[AlarmEvent]:
    out: list[AlarmEvent] = []
    bus.subscribe(Events.ALARM, lambda _t, payload: out.append(payload))
    return out


def _svc(bus: EventBus, tmp_path: Path, **kw) -> TelemetryService:
    return TelemetryService(None, _cfg(tmp_path, **kw), bus=bus)


# ------------------------------------------------------------------ 解析入口
def test_feed_binary_fills_latest_and_history(bus, tmp_path):
    svc = _svc(bus, tmp_path)
    out = svc.feed(_binary(), ts=1000.0)
    assert len(out) == 1
    assert svc.latest() is out[0]
    assert svc.latest().ts == 1000.0
    assert svc.latest().voltage == pytest.approx(24.61)
    assert svc.distance_m == pytest.approx(0.0), "首帧无前帧可积分"
    assert len(svc.history()) == 1
    assert svc.parsed == 1


def test_feed_ascii_protocol_when_configured(bus, tmp_path):
    svc = _svc(bus, tmp_path, **{"telemetry.frame.protocol": "ascii"})
    assert svc.parser.name == "ascii"
    assert svc.feed(_ascii(voltage=23.5), ts=2000.0)[0].voltage == pytest.approx(23.5)


def test_feed_ignores_unrelated_bytes(bus, tmp_path):
    svc = _svc(bus, tmp_path)
    assert svc.feed(b"AT+CHANNEL?\r\nOK\r\n") == []
    assert svc.latest() is None
    assert svc.age_s() == float("inf")


def test_feed_respects_disabled_switch(bus, tmp_path):
    svc = _svc(bus, tmp_path, **{"telemetry.enabled": False})
    assert svc.feed(_binary()) == []
    assert svc.start() is False


def test_feed_uses_arrival_time_when_ts_missing(bus, tmp_path):
    svc = _svc(bus, tmp_path)
    before = time.time()
    sample = svc.feed(_binary())[0]
    assert sample.ts >= before


# ------------------------------------------------------------------ 旁路订阅
def test_subscribes_pt_bytes_and_only_ingests_rx(bus, tmp_path):
    svc = _svc(bus, tmp_path)
    assert svc.start() is True
    try:
        bus.publish(Events.PT_BYTES, ByteRecord(ts=3000.0, direction="tx", data=_binary()))
        assert svc.latest() is None, "发出的字节不该被当遥测解析"
        bus.publish(Events.PT_BYTES, ByteRecord(ts=3001.0, direction="rx", data=_binary()))
        assert svc.latest() is not None
        assert svc.latest().ts == 3001.0
    finally:
        svc.stop()
    assert svc.running is False
    bus.publish(Events.PT_BYTES, ByteRecord(ts=3002.0, direction="rx", data=_binary()))
    assert svc.latest().ts == 3001.0, "退订后不再吃数据"


# ------------------------------------------------------------------ 节流发布
def test_publish_is_throttled_but_history_is_complete(bus, tmp_path):
    svc = _svc(bus, tmp_path, **{"telemetry.display_throttle_hz": 0.5})
    seen: list[dict] = []
    bus.subscribe(Events.TELEMETRY_SAMPLE, lambda _t, p: seen.append(p))
    for i in range(10):
        svc.feed(_binary(voltage=24.0 + i * 0.1), ts=4000.0 + i * 0.1)
    assert len(svc.history()) == 10, "节流只影响界面，不影响留痕"
    assert len(seen) == 1
    assert seen[0]["sample"].voltage == pytest.approx(24.0)
    assert seen[0]["stale"] is False


def test_stale_edge_forces_one_extra_publish(bus, tmp_path, alarms):
    svc = _svc(bus, tmp_path, **{"telemetry.stale_after_s": 3.0})
    seen: list[dict] = []
    bus.subscribe(Events.TELEMETRY_SAMPLE, lambda _t, p: seen.append(p))
    svc.feed(_binary(), ts=5000.0)
    before = len(seen)
    assert svc.check_freshness(now=5005.0) is True
    assert svc.is_stale is True
    assert svc.check_freshness(now=5006.0) is True, "已过期时不重复判定"
    assert len(seen) == before + 1, "刚变过期要立刻推一次让界面置灰"
    assert [a.code for a in alarms] == ["telemetry_stale"]
    assert svc.is_stale is True

    svc.feed(_binary(), ts=5007.0)
    assert svc.is_stale is False
    assert [a.code for a in alarms][-1] == "telemetry_resumed"
    assert seen[-1]["stale"] is False


# ------------------------------------------------------------------ 边沿告警
def test_low_voltage_alarms_once_and_recovers(bus, tmp_path, alarms):
    svc = _svc(bus, tmp_path)
    svc.feed(_binary(voltage=21.0), ts=6000.0)
    svc.feed(_binary(voltage=20.5), ts=6001.0)
    assert [a.code for a in alarms] == ["battery_low_voltage"]
    assert alarms[0].level == AlarmLevel.WARN
    assert svc.is_low_voltage is True
    svc.feed(_binary(voltage=24.0), ts=6002.0)
    assert [a.code for a in alarms] == ["battery_low_voltage", "battery_low_voltage_cleared"]
    assert svc.is_low_voltage is False


def test_low_voltage_threshold_follows_config(bus, tmp_path, alarms):
    svc = _svc(bus, tmp_path, **{"telemetry.battery.low_voltage_v": 18.0})
    svc.feed(_binary(voltage=20.0), ts=6100.0)
    assert alarms == []
    assert svc.is_low_voltage is False


def test_estop_alarm_is_level_crit(bus, tmp_path, alarms):
    svc = _svc(bus, tmp_path)
    svc.feed(_binary(status=3), ts=6200.0)
    assert alarms[0].code == "robot_estop"
    assert alarms[0].level == AlarmLevel.CRIT
    svc.feed(_binary(status=1), ts=6201.0)
    assert [a.code for a in alarms] == ["robot_estop", "robot_estop_released"]


# ------------------------------------------------------------------ 里程与落盘
def test_distance_integrates_trapezoid(bus, tmp_path):
    svc = _svc(bus, tmp_path)
    svc.feed(_binary(speed=1.0), ts=7000.0)
    svc.feed(_binary(speed=2.0), ts=7001.0)
    assert svc.distance_m == pytest.approx(1.5)
    assert svc.latest().distance == pytest.approx(1.5)
    svc.feed(_binary(speed=5.0), ts=7100.0)
    assert svc.distance_m == pytest.approx(1.5), "间隔超过 5s 视为断流，不补里程"


def test_flush_csv_writes_header_then_rows(bus, tmp_path):
    svc = _svc(bus, tmp_path)
    svc.feed(_binary(), ts=8000.0)
    svc.feed(_binary(voltage=23.0), ts=8001.0)
    assert svc.flush_csv() == 2
    assert svc.flush_csv() == 0, "冲刷后缓冲清空"
    files = list((tmp_path / "data").glob("telemetry_*.csv"))
    assert len(files) == 1
    raw = files[0].read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "Excel 友好必须带 BOM"
    with files[0].open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == CSV_HEADER
    assert len(rows) == 3
    assert rows[1][2] == "24.61"
    assert rows[2][2] == "23.00"


def test_stop_flushes_pending_rows(bus, tmp_path):
    svc = _svc(bus, tmp_path)
    svc.start()
    svc.feed(_binary(), ts=9000.0)
    svc.stop()
    files = list((tmp_path / "data").glob("telemetry_*.csv"))
    assert files and len(files[0].read_text(encoding="utf-8-sig").splitlines()) == 2


def test_log_csv_disabled_skips_writing(bus, tmp_path):
    svc = _svc(bus, tmp_path, **{"telemetry.log_csv": False})
    svc.feed(_binary(), ts=9100.0)
    assert svc.flush_csv() == 0
    assert not (tmp_path / "data").exists()
    assert svc.summary()["csv"] == ""


# ------------------------------------------------------------------ mock 源
def test_mock_step_produces_valid_samples(bus, tmp_path):
    cfg = _cfg(tmp_path, **{"app.mock": True, "telemetry.mock.enabled": "auto"})
    svc = TelemetryService(None, cfg, bus=bus)
    assert svc._mock_on is True, "auto 应跟随 app.mock"
    for _ in range(3):
        svc._mock_step()
    assert svc.parsed == 3
    sample = svc.latest()
    assert sample is not None
    assert 20.0 < sample.voltage < 26.0
    assert sample.gps_valid is True


def test_mock_can_be_forced_off(bus, tmp_path):
    cfg = _cfg(tmp_path, **{"app.mock": True, "telemetry.mock.enabled": "false"})
    assert TelemetryService(None, cfg, bus=bus)._mock_on is False


# ------------------------------------------------------------------ 汇总
def test_summary_shape(bus, tmp_path):
    svc = _svc(bus, tmp_path)
    svc.feed(_binary(), ts=9500.0)
    st = svc.summary()
    assert set(st) >= {
        "enabled", "running", "protocol", "mock", "parsed", "published",
        "history", "stale", "age_s", "low_voltage", "estop", "distance_m", "csv",
    }
    assert st["protocol"] == "binary"
    assert st["parsed"] == 1
    assert st["running"] is False


# ------------------------------------------------------------------ Wheeltec 通路
def _wheeltec(**kw) -> bytes:
    return encode_wheeltec_frame(**kw)


def test_feed_wheeltec_fills_motion_fields(bus, tmp_path):
    svc = _svc(bus, tmp_path, **{"telemetry.frame.protocol": "wheeltec"})
    sample = svc.feed(
        _wheeltec(vx=0.155, wz=-0.033, az=9.8, gx=0.002, voltage=23.431),
        ts=9600.0,
    )[0]
    assert sample.flag_stop == 0
    assert sample.vx == pytest.approx(0.155)
    assert sample.vz == pytest.approx(-0.033)
    assert sample.az == pytest.approx(9.8, abs=0.01)
    assert sample.speed == pytest.approx(0.155), "平面合成速度直接喂里程积分"
    assert sample.voltage == pytest.approx(23.431)
    assert svc.distance_m == pytest.approx(0.0), "首帧无前帧可积分"


def test_estop_uses_flag_stop_on_wheeltec(bus, tmp_path, alarms):
    svc = _svc(bus, tmp_path, **{"telemetry.frame.protocol": "wheeltec"})
    svc.feed(_wheeltec(flag_stop=1, voltage=24.0), ts=9700.0)
    assert alarms[0].code == "robot_estop"
    assert alarms[0].level == AlarmLevel.CRIT
    assert svc.summary()["estop"] is True
    svc.feed(_wheeltec(flag_stop=0, voltage=24.0), ts=9701.0)
    assert [a.code for a in alarms] == ["robot_estop", "robot_estop_released"]
    assert svc.summary()["estop"] is False


def test_wheeltec_csv_row_marks_enable_disable(bus, tmp_path):
    svc = _svc(bus, tmp_path, **{"telemetry.frame.protocol": "wheeltec"})
    svc.feed(_wheeltec(flag_stop=0, voltage=24.0), ts=9800.0)
    svc.feed(_wheeltec(flag_stop=1, voltage=24.0), ts=9801.0)
    assert svc.flush_csv() == 2
    files = list((tmp_path / "data").glob("telemetry_*.csv"))
    with files[0].open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    assert [r[1] for r in rows[1:]] == ["使能", "失能"]


def test_mock_step_emits_wheeltec_frames(bus, tmp_path):
    cfg = _cfg(
        tmp_path,
        **{
            "app.mock": True,
            "telemetry.mock.enabled": "auto",
            "telemetry.frame.protocol": "wheeltec",
        },
    )
    svc = TelemetryService(None, cfg, bus=bus)
    for _ in range(3):
        svc._mock_step()
    assert svc.parsed == 3
    sample = svc.latest()
    assert sample.flag_stop == 0
    assert sample.voltage == pytest.approx(24.0, abs=2.0)
    assert sample.az == pytest.approx(9.8, abs=0.01), "量化往返误差 1/1672"
