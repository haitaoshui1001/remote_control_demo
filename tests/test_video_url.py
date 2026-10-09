"""取流地址解析：显式 url 优先、留空走模板拼装，服务层与传输层必须同一结论。

回归锁定：实机配置只填 camera_ip、url 留空时，点"启动视频"曾报
"视频地址为空"——因为 RtspVideoSource 只读裸 cfg.url、没用 url_template。
"""

from __future__ import annotations

from fractions import Fraction

from hmi.core.config import AppConfig, VideoCfg
from hmi.transport.rtsp import RtspVideoSource, _frame_time


class _NoPtsTimeFrame:
    pts = 90000
    time_base = Fraction(1, 90000)

    @property
    def pts_time(self) -> float:
        raise AttributeError("'av.video.frame.VideoFrame' object has no attribute 'pts_time'")


class _PtsTimeFrame:
    pts = 42
    time_base = Fraction(1, 1000)
    pts_time = 0.042


class _BareFrame:
    pass


def test_frame_time_falls_back_to_pts_times_time_base():
    assert _frame_time(_NoPtsTimeFrame(), "pts_time") == 1.0
    assert _frame_time(_PtsTimeFrame(), "pts_time") == 0.042
    assert _frame_time(_NoPtsTimeFrame(), "dts_time") is None
    assert _frame_time(_BareFrame(), "pts_time") is None


def test_template_used_when_url_empty():
    v = VideoCfg(url="", camera_ip="192.168.1.64", user="admin", password="pw")
    url = v.resolved_url()
    assert url.startswith("rtsp://192.168.1.64:554/")
    assert "user=admin" in url
    assert "password=pw" in url
    assert f"stream={v.stream_index}" in url


def test_explicit_url_wins_and_stream_is_replaced():
    v = VideoCfg(url="rtsp://10.0.0.9/live?stream=1", stream_index=0)
    assert v.resolved_url() == "rtsp://10.0.0.9/live?stream=0"
    assert v.resolved_url(1) == "rtsp://10.0.0.9/live?stream=1"


def test_explicit_url_without_stream_marker_is_untouched():
    v = VideoCfg(url="rtsp://10.0.0.9/cam0/1")
    assert v.resolved_url(0) == "rtsp://10.0.0.9/cam0/1"


def test_appconfig_rtsp_url_delegates_to_video_cfg():
    cfg = AppConfig()
    cfg.video.camera_ip = "192.168.1.64"
    cfg.video.url = ""
    assert cfg.rtsp_url(0) == cfg.video.resolved_url(0)
    assert "192.168.1.64" in cfg.rtsp_url(0)


def test_rtsp_source_construction_resolves_template():
    src = RtspVideoSource(VideoCfg(url="", camera_ip="192.168.1.64"), name="rtsp")
    assert src.url.startswith("rtsp://192.168.1.64:554/")


def test_rtsp_source_start_still_raises_when_truly_empty():
    v = VideoCfg(url="", camera_ip="", url_template="")
    src = RtspVideoSource(v, name="rtsp")
    src.url = ""
    try:
        src.start()
    except RuntimeError as exc:
        assert "camera_ip" in str(exc) or "缺少 PyAV" in str(exc)
    else:
        raise AssertionError("空地址必须拒绝启动")


def test_options_udp_switches_transport_and_raises_socket_buffer():
    src = RtspVideoSource(VideoCfg(url="rtsp://10.0.0.9/live", transport="udp"), name="rtsp")
    opts = src._options()
    assert opts["rtsp_transport"] == "udp"
    assert opts["buffer_size"] == "4194304", "UDP 收流要放大 socket 缓冲，防内核丢弃"


def test_options_tcp_leaves_buffer_size_unset():
    src = RtspVideoSource(VideoCfg(url="rtsp://10.0.0.9/live"), name="rtsp")
    opts = src._options()
    assert opts["rtsp_transport"] == "tcp"
    assert "buffer_size" not in opts, "TCP 走内核流控，不该设这个 UDP 专用的键"
