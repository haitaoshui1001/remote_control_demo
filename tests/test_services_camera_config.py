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
import yaml

from hmi.core.config import AppConfig, VideoCfg
from hmi.core.events import EventBus, Events
from hmi.protocol.onvif import (
    EncoderOptions,
    EncoderSettings,
    OnvifError,
    OnvifProfile,
)
from hmi.services import camera_config as cc
from hmi.services.camera_config import (
    CameraConfigService,
    CameraParams,
    CameraProbe,
    _probe_rtsp,
    compose_rtsp_url,
)


class FakeClient:
    """最小 ONVIF 客户端替身：按脚本返回，并记录 set 调用。"""

    def __init__(
        self,
        *,
        profiles: list[OnvifProfile] | None = None,
        current: EncoderSettings | None = None,
        options: EncoderOptions | None = None,
        set_error: str = "",
        info: dict | None = None,
        info_error: str = "",
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
        self.info = info if info is not None else {
            "manufacturer": "TP-LINK", "model": "TL-IPC341P-W2.8",
            "firmware": "1.0.0", "serial": "SN-0001",
        }
        self.info_error = info_error

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

    def get_device_information(self) -> dict:
        if self.info_error:
            raise OnvifError(self.info_error)
        return self.info


class FakeVideo:
    def __init__(self, *, fail: bool = False) -> None:
        self.restart_count = 0
        self.fail = fail
        self.stream_calls: list[tuple[int, bool]] = []
        self.running = True
        self.stream_return = {"ok": True, "detail": "已切换", "url": ""}

    def restart(self) -> None:
        self.restart_count += 1
        if self.fail:
            raise RuntimeError("取流重启炸了")

    def set_stream(self, stream_index: int, *, persist: bool = True) -> dict:
        self.stream_calls.append((int(stream_index), bool(persist)))
        return dict(self.stream_return)


_EXPAT_MSG = "No module named expat; use SimpleXMLTreeBuilder instead"


class ExplodingClient(FakeClient):
    """冻结包缺 XML 解析器的形态：ONVIF 调用抛非 OnvifError 的 ImportError。"""

    def get_profiles(self) -> list[OnvifProfile]:
        raise ImportError(_EXPAT_MSG)

    def get_encoder_options(self, token: str) -> EncoderOptions:
        raise ImportError(_EXPAT_MSG)


def _service(
    monkeypatch, fake: FakeClient, *, video=None, cfg=None, bus=None
) -> CameraConfigService:
    cfg = cfg or AppConfig()
    svc = CameraConfigService(cfg, bus=bus or EventBus(), video=video)
    monkeypatch.setattr(svc, "_client", lambda v=None: fake)
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


def test_load_captures_non_onvif_error(monkeypatch):
    """ImportError 之类的非协议异常也要归一为 ok=False，不许抛穿。"""
    svc = _service(monkeypatch, ExplodingClient())
    res = svc.load(0)
    assert not res.ok and res.action == "load" and res.stream_index == 0
    assert "ImportError" in res.error and "expat" in res.error


# ---------------------------------------------------------------- apply
def test_apply_captures_non_onvif_error(monkeypatch):
    fake = ExplodingClient()
    video = FakeVideo()
    svc = _service(monkeypatch, fake, video=video)
    res = svc.apply(EncoderSettings(encoder_token="ENC0"))
    assert not res.ok and res.action == "apply"
    assert "ImportError" in res.error
    assert video.restart_count == 0, "写入没成就不能重启取流"


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


def test_action_crash_still_publishes(monkeypatch):
    """工作线程里任何异常都必须回总线，否则界面永久停在"读取中"。"""
    bus = EventBus()
    got: list[CameraParams] = []
    done = threading.Event()

    def handler(topic, payload):
        got.append(payload)
        done.set()

    def load(idx: int) -> CameraParams:
        raise ImportError(_EXPAT_MSG)

    bus.subscribe(Events.CAMERA_CFG, handler)
    svc = _service(monkeypatch, FakeClient(), bus=bus)
    monkeypatch.setattr(svc, "load", load)
    assert svc.load_async(0) is True
    assert done.wait(3.0), "炸了也得发结果"
    assert not got[0].ok and got[0].action == "load"
    assert "ImportError" in got[0].error


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


# ---------------------------------------------------------------- 地址拼装
def test_compose_rtsp_url_escapes_credentials():
    v = VideoCfg(
        camera_ip="192.168.1.60", rtsp_port=554, user="ad@min",
        password="p:ss1234", stream_paths=["stream1", "stream2"], stream_index=1,
    )
    assert compose_rtsp_url(v) == "rtsp://ad%40min:p%3Ass1234@192.168.1.60:554/stream2"


def test_compose_rtsp_url_needs_ip_and_paths():
    assert compose_rtsp_url(VideoCfg(camera_ip="", stream_paths=["s1"])) == ""
    assert compose_rtsp_url(VideoCfg(camera_ip="1.2.3.4", stream_paths=[])) == ""


def test_compose_rtsp_url_out_of_range_takes_last_tier():
    v = VideoCfg(camera_ip="1.2.3.4", stream_paths=["main", "sub"])
    assert compose_rtsp_url(v, 9) == "rtsp://1.2.3.4:554/sub"


# ---------------------------------------------------------------- RTSP 明文探测
class _Sock:
    """按脚本回包的假 socket：每次 recv 吐一整段应答。"""

    def __init__(self, replies) -> None:
        self.replies = list(replies)
        self.sent: list[bytes] = []
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, _n: int) -> bytes:
        if not self.replies:
            return b""
        return self.replies.pop(0)

    def close(self) -> None:
        self.closed = True


