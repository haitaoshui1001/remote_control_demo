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


def test_stream_paths_swaps_trailing_path_on_manual_url():
    """手填地址 + stream_paths：末段等于第一档时按码流序号换路径（TP-Link 实测）。"""
    v = VideoCfg(
        url="rtsp://admin:pw123456@192.168.1.60:554/stream1",
        stream_index=0,
        stream_paths=["stream1", "stream2"],
    )
    assert v.resolved_url(0) == "rtsp://admin:pw123456@192.168.1.60:554/stream1"
    assert v.resolved_url(1) == "rtsp://admin:pw123456@192.168.1.60:554/stream2"
    assert v.resolved_url(9) == "rtsp://admin:pw123456@192.168.1.60:554/stream2", "越界取最后一档"


def test_stream_paths_placeholder_wins_over_tail_match():
    v = VideoCfg(
        url="rtsp://10.0.0.9/{path}?token=abc",
        stream_paths=["/media/video1/", "media/video2"],
    )
    assert v.resolved_url(0) == "rtsp://10.0.0.9/media/video1?token=abc"
    assert v.resolved_url(1) == "rtsp://10.0.0.9/media/video2?token=abc"


def test_stream_paths_empty_keeps_legacy_behaviour():
    """没配 stream_paths 就不能改变既有结论：地址整条不动、模板照常拼装。"""
    v = VideoCfg(url="rtsp://10.0.0.9/cam0/1", stream_paths=[])
    assert v.resolved_url(1) == "rtsp://10.0.0.9/cam0/1"
    assert v._stream_path(0) == ""
    plain = VideoCfg(url="", camera_ip="192.168.1.64")
    assert "{path}" not in plain.resolved_url(1)
    assert plain.resolved_url(1).endswith("stream=1.sdp?")


def test_template_with_path_placeholder_tolerates_missing_paths():
    """模板里写 {path} 时，stream_paths 留空只该得到空路径，不该 KeyError。"""
    v = VideoCfg(url="", url_template="rtsp://{ip}/{path}", stream_paths=[])
    assert v.resolved_url(0) == "rtsp://192.168.1.10/"
    v.stream_paths = ["live", "sub"]
    assert v.resolved_url(1) == "rtsp://192.168.1.10/sub"


def test_stream_paths_present_composes_direct_url_not_vendor_template():
    """填了码流路径就按 IP/端口/账号/路径直拼：自检和取流必须用同一条地址。

    连接对话框里用户填的是"IP + 端口 + 账号 + stream1/stream2"，若这里还回
    厂商模板，【测试】测通的和【保存】后播放器拉的就是两条地址。
    """
    v = VideoCfg(
        url="", camera_ip="192.168.1.60", user="admin", password="pw123456",
        stream_paths=["stream1", "stream2"], stream_index=0,
    )
    assert v.resolved_url(0) == "rtsp://admin:pw123456@192.168.1.60:554/stream1"
    assert v.resolved_url(1) == "rtsp://admin:pw123456@192.168.1.60:554/stream2"
    assert v.resolved_url(9) == "rtsp://admin:pw123456@192.168.1.60:554/stream2"


def test_composed_url_drops_empty_password():
    """密码没填就别写 ``admin:@ip``：那只会让设备回 401，看不出是密码缺失。"""
    v = VideoCfg(
        url="", camera_ip="1.2.3.4", rtsp_port=8554, user="admin", password="",
        stream_paths=["live"],
    )
    assert v.resolved_url(0) == "rtsp://1.2.3.4:8554/live"
    v.password = "p@ss:1"
    assert v.resolved_url(0) == "rtsp://admin:p%40ss%3A1@1.2.3.4:8554/live"


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
