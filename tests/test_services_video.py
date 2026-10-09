"""视频服务传输层配置入口（UDP/TCP 切换）测试。

回归关注点：切换要同时命中三处——内存 cfg（源起流时现读）、
app.yaml（重启软件后保得住）、正在跑的流（重启才对新参数生效）；
漏任何一处都会变成"看着切了其实没切"或"重启就丢"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hmi.core.config import AppConfig
from hmi.mock.video import MockVideoSource
from hmi.services.video import VideoService


@pytest.fixture()
def cfg(tmp_path):
    c = AppConfig()
    c.meta.path = str(tmp_path / "app.yaml")
    return c


def _svc(cfg: AppConfig) -> VideoService:
    return VideoService(cfg, source=MockVideoSource(cfg.video, name="mockcam"))


def test_set_transport_udp_updates_cfg_and_persists(cfg):
    svc = _svc(cfg)
    res = svc.set_transport("udp")
    assert res["ok"] and res["transport"] == "udp"
    assert svc.transport == "udp" and svc.cfg.video.transport == "udp"
    text = Path(cfg.meta.path).read_text(encoding="utf-8")
    assert "transport: udp" in text, "切换必须写回 app.yaml，重启软件后才保得住"


def test_set_transport_normalizes_case_and_blanks(cfg):
    svc = _svc(cfg)
    assert svc.set_transport(" UDP ")["transport"] == "udp"


def test_set_transport_rejects_unknown_mode(cfg):
    svc = _svc(cfg)
    res = svc.set_transport("sctp")
    assert not res["ok"]
    assert res["transport"] == "tcp" and svc.transport == "tcp", "非法值必须原样不动"
    assert not Path(cfg.meta.path).exists(), "被拒绝的切换不应留下任何配置文件"


def test_persist_false_keeps_config_file_untouched(cfg):
    svc = _svc(cfg)
    res = svc.set_transport("udp", persist=False)
    assert res["ok"] and not res["saved"]
    assert svc.transport == "udp", "persist=False 只切内存，供临时试验"
    assert not Path(cfg.meta.path).exists()


def test_running_stream_restarts_on_switch(cfg):
    svc = _svc(cfg)
    try:
        assert svc.start() and svc.running
        res = svc.set_transport("udp")
        assert res["ok"] and res["restarted"], "取流中切换必须重启才对新参数生效"
        assert svc.running and svc.cfg.video.transport == "udp"
    finally:
        svc.stop()


def test_same_transport_does_not_restart(cfg):
    svc = _svc(cfg)
    try:
        assert svc.start() and svc.running
        res = svc.set_transport("tcp")
        assert res["ok"] and not res["restarted"], "没变化就别动正在跑的流"
    finally:
        svc.stop()


if __name__ == "__main__":  # pragma: no cover - 手工入口
    raise SystemExit(pytest.main([__file__, "-q"]))
