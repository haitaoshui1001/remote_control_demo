"""落盘服务 RecordService 的单元测试：文件全部落在 tmp_path，不碰工程 data/ 目录。

RecordService 的形状由两条契约决定：
（1）总线回调只做"格式化 + 入队"，真正落盘由 flush 批量完成——所以测试先
publish 再手动 flush，并断言 flush 返回的行数与文件内容一致；
（2）CSV 只收 LinkSnapshot，文本流水按 sink 分类型路由，类型不匹配一律静默
丢弃——路由错一个 sink 就会把 AT 记录写进透传流水，测试专门盯这条。
刷盘节拍用 3600 秒间隔固定住，避免后台线程和手动 flush 抢队列。
"""

from __future__ import annotations

import csv
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hmi.core.config import load_config
from hmi.core.events import EventBus, Events
from hmi.core.models import (
    AlarmEvent,
    AtExchange,
    ByteRecord,
    CommandResult,
    LinkSnapshot,
)
from hmi.services.record import CSV_FIELDS, SINK_TOPICS, RecordService


def _cfg(tmp_path: Path, **kw):
    base = {
        "app.mock": True,
        "record.video_dir": str(tmp_path / "video"),
        "record.byte_dir": str(tmp_path / "byte"),
        "record.log_dir": str(tmp_path / "log"),
        "record.snapshot_dir": str(tmp_path / "snap"),
    }
    base.update(kw)
    return load_config(None, **base)


@pytest.fixture()
def bus() -> EventBus:
    return EventBus()


@pytest.fixture()
def svc(bus, tmp_path: Path) -> RecordService:
    s = RecordService(_cfg(tmp_path), bus=bus, flush_interval_s=3600.0)
    assert s.start() is True
    yield s
    s.stop()


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _sink_path(svc, name: str) -> str:
    # paths() 的真实键是句柄名：文本流水带 _path 后缀，CSV 与视频不带
    return svc.paths()[f"{name}_path"]


# ------------------------------------------------------------------ 起停与文件
def test_start_creates_sinks_and_csv_header(svc):
    assert svc.started is True
    assert set(svc.paths()) == {"bytes_path", "at_path", "events_path", "csv"}
    for key, p in svc.paths().items():
        assert Path(p).exists(), key
    first = _read(svc.paths()["csv"]).splitlines()[0]
    assert first == ",".join(CSV_FIELDS)


def test_start_is_idempotent(bus, tmp_path: Path):
    s = RecordService(_cfg(tmp_path), bus=bus, flush_interval_s=3600.0)
    try:
        assert s.start() is True
        before = dict(s.paths())
        assert s.start() is True
        assert s.paths() == before
    finally:
        s.stop()


def test_sink_topics_shape(svc):
    assert set(SINK_TOPICS) == {"bytes", "at", "events"}
    assert SINK_TOPICS["at"] == (Events.AT_EXCHANGE, Events.CMD_RESULT)
    assert set(svc.summary()["pending"]) == {"bytes", "at", "events", "csv"}


def test_start_fails_on_unwritable_dir(bus, tmp_path: Path):
    blocker = tmp_path / "byte"
    blocker.write_text("我是文件不是目录", encoding="utf-8")
    s = RecordService(_cfg(tmp_path, **{"record.byte_dir": str(blocker)}), bus=bus)
    assert s.start() is False
    assert s.started is False
    assert s.errors == 1


# ------------------------------------------------------------------ 文本流水路由
def test_byte_record_lands_in_bytes_sink(svc):
    rec = ByteRecord(data=b"\x01\x02\x03", channel="485", kind="raw", note="手发")
    svc.bus.publish(Events.PT_BYTES, rec)
    assert svc.summary()["pending"]["bytes"] == 1
    assert svc.flush() == 1
    body = _read(_sink_path(svc, "bytes"))
    assert "485" in body and "[raw]" in body and "手发" in body and "3B" in body
    assert _read(_sink_path(svc, "at")) == ""


def test_at_and_cmd_share_at_sink(svc):
    ex = AtExchange(
        line="AT", name="MODE", kind="query", rtt_ms=12.5, channel="udp", response=["OK"]
    )
    res = CommandResult(
        template_id="t1", name="开闸", ok=False, error="超时", sent_bytes=b"\x01"
    )
    svc.bus.publish(Events.AT_EXCHANGE, ex)
    svc.bus.publish(Events.CMD_RESULT, res)
    assert svc.flush() == 2
    body = _read(_sink_path(svc, "at"))
    assert "[query:MODE]" in body and "rtt=12.5ms" in body
    assert "CMD t1" in body and "ERR" in body and "超时" in body


def test_events_sink_state_and_alarm(svc):
    svc.bus.publish(
        Events.TRANSPORT_STATE,
        {"ts": time.time(), "kind": "link", "name": "local", "state": "open", "detail": "已连接"},
    )
    svc.bus.publish(
        Events.ALARM,
        AlarmEvent(level="error", source="at", code="X1", message="boom"),
    )
    assert svc.flush() == 2
    body = _read(_sink_path(svc, "events"))
    assert "STATE link/local -> open 已连接" in body
    assert "ALARM [error] at/X1 boom" in body