def _sdp_reply() -> bytes:
    body = (
        "v=0\r\nm=video 0 RTP/AVP 96\r\n"
        "a=rtpmap:96 H265/90000\r\nx-dimension: 1920*1080@#25\r\n"
    ).encode()
    return (
        b"RTSP/1.0 200 OK\r\nCSeq: 2\r\nContent-Type: application/sdp\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )


def _patch_sock(monkeypatch, replies):
    sock = _Sock(replies)
    monkeypatch.setattr(cc.socket, "create_connection", lambda *a, **k: sock)
    return sock


def test_probe_rtsp_ok_reports_resolution(monkeypatch):
    _patch_sock(monkeypatch, [b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n", _sdp_reply()])
    ok, detail = _probe_rtsp("rtsp://admin:pwd@192.168.1.60:554/stream1")
    assert ok and "1920x1080" in detail and "25fps" in detail


def test_probe_rtsp_credentials_not_in_request_line(monkeypatch):
    sock = _patch_sock(monkeypatch, [b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n", _sdp_reply()])
    _probe_rtsp("rtsp://admin:pwd@192.168.1.60:554/stream1")
    assert b"pwd" not in sock.sent[0], "请求行不能带密码"
    assert b"Authorization: Basic" in sock.sent[0], "凭据要走 Basic 头"


def test_probe_rtsp_connection_refused_points_at_ip_port(monkeypatch):
    def boom(*a, **k):
        raise ConnectionRefusedError("10061 拒绝")

    monkeypatch.setattr(cc.socket, "create_connection", boom)
    ok, detail = _probe_rtsp("rtsp://admin@192.168.1.99:554/stream1")
    assert not ok and "RTSP 端口连不上" in detail and "192.168.1.99:554" in detail


def test_probe_rtsp_401_blames_password(monkeypatch):
    _patch_sock(monkeypatch, [b"RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n\r\n"])
    ok, detail = _probe_rtsp("rtsp://admin:bad@192.168.1.60:554/stream1")
    assert not ok and "账号或密码不对" in detail


def test_probe_rtsp_404_blames_stream_path(monkeypatch):
    _patch_sock(monkeypatch, [
        b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n",
        b"RTSP/1.0 404 Not Found\r\nCSeq: 2\r\n\r\n",
    ])
    ok, detail = _probe_rtsp("rtsp://admin:pwd@192.168.1.60:554/h264")
    assert not ok and "推流路径不存在" in detail


def test_probe_rtsp_silent_port(monkeypatch):
    _patch_sock(monkeypatch, [b""])
    ok, detail = _probe_rtsp("rtsp://192.168.1.60:80/stream1")
    assert not ok and "没应答" in detail


def test_probe_rtsp_rejects_malformed_url():
    ok, detail = _probe_rtsp("http://192.168.1.60/stream1")
    assert not ok and "地址格式不对" in detail


# ---------------------------------------------------------------- probe
def test_probe_judges_rtsp_and_onvif_separately(monkeypatch):
    fake = FakeClient()
    svc = _service(monkeypatch, fake)
    monkeypatch.setattr(cc, "_probe_rtsp", lambda url, **k: (False, "推流路径不存在"))
    res = svc.probe()
    assert not res.ok and res.rtsp_detail == "推流路径不存在"
    assert res.onvif_ok and "TP-LINK" in res.onvif_detail and "TL-IPC341P" in res.onvif_detail


def test_probe_uses_snapshot_not_saved_config(monkeypatch):
    seen: list[str] = []
    svc = _service(monkeypatch, FakeClient())
    monkeypatch.setattr(
        cc, "_probe_rtsp",
        lambda url, **k: seen.append(url) or (True, "DESCRIBE 正常"),
    )
    snap = VideoCfg(camera_ip="10.0.0.9", stream_paths=["live"])
    res = svc.probe(snap, 0)
    assert res.ok and seen == ["rtsp://10.0.0.9:554/live"]
    assert res.rtsp_url == "rtsp://10.0.0.9:554/live"


def test_probe_without_ip_skips_onvif(monkeypatch):
    svc = _service(monkeypatch, FakeClient())
    monkeypatch.setattr(cc, "_probe_rtsp", lambda url, **k: (True, "DESCRIBE 正常"))
    snap = VideoCfg(camera_ip="", url="rtsp://192.168.1.60/stream1")
    res = svc.probe(snap)
    assert res.ok and res.onvif_ok is False and "跳过 ONVIF" in res.onvif_detail


def test_probe_onvif_failure_does_not_kill_rtsp(monkeypatch):
    svc = _service(monkeypatch, FakeClient(info_error="401 未授权"))
    monkeypatch.setattr(cc, "_probe_rtsp", lambda url, **k: (True, "DESCRIBE 正常"))
    res = svc.probe()
    assert res.ok and not res.onvif_ok and "ONVIF 不可用" in res.onvif_detail


def test_probe_empty_url_is_clean_error(monkeypatch):
    cfg = AppConfig()
    cfg.video.camera_ip = ""
    cfg.video.url = ""
    cfg.video.stream_paths = []
    svc = _service(monkeypatch, FakeClient(), cfg=cfg)
    res = svc.probe()
    assert not res.ok and "取流地址是空的" in res.error


def test_probe_async_publishes_probe_topic(monkeypatch):
    bus = EventBus()
    got: list[CameraProbe] = []
    cfg_payloads: list[object] = []
    done = threading.Event()
    bus.subscribe(Events.CAMERA_PROBE, lambda topic, payload: (got.append(payload), done.set()))
    bus.subscribe(Events.CAMERA_CFG, lambda topic, payload: cfg_payloads.append(payload))
    svc = _service(monkeypatch, FakeClient(), bus=bus)
    monkeypatch.setattr(cc, "_probe_rtsp", lambda url, **k: (True, "DESCRIBE 正常"))
    assert svc.probe_async() is True
    assert done.wait(3.0), "自检结果应发 CAMERA_PROBE，不是 CAMERA_CFG"
    assert got[0].ok and got[0].action == "probe"
    assert not cfg_payloads, "不得污染摄像头参数对话框"


# ---------------------------------------------------------------- 保存连接参数
def _writable_cfg(tmp_path) -> AppConfig:
    cfg = AppConfig()
    cfg.meta.path = str(tmp_path / "app.yaml")
    return cfg


def test_apply_connection_writes_whitelist_and_reconnects(monkeypatch, tmp_path):
    cfg = _writable_cfg(tmp_path)
    cfg.video.buffer_ms = 300
    svc = _service(monkeypatch, FakeClient(), cfg=cfg)
    video = FakeVideo()
    svc.video = video
    snap = VideoCfg(
        camera_ip="192.168.1.88", rtsp_port=8554, onvif_port=2000,
        user="admin", password="pw123", stream_paths=["stream1", "stream2"],
        stream_index=1, transport="udp", url="",
    )
    res = svc.apply_connection(snap)
    assert res["ok"] and res["reconnected"]
    v = cfg.video
    assert (v.camera_ip, v.rtsp_port, v.onvif_port, v.password) == (
        "192.168.1.88", 8554, 2000, "pw123",
    )
    assert v.stream_paths == ["stream1", "stream2"] and v.transport == "udp"
    assert v.buffer_ms == 300, "渲染参数不能跟着连接参数一起被冲掉"
    assert video.stream_calls == [(1, False)], "生效要走 set_stream，且别再存一次盘"
    assert res["url"] == "rtsp://admin:pw123@192.168.1.88:8554/stream2"

    saved = yaml.safe_load((tmp_path / "app.yaml").read_text(encoding="utf-8"))
    assert saved["video"]["camera_ip"] == "192.168.1.88"
    assert saved["video"]["stream_paths"] == ["stream1", "stream2"]


def test_apply_connection_rejects_empty_target(monkeypatch, tmp_path):
    saved: list = []
    svc = _service(monkeypatch, FakeClient(), cfg=_writable_cfg(tmp_path))
    monkeypatch.setattr(svc.cfg, "save", lambda *a, **k: saved.append(1))
    video = FakeVideo()
    svc.video = video
    res = svc.apply_connection(VideoCfg(camera_ip="", url=""))
    assert not res["ok"] and "至少填一项" in res["detail"]
    assert not saved and video.stream_calls == []


def test_apply_connection_survives_save_failure(monkeypatch, tmp_path):
    """配置文件被占用（用户在编辑器里开着）时，内存里照样生效并说清楚。"""
    svc = _service(monkeypatch, FakeClient(), cfg=_writable_cfg(tmp_path))
    monkeypatch.setattr(svc.cfg, "save", lambda *a, **k: (_ for _ in ()).throw(OSError("busy")))
    svc.video = FakeVideo()
    res = svc.apply_connection(VideoCfg(camera_ip="10.1.1.1", stream_paths=["s1"]))
    assert res["ok"] and "写盘失败" in res["detail"]
    assert svc.cfg.video.camera_ip == "10.1.1.1"


def test_apply_connection_clamps_stream_index(monkeypatch, tmp_path):
    svc = _service(monkeypatch, FakeClient(), cfg=_writable_cfg(tmp_path))
    video = FakeVideo()
    svc.video = video
    res = svc.apply_connection(VideoCfg(camera_ip="10.1.1.1", stream_index=7))
    assert svc.cfg.video.stream_index == 1
    assert video.stream_calls == [(1, False)]


def test_apply_connection_without_video_service(monkeypatch, tmp_path):
    svc = _service(monkeypatch, FakeClient(), cfg=_writable_cfg(tmp_path))
    res = svc.apply_connection(VideoCfg(camera_ip="10.1.1.1", stream_paths=["s1"]))
    assert res["ok"] and "reconnected" not in res


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
