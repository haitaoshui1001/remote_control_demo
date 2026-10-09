"""装配根 Session 的单元测试：只验证"接线与起停顺序"，不验证各服务内部行为。

Session 的价值是把三条装配约束写死（link 最先、record 最后启动、停止走逆序），
并把串口被占、目录不可写这类**常态失败**收进 dict 而不是抛异常。测试因此盯四点：
装配关系（record.video 必须就是 session.video）、起停开关（构造级 vs 单次级是两个
维度）、失败收集进 detail、summary 键集。全部 mock 模式 + 独立 EventBus +
tmp_path 落盘目录，不碰全局总线也不碰工程 data/。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hmi.core.config import load_config
from hmi.core.events import EventBus, Events
from hmi.core.models import ByteRecord
from hmi.services.record import RecordService
from hmi.services.session import Session, build_session
from hmi.services.video import VideoService


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


def _sess(bus, tmp_path: Path, **kw):
    video = kw.pop("video", False)
    record = kw.pop("record", True)
    return Session(_cfg(tmp_path, **kw), bus=bus, video=video, record=record)


def _cleanup(sess: Session) -> None:
    try:
        sess.stop()
    except Exception:
        pass


# ------------------------------------------------------------------ 装配
def test_build_session_wires_all_services():
    sess = build_session(None, mock=True)
    try:
        assert sess.mock is True
        assert sess.endpoint == "local"
        assert isinstance(sess.video, VideoService)
        assert isinstance(sess.record, RecordService)
        assert sess.record.video is sess.video  # 抓拍必须共享同一个取流通道
        assert sess.started is False
    finally:
        sess.stop()


def test_build_session_service_switches():
    sess = build_session(None, mock=True, endpoint="remote", video=False, record=False)
    assert sess.video is None
    assert sess.record is None
    assert sess.endpoint == "remote"
    sess.stop()


def test_video_record_flags_are_not_config_overrides():
    # 文档契约：video/record 收下的是"装不装服务"，不能掉进 overrides 覆盖整段配置
    sess = build_session(None, mock=True, video=False, record=False)
    try:
        assert sess.cfg.video.auto_connect is True  # video=False 没把 video 段抹掉
        assert sess.cfg.record.export_csv is True  # record=False 没把 record 段抹掉
    finally:
        sess.stop()


def test_build_session_dot_path_overrides():
    sess = build_session(None, mock=True, video=False, record=False,
                         **{"record.export_csv": False})
    try:
        assert sess.cfg.record.export_csv is False
    finally:
        sess.stop()


# ------------------------------------------------------------------ 起停
def test_start_stop_lifecycle(bus, tmp_path: Path):
    sess = _sess(bus, tmp_path)
    try:
        res = sess.start()
        assert {"link", "passthrough", "video", "record", "detail", "polling", "ok"} <= set(res)
        assert res["link"] is True and res["ok"] is True
        assert res["passthrough"] is True
        assert res["record"] is True
        assert res["video"] is None  # 构造时没装视频服务
        assert isinstance(res["polling"], bool)
        assert res["detail"] == []
        assert sess.started is True
        assert any(p.endswith("_path") for p in sess.record.paths())
    finally:
        sess.stop()
    assert sess.started is False
    assert sess.link.is_open is False
    # stop 后总线事件不再入队
    bus.publish(Events.PT_BYTES, ByteRecord(data=b"\x01"))
    assert sess.record.summary()["pending"]["bytes"] == 0


def test_start_link_false_skips_open(bus, tmp_path: Path):
    sess = _sess(bus, tmp_path, record=False)
    try:
        res = sess.start(link=False)
        assert res["link"] is None
        assert res["ok"] is False
        assert sess.started is False
        assert res["passthrough"] is True  # 透传引擎不依赖通道已打开
    finally:
        _cleanup(sess)


def test_start_video_false_skips_built_video(bus, tmp_path: Path):
    sess = _sess(bus, tmp_path, video=True, record=False)
    try:
        res = sess.start(video=False)
        assert res["video"] is None  # 单次开关：装了服务也可以这次不起流
    finally:
        _cleanup(sess)


def test_record_failure_collected_not_raised(bus, tmp_path: Path):
    blocker = tmp_path / "byte"
    blocker.write_text("我是文件不是目录", encoding="utf-8")
    sess = _sess(bus, tmp_path)
    try:
        res = sess.start()
        assert res["record"] is False
        assert res["ok"] is True  # 落盘失败不影响链路本身
        assert any("落盘目录不可写" in d for d in res["detail"])
    finally:
        _cleanup(sess)


def test_context_manager_starts_and_stops(bus, tmp_path: Path):
    sess = _sess(bus, tmp_path, record=False)
    with sess as entered:
        assert entered is sess
        assert sess.started is True
    assert sess.started is False
    assert sess.link.is_open is False


# ------------------------------------------------------------------ 汇总
def test_summary_shape(bus, tmp_path: Path):
    sess = _sess(bus, tmp_path, video=False, record=False)
    st = sess.summary()
    assert set(st) == {
        "endpoint",
        "mock",
        "started",
        "link",
        "radio",
        "passthrough",
        "telemetry",
        "motion",
        "video",
        "camera",
        "record",
    }
    assert st["endpoint"] == "local"
    assert st["mock"] is True
    assert st["video"] is None and st["record"] is None
    assert isinstance(st["link"], dict)
    assert isinstance(st["telemetry"], dict)
    sess.stop()


def test_repr(bus, tmp_path: Path):
    sess = _sess(bus, tmp_path, record=False)
    try:
        assert "Session local" in repr(sess)
        assert "mock=True" in repr(sess)
    finally:
        sess.stop()