def test_type_mismatch_is_dropped(svc):
    # AtExchange 走 bytes sink、ByteRecord 走 at sink，都应被 _format 拒收
    svc.bus.publish(Events.PT_BYTES, AtExchange(line="AT"))
    svc.bus.publish(Events.AT_EXCHANGE, ByteRecord(data=b"\x00"))
    assert svc.flush() == 0
    assert svc.written == 0


# ------------------------------------------------------------------ 遥测 CSV
def test_link_snapshot_goes_to_csv(svc):
    snap = LinkSnapshot(
        ts=1000.5,
        endpoint="local",
        role="sta",
        connected=True,
        rssi_dbm=-67.12345,
        bandwidth_mhz=20,
        ssid="AP1",
        errors=["a", "b"],
    )
    svc.bus.publish(Events.LINK_SNAPSHOT, snap)
    assert svc.summary()["pending"]["csv"] == 1
    assert svc.flush() == 1
    assert svc.rows == 1
    with Path(svc.paths()["csv"]).open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == list(CSV_FIELDS)
    assert len(rows) == 2 and len(rows[1]) == 20
    r = rows[1]
    assert r[0] == "1000.500" and r[2] == "local" and r[3] == "sta"
    assert r[4] == "1" and r[5] == "-67.1" and r[8] == "20"
    assert r[15] == "AP1" and r[19] == "a;b"


def test_export_csv_disabled_drops_snapshots(bus, tmp_path: Path):
    s = RecordService(
        _cfg(tmp_path, **{"record.export_csv": False}), bus=bus, flush_interval_s=3600.0
    )
    assert s.start() is True
    try:
        assert "csv" not in s.paths()
        s.bus.publish(Events.LINK_SNAPSHOT, LinkSnapshot())
        assert s.flush() == 0
        assert s.rows == 0
        assert s.summary()["pending"]["csv"] == 0
    finally:
        s.stop()


# ------------------------------------------------------------------ 队列上限
def test_queue_cap_drops_oldest(bus, tmp_path: Path):
    s = RecordService(
        _cfg(tmp_path), bus=bus, flush_interval_s=3600.0, queue_cap=100
    )
    assert s.start() is True
    try:
        for i in range(150):
            s.bus.publish(Events.PT_BYTES, ByteRecord(data=bytes([i % 256])))
        assert s.dropped == 50
        assert s.summary()["pending"]["bytes"] == 100
        assert s.flush() == 100
    finally:
        s.stop()


def test_flush_accumulates_written(svc):
    for _ in range(3):
        svc.bus.publish(Events.PT_BYTES, ByteRecord(data=b"\xaa"))
    assert svc.flush() == 3
    assert svc.written == 3
    svc.bus.publish(Events.PT_BYTES, ByteRecord(data=b"\xbb"))
    assert svc.flush() == 1
    assert svc.written == 4
    assert svc.flush() == 0  # 队列已空


# ------------------------------------------------------------------ 抓拍 / 录像守卫
def test_capture_without_video_rejected(svc):
    assert svc.capture() == {"ok": False, "detail": "未装配视频服务"}
    assert svc.snapshots == 0
    assert svc.start_video() == {"ok": False, "detail": "未装配视频服务"}
    assert svc.recording is False
    assert svc.stop_video() == {"ok": False, "detail": "当前没有录像", "path": ""}


def test_capture_with_fake_video_counts(bus, tmp_path: Path):
    video = SimpleNamespace(snapshot=lambda path=None: {"ok": True, "path": "snap.jpg"})
    s = RecordService(_cfg(tmp_path), bus=bus, video=video, flush_interval_s=3600.0)
    assert s.start() is True
    try:
        res = s.capture(label="manual")
        assert res["ok"] is True
        assert s.snapshots == 1
        assert s.flush() == 1
        assert "STATE snapshot/snap.jpg" in _read(_sink_path(s, "events"))
    finally:
        s.stop()


def test_capture_failure_not_counted(bus, tmp_path: Path):
    video = SimpleNamespace(snapshot=lambda path=None: {"ok": False, "detail": "暂无画面"})
    s = RecordService(_cfg(tmp_path), bus=bus, video=video, flush_interval_s=3600.0)
    assert s.start() is True
    try:
        assert s.capture()["ok"] is False
        assert s.snapshots == 0
        assert s.flush() == 0
    finally:
        s.stop()


# ------------------------------------------------------------------ 收尾
def test_stop_unsubscribes_and_closes(bus, tmp_path: Path):
    s = RecordService(_cfg(tmp_path), bus=bus, flush_interval_s=3600.0)
    assert s.start() is True
    s.stop()
    assert s.started is False
    s.bus.publish(Events.PT_BYTES, ByteRecord(data=b"\x01"))
    assert s.flush() == 0
    pend = s.summary()["pending"]
    assert all(v == 0 for v in pend.values())


def test_summary_shape_and_repr(svc):
    st = svc.summary()
    assert set(st) == {
        "started",
        "written",
        "rows",
        "snapshots",
        "dropped",
        "errors",
        "pending",
        "recording",
        "record_fps",
        "paths",
        "ticker",
    }
    assert st["ticker"]["name"] == "record-flush"
    assert st["record_fps"] >= 1.0
    assert "RecordService" in repr(svc)
