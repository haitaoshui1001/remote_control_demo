"""摄像头配置服务层测试：桩掉 ONVIF 客户端，盯四件事。

1. load 的边界：越界序号、camera_ip 为空、无 Profile、无编码引用，
   全部归一为 ok=False+中文 error，不许抛穿。
2. apply 的值域纪律：先问 Options 再 clamped，写进设备的是夹过的值；
   成功后重启取流，失败绝不 restart。
3. 异步入口：结果发布到 Events.CAMERA_CFG；同一时刻只允许一个在途。
4. summary 给状态栏用的三键。
"""

from __future__ import annotations

import threading
import time

import pytest

from hmi.core.config import AppConfig
from hmi.core.events import EventBus, Events
from hmi.protocol.onvif import (
    EncoderOptions,
    EncoderSettings,
    OnvifError,
    OnvifProfile,
)
from hmi.services.camera_config import CameraConfigService, CameraParams


class FakeClient:
    """最小 ONVIF 客户端替身：按脚本返回，并记录 set 调用。"""

    def __init__(
        self,
        *,
        profiles: list[OnvifProfile] | None = None,
        current: EncoderSettings | None = None,
        options: EncoderOptions | None = None,
        set_error: str = "",
    ) -> None:
        self.auth_mode = "wsse"
        self.profiles = profiles if profiles is not None else [
            OnvifProfile(token="PROF0", name="Chan01_Main", encoder_token="ENC0"),
            OnvifProfile(token="PROF1", name="Chan01_Sub", encoder_token="ENC1"),
        ]
        self.current = current or EncoderSettings(
            encoder_token="ENC0", name="MainStream", width=1920, height=1080,
            frame_rate=25, bitrate_kbps=2048, rate_mode="C", gov_length=20,
        )
        self.options = options or EncoderOptions(
            encodings=["H264"],
            resolutions=[(640, 360), (1280, 720), (1920, 1080)],
            fps_range=(1, 25),
            bitrate_range=(64, 4096),
            source="device",
        )
        self.set_error = set_error
        self.set_calls: list[EncoderSettings] = []
        self.options_tokens: list[str] = []

    def get_profiles(self) -> list[OnvifProfile]:
        return self.profiles

    def get_encoder_configuration(self, token: str) -> EncoderSettings:
        return self.current

    def get_encoder_options(self, token: str) -> EncoderOptions:
        self.options_tokens.append(token)
        return self.options

    def set_encoder_configuration(self, st: EncoderSettings) -> str:
        if self.set_error:
            raise OnvifError(self.set_error)
        self.set_calls.append(st)
        return st.encoder_token


class FakeVideo:
    def __init__(self, *, fail: bool = False) -> None:
        self.restart_count = 0
        self.fail = fail

    def restart(self) -> None:
        self.restart_count += 1
        if self.fail:
            raise RuntimeError("取流重启炸了")


def _service(
    monkeypatch, fake: FakeClient, *, video=None, cfg=None, bus=None
) -> CameraConfigService:
    cfg = cfg or AppConfig()
    svc = CameraConfigService(cfg, bus=bus or EventBus(), video=video)
    monkeypatch.setattr(svc, "_client", lambda: fake)
    return svc


def _wait_until(predicate, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------- load
def test_load_ok_returns_current_and_options(monkeypatch):
    fake = FakeClient()
    svc = _service(monkeypatch, fake)
    res = svc.load(1)
    assert res.ok and res.action == "load" and res.stream_index == 1
    assert res.auth_mode == "wsse"
    assert res.profile_name == "Chan01_Sub"
    assert res.current is fake.current and res.options is fake.options


def test_load_index_out_of_range(monkeypatch):
    svc = _service(monkeypatch, FakeClient())
    res = svc.load(5)
    assert not res.ok and "越界" in res.error and res.stream_index == 5


def test_load_empty_ip_is_clean_error():
    cfg = AppConfig()
    cfg.video.camera_ip = ""
    svc = CameraConfigService(cfg, bus=EventBus())
    res = svc.load(0)
    assert not res.ok and "camera_ip 为空" in res.error


def test_load_no_profiles(monkeypatch):
    svc = _service(monkeypatch, FakeClient(profiles=[]))
    res = svc.load(0)
    assert not res.ok and "没有返回任何 Profile" in res.error


def test_load_profile_without_encoder_token(monkeypatch):
    fake = FakeClient(
        profiles=[OnvifProfile(token="PROF0", name="NoVideo", encoder_token="")]
    )
    svc = _service(monkeypatch, fake)
    res = svc.load(0)
    assert not res.ok and "无视频编码引用" in res.error


# ---------------------------------------------------------------- apply
def test_apply_clamps_then_writes_and_restarts(monkeypatch):
    fake = FakeClient()
    video = FakeVideo()
    svc = _service(monkeypatch, fake, video=video)
    st = EncoderSettings(
        encoder_token="ENC1", width=1280, height=720,
        frame_rate=60, bitrate_kbps=9999, rate_mode="V",
    )
    res = svc.apply(st)
    assert res.ok and res.action == "apply"
    assert len(res.fixed) == 2, "帧率与码率都应被夹并说明"
    written = fake.set_calls[0]
    assert written.frame_rate == 25 and written.bitrate_kbps == 4096
    assert written.encoder_token == "ENC1"
    assert fake.options_tokens == ["ENC1"], "值域应按写入的 token 询问"
    assert video.restart_count == 1


def test_apply_error_skips_restart(monkeypatch):
    fake = FakeClient(set_error="设备拒绝写入")
    video = FakeVideo()
    svc = _service(monkeypatch, fake, video=video)
    res = svc.apply(EncoderSettings(encoder_token="ENC0", width=640, height=360))
    assert not res.ok and "拒绝" in res.error
    assert video.restart_count == 0 and not fake.set_calls


def test_apply_restart_failure_still_ok(monkeypatch):
    fake = FakeClient()
    svc = _service(monkeypatch, fake, video=FakeVideo(fail=True))
    res = svc.apply(EncoderSettings(encoder_token="ENC0", width=640, height=360))
    assert res.ok, "参数已写进设备，重启失败只该警告不该翻脸"


# ---------------------------------------------------------------- 异步
def test_load_async_publishes_on_bus(monkeypatch):
    bus = EventBus()
    got: list[CameraParams] = []
    done = threading.Event()

    def handler(topic, payload):
        got.append(payload)
        done.set()

    bus.subscribe(Events.CAMERA_CFG, handler)
    svc = _service(monkeypatch, FakeClient(), bus=bus)
    assert svc.load_async(0) is True
    assert done.wait(3.0), "工作线程应把结果发上总线"
    assert got[0].ok and got[0].action == "load"


def test_single_flight_busy(monkeypatch):
    release = threading.Event()
    svc = _service(monkeypatch, FakeClient())

    def slow_load(idx: int) -> CameraParams:
        release.wait(3.0)
        return CameraParams(ok=True, action="load", stream_index=idx)

    monkeypatch.setattr(svc, "load", slow_load)
    assert svc.load_async(0) is True
    assert svc.busy is True
    assert svc.load_async(1) is False, "在途期间第二次 spawn 必须被拒"
    release.set()
    assert _wait_until(lambda: not svc.busy), "线程结束后 busy 应回落"


def test_summary_keys(monkeypatch):
    cfg = AppConfig()
    cfg.video.onvif_port = 8080
    svc = _service(monkeypatch, FakeClient(), cfg=cfg)
    s = svc.summary()
    assert s == {"ip": cfg.video.camera_ip, "port": 8080, "busy": False}


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
