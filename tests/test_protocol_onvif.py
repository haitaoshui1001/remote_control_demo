"""ONVIF 协议层测试：离线假服务回放应答，全程不碰真摄像头。

盯四件事：
1. WSSE 头可被设备端复算（Username/Password/Nonce/Created 四要素齐全，
   sha1(nonce+created+password) 对得上——Nonce 曾漏发，这里钉死回归）。
2. 鉴权降级：401 + Digest 挑战时自动改走 HTTP Digest，并记 auth_mode。
3. 应答解析：Profile/编码参数/值域/Set 回显，以及 SOAP Fault 转 OnvifError。
4. clamped() 值域过滤：越界帧率/码率夹回区间、分辨率取面积最接近档位。
5. 雄迈真机原文回放（mode="xm"）：media2 排在前面仍挑中 Media1、空 Configurations
   退回 Profile token、Media2 风格值域（离散帧率 + 按编码分档）全解析。
"""

from __future__ import annotations

import base64
import hashlib
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hmi.protocol.onvif import (
    EncoderOptions,
    EncoderSettings,
    OnvifClient,
    OnvifError,
    _pick_media_url,
    _wsse_header,
)

_ENVELOPE_OPEN = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    "<s:Body>"
)
_ENVELOPE_CLOSE = "</s:Body></s:Envelope>"
_NS_TT = 'xmlns:tt="http://www.onvif.org/ver10/schema"'
_NS_MEDIA = 'xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
_NS_DEV = 'xmlns:tdr="http://www.onvif.org/ver10/device/wsdl"'

DEVICE_INFO = (
    _ENVELOPE_OPEN +
    f"<tdr:GetDeviceInformationResponse {_NS_DEV}>"
    "<tdr:Manufacturer>XM</tdr:Manufacturer>"
    "<tdr:Model>IPG-85HE20PY</tdr:Model>"
    "<tdr:FirmwareVersion>5.2.1</tdr:FirmwareVersion>"
    "<tdr:SerialNumber>SN123456</tdr:SerialNumber>"
    "</tdr:GetDeviceInformationResponse>" + _ENVELOPE_CLOSE
)

GET_SERVICES = (
    _ENVELOPE_OPEN +
    f"<tdr:GetServicesResponse {_NS_DEV}>"
    "<tdr:Service>"
    "<tdr:XAddr>http://127.0.0.1:{port}/onvif/device_service</tdr:XAddr>"
    "</tdr:Service>"
    "<tdr:Service>"
    "<tdr:XAddr>http://127.0.0.1:{port}/onvif/media_service</tdr:XAddr>"
    "</tdr:Service>"
    "</tdr:GetServicesResponse>" + _ENVELOPE_CLOSE
)

PROFILES = (
    _ENVELOPE_OPEN +
    f"<trt:GetProfilesResponse {_NS_MEDIA}>"
    f'<trt:Profiles token="PROF0" {_NS_TT}>'
    "<tt:Name>Chan1_Main</tt:Name>"
    '<tt:VideoEncoderConfiguration token="ENC0">'
    "<tt:Name>main_enc</tt:Name>"
    "</tt:VideoEncoderConfiguration>"
    "</trt:Profiles>"
    f'<trt:Profiles token="PROF1" {_NS_TT}>'
    "<tt:Name>Chan1_Sub</tt:Name>"
    '<tt:VideoEncoderConfiguration token="ENC1">'
    "<tt:Name>sub_enc</tt:Name>"
    "</tt:VideoEncoderConfiguration>"
    "</trt:Profiles>"
    "</trt:GetProfilesResponse>" + _ENVELOPE_CLOSE
)

ENC_CBR = (
    _ENVELOPE_OPEN +
    f"<trt:GetVideoEncoderConfigurationResponse {_NS_MEDIA}>"
    f'<trt:Configuration token="ENC0" {_NS_TT}>'
    "<tt:Name>main_enc</tt:Name>"
    "<tt:Encoding>H264</tt:Encoding>"
    "<tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height>"
    "</tt:Resolution>"
    "<tt:Quality>4</tt:Quality>"
    "<tt:RateControl><tt:ConstantBitrateRate>2048</tt:ConstantBitrateRate>"
    "<tt:Encoding>CBR</tt:Encoding></tt:RateControl>"
    "<tt:BitrateLimit>2048</tt:BitrateLimit>"
    "<tt:FrameRateLimit>25</tt:FrameRateLimit>"
    "<tt:GovLength>20</tt:GovLength>"
    "</trt:Configuration>"
    "</trt:GetVideoEncoderConfigurationResponse>" + _ENVELOPE_CLOSE
)

ENC_VBR = (
    _ENVELOPE_OPEN +
    f"<trt:GetVideoEncoderConfigurationResponse {_NS_MEDIA}>"
    f'<trt:Configuration token="ENC1" {_NS_TT}>'
    "<tt:Name>sub_enc</tt:Name>"
    "<tt:Encoding>H265</tt:Encoding>"
    "<tt:Resolution><tt:Width>1280</tt:Width><tt:Height>720</tt:Height>"
    "</tt:Resolution>"
    "<tt:RateControl><tt:AverageBitrateRate>1500</tt:AverageBitrateRate>"
    "<tt:PeakBitrateRate>3000</tt:PeakBitrateRate></tt:RateControl>"
    "<tt:BitrateLimit>1500</tt:BitrateLimit>"
    "<tt:FrameRateLimit>15</tt:FrameRateLimit>"
    "</trt:Configuration>"
    "</trt:GetVideoEncoderConfigurationResponse>" + _ENVELOPE_CLOSE
)

OPTIONS = (
    _ENVELOPE_OPEN +
    f"<trt:GetVideoEncoderConfigurationOptionsResponse {_NS_MEDIA}>"
    f"<trt:Options {_NS_TT}>"
    "<tt:QualityRange><tt:Min>1</tt:Min><tt:Max>10</tt:Max></tt:QualityRange>"
    "<tt:H264>"
    "<tt:ResolutionsAvailable><tt:Width>640</tt:Width><tt:Height>360</tt:Height>"
    "</tt:ResolutionsAvailable>"
    "<tt:ResolutionsAvailable><tt:Width>1280</tt:Width><tt:Height>720</tt:Height>"
    "</tt:ResolutionsAvailable>"
    "<tt:ResolutionsAvailable><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height>"
    "</tt:ResolutionsAvailable>"
    "<tt:FrameRateRange><tt:Min>1</tt:Min><tt:Max>25</tt:Max></tt:FrameRateRange>"
    "</tt:H264>"
    "<tt:RateControl><tt:BitrateRange><tt:Min>64</tt:Min><tt:Max>4096</tt:Max>"
    "</tt:BitrateRange></tt:RateControl>"
    "</trt:Options>"
    "</trt:GetVideoEncoderConfigurationOptionsResponse>" + _ENVELOPE_CLOSE
)

OPTIONS_EMPTY = (
    _ENVELOPE_OPEN +
    f"<trt:GetVideoEncoderConfigurationOptionsResponse {_NS_MEDIA}/>"
    + _ENVELOPE_CLOSE
)

SET_RESP = (
    _ENVELOPE_OPEN +
    f"<trt:SetVideoEncoderConfigurationResponse {_NS_MEDIA}>"
    f'<trt:Configuration token="ENC0" {_NS_TT}>'
    "<tt:Name>main_enc</tt:Name>"
    "</trt:Configuration>"
    "</trt:SetVideoEncoderConfigurationResponse>" + _ENVELOPE_CLOSE
)

SOAP_FAULT = (
    _ENVELOPE_OPEN
    + "<s:Fault>"
    "<s:Code><s:Value>s:Sender</s:Value></s:Code>"
    "<s:Reason><s:Text xml:lang=\"en\">Invalid Configuration Token</s:Text>"
    "</s:Reason>"
    "</s:Fault>" + _ENVELOPE_CLOSE
)


def _fault_config(why: str) -> str:
    """实机改参数失败时的原文形状：HTTP 400 + SOAP 1.1 多层 Subcode Fault。"""
    return (
        _ENVELOPE_OPEN
        + "<s:Fault>"
        "<s:Code><s:Value>s:Sender</s:Value>"
        "<s:Subcode><s:Value>ter:InvalidArgVal</s:Value></s:Subcode>"
        "<s:Subcode><s:Value>ter:ConfigModify</s:Value></s:Subcode></s:Code>"
        "<s:Reason><s:Text xml:lang=\"en\">The configuration parameters are not "
        f"possible to set [{why}]</s:Text></s:Reason>"
        "</s:Fault>" + _ENVELOPE_CLOSE
    )


# 实机 SetVideoEncoderConfiguration 只认它自己 Get 回来的形状，四种"标准写法"
# 全被判 400：token 写成子元素、码率写成 Media2 的 Constant/AverageBitrateRate、
# GovLength 挂顶层、多塞 ForcePersistence。这里逐条复刻，改协议一跑就现形。
_SET_SHAPE = (
    '<tt:Configuration token="ENC0"',
    "<tt:Name>",
    "<tt:UseCount>",
    "<tt:Encoding>",
    "<tt:Resolution>",
    "<tt:Quality>",
    "<tt:RateControl>",
    "<tt:FrameRateLimit>",
    "<tt:EncodingInterval>",
    "<tt:BitrateLimit>",
    "</tt:RateControl>",
    "<tt:H264>",
    "<tt:GovLength>",
    "<tt:H264Profile>",
    "</tt:H264>",
)
_SET_BANNED = (
    "<tt:token>",
    "ForcePersistence",
    "ConstantBitrateRate",
    "AverageBitrateRate",
)


def _set_body_rejected(raw: str) -> str:
    """按实机口味检查 Set 请求体，返回拒收原因（空串 = 设备会接受）。"""
    pos = 0
    for tag in _SET_SHAPE:
        i = raw.find(tag, pos)
        if i < 0:
            return f"缺少或顺序不对：{tag}"
        pos = i + len(tag)
    for bad in _SET_BANNED:
        if bad in raw:
            return f"实机不接受的写法：{bad}"
    return ""

# 实机原文：雄迈相机对未挂载的 /onvif/* 路径回 HTTP 200 + 这份 HTML，
# 而它恰好是合法 XML（标签全闭合），不校验根元素就会被当成"连通但字段全空"。
_HTML_404 = (
    b"<html><head><title>404 File Not Found</title></head>\n"
    b"<body>The requested URL was not found on this server</body></html>"
)

# 192.168.1.10:8899 实录原文（假服务回放时把 host:port 换成本地测试端口，
# 路径、顺序、属性写法一律照抄真机）。
_XM_ADDR = "192.168.1.10:8899"
# 三处与标准 Media1 不同，全部照抄真机：
# 1. GetServices 同时报 media2_service 和 media_service，且 media2 在前；
# 2. GetProfiles 的 <Configurations/> 是空的，编码 token 只能退回 Profile token；
# 3. Options 是一栈并列块、编码写在子元素里、帧率写在 FrameRatesSupported 属性上。
GET_SERVICES_XM = (
    _ENVELOPE_OPEN +
    f"<tdr:GetServicesResponse {_NS_DEV}>"
    "<tdr:Service>"
    "<tdr:XAddr>http://192.168.1.10:8899/onvif/device_service</tdr:XAddr>"
    "</tdr:Service>"
    "<tdr:Service>"
    "<tdr:XAddr>http://192.168.1.10:8899/onvif/media2_service</tdr:XAddr>"
    "</tdr:Service>"
    "<tdr:Service>"
    "<tdr:XAddr>http://192.168.1.10:8899/onvif/media_service</tdr:XAddr>"
    "</tdr:Service>"
    "</tdr:GetServicesResponse>" + _ENVELOPE_CLOSE
)

PROFILES_XM = (
    _ENVELOPE_OPEN +
    f'<tr2:GetProfilesResponse {_NS_MEDIA} '
    'xmlns:tr2="http://www.onvif.org/ver20/media/wsdl">'
    '<tr2:Profiles token="000" fixed="true">'
    "<tr2:Name>mainStream</tr2:Name><tr2:Configurations/></tr2:Profiles>"
    '<tr2:Profiles token="001" fixed="true">'
    "<tr2:Name>subStream</tr2:Name><tr2:Configurations/></tr2:Profiles>"
    "</tr2:GetProfilesResponse>" + _ENVELOPE_CLOSE
)

ENC_XM = (
    _ENVELOPE_OPEN +
    f"<trt:GetVideoEncoderConfigurationResponse {_NS_MEDIA}>"
    f'<trt:Configuration token="000" {_NS_TT}>'
    "<tt:Name>V_ENC_000</tt:Name><tt:UseCount>1</tt:UseCount>"
    "<tt:Encoding>H265</tt:Encoding>"
    "<tt:Resolution><tt:Width>1280</tt:Width><tt:Height>720</tt:Height>"
    "</tt:Resolution><tt:Quality>4</tt:Quality>"
    "<tt:RateControl><tt:FrameRateLimit>20</tt:FrameRateLimit>"
    "<tt:EncodingInterval>0</tt:EncodingInterval>"
    "<tt:BitrateLimit>851</tt:BitrateLimit></tt:RateControl>"
    "<tt:H265><tt:GovLength>20</tt:GovLength>"
    "<tt:H265Profile>Main</tt:H265Profile></tt:H265>"
    "</trt:Configuration>"
    "</trt:GetVideoEncoderConfigurationResponse>" + _ENVELOPE_CLOSE
)

_OPTIONS_XM_BLOCK = """
      <tr2:Options GovLengthRange="1 300" FrameRatesSupported="25 24 23 22 21 20 \
19 18 17 16 15 14 13 12 11 10 9 8 7 6 5 4 3 2 1" \
ProfilesSupported="{profiles}" ConstantBitRateSupported="true">
        <tt:Encoding>{enc}</tt:Encoding>
        <tt:QualityRange><tt:Min>1</tt:Min><tt:Max>6</tt:Max></tt:QualityRange>
        <tt:ResolutionsAvailable><tt:Width>1280</tt:Width><tt:Height>720</tt:Height>\
</tt:ResolutionsAvailable>
        <tt:ResolutionsAvailable><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height>\
</tt:ResolutionsAvailable>
        <tt:ResolutionsAvailable><tt:Width>2304</tt:Width><tt:Height>1296</tt:Height>\
</tt:ResolutionsAvailable>
        <tt:BitrateRange><tt:Min>32</tt:Min><tt:Max>10240</tt:Max></tt:BitrateRange>
      </tr2:Options>"""

OPTIONS_XM = (
    _ENVELOPE_OPEN +
    f'<tr2:GetVideoEncoderConfigurationOptionsResponse {_NS_MEDIA} {_NS_TT} '
    'xmlns:tr2="http://www.onvif.org/ver20/media/wsdl">'
    + _OPTIONS_XM_BLOCK.format(enc="H264", profiles="Baseline Main High")
    + _OPTIONS_XM_BLOCK.format(enc="H265", profiles="Main Main10")
    + "</tr2:GetVideoEncoderConfigurationOptionsResponse>" + _ENVELOPE_CLOSE
)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A002 - 静默测试服务的访问日志
        pass

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 约定的方法名
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        self.server.bodies.append(raw)
        action = (self.headers.get("SOAPAction") or "").strip('"').rsplit("/", 1)[-1]
        if self.server.mode == "digest" and not (
            self.headers.get("Authorization") or ""
        ).lower().startswith("digest"):
            self._reply(
                401, b"",
                challenge='Digest realm="onvif", nonce="dcd98b71", qop="auth"',
            )
            return
        if self.server.mode == "http400":
            self._reply(400, _fault_config("测试固定回放").encode())
            return
        if self.server.mode == "http404":
            self._reply(404, _HTML_404)
            return
        if self._strict_set(action, raw):
            return
        self._reply(200, self._payload(action, raw))

    def _strict_set(self, action: str, raw: str) -> bool:
        """复刻实机：形状不对就 400 + 多层 Subcode Fault，形状对就 200。"""
        if self.server.mode != "strict_set" or action != "SetVideoEncoderConfiguration":
            return False
        why = _set_body_rejected(raw)
        if why:
            self._reply(400, _fault_config(why).encode())
        else:
            self._reply(200, SET_RESP.encode())
        return True

    def _payload(self, action: str, raw: str) -> bytes:
        if self.server.mode == "html":
            return _HTML_404
        if self.server.mode == "garbage":
            return b"not xml at all <<<"
        if self.server.mode == "fault":
            return SOAP_FAULT.encode()
        port = self.server.server_address[1]
        xm = self.server.mode == "xm"
        table = {
            "GetServices": (
                GET_SERVICES_XM.replace(_XM_ADDR, f"127.0.0.1:{port}")
                if xm
                else GET_SERVICES.replace("{port}", str(port))
            ),
            "GetDeviceInformation": DEVICE_INFO,
            "GetProfiles": PROFILES_XM if xm else PROFILES,
            "GetVideoEncoderConfiguration": (
                ENC_XM
                if xm
                else (ENC_VBR if "ENC1" in raw else ENC_CBR)
            ),
            "GetVideoEncoderConfigurationOptions": (
                OPTIONS_XM
                if xm
                else (
                    OPTIONS_EMPTY if self.server.mode == "no_options" else OPTIONS
                )
            ),
            "SetVideoEncoderConfiguration": SET_RESP,
        }
        payload = table.get(action)
        if payload is None:
            return SOAP_FAULT.encode()
        return payload.encode()

    def _reply(self, code: int, payload: bytes, challenge: str = "") -> None:
        self.send_response(code)
        if challenge:
            self.send_header("WWW-Authenticate", challenge)
        self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)


@pytest.fixture()
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.mode = "wsse"  # type: ignore[attr-defined]
    srv.bodies = []  # type: ignore[attr-defined]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _client(srv, **kw) -> OnvifClient:
    return OnvifClient(
        "127.0.0.1", srv.server_address[1], "admin", "pass", timeout_s=5.0, **kw
    )


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _opt() -> EncoderOptions:
    return EncoderOptions(
        encodings=["H264", "H265"],
        resolutions=[(640, 360), (1280, 720), (1920, 1080)],
        fps_range=(1, 25),
        bitrate_range=(64, 4096),
        source="device",
    )


# ---------------------------------------------------------------- WSSE 头
def test_wsse_digest_is_recomputable():
    """设备端只用 Nonce+Created+密码原文复算 sha1，与 Password 比对。"""
    import xml.etree.ElementTree as ET

    fragment = _wsse_header("admin", "secret")
    root = ET.fromstring(
        '<wrap xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        + fragment
        + "</wrap>"
    )

    def local(name: str):
        for el in root.iter():
            if el.tag.rsplit("}", 1)[-1] == name:
                return el
        raise AssertionError(f"WSSE 头缺少 {name}")

    created = local("Created").text
    nonce_b64 = local("Nonce").text
    assert local("Username").text == "admin"
    raw = base64.b64decode(nonce_b64) + created.encode("utf-8") + b"secret"
    expect = base64.b64encode(hashlib.sha1(raw).digest()).decode("ascii")
    assert local("Password").text == expect
    assert local("Nonce").get("EncodingType", "").endswith("#Base64Binary")


# ---------------------------------------------------------------- 完整往返
def test_wsse_flow_read_and_write(server):
    cli = _client(server)
    info = cli.get_device_information()
    assert info["model"] == "IPG-85HE20PY"
    assert cli.auth_mode == "wsse"
    assert cli.media_url().endswith("/onvif/media_service")

    profiles = cli.get_profiles()
    assert [p.encoder_token for p in profiles] == ["ENC0", "ENC1"]
    assert profiles[0].name == "Chan1_Main"

    st = cli.get_encoder_configuration("ENC0")
    assert (st.width, st.height, st.frame_rate) == (1920, 1080, 25)
    assert (st.bitrate_kbps, st.rate_mode, st.gov_length) == (2048, "C", 20)
    assert st.quality == 4.0

    opt = cli.get_encoder_options("ENC0")
    assert opt.source == "device"
    assert opt.fps_range == (1, 25)
    assert opt.bitrate_range == (64, 4096)
    assert opt.resolution_labels() == ["640x360", "1280x720", "1920x1080"]

    assert cli.set_encoder_configuration(st) == "ENC0"
    sent = "".join(server.bodies)
    assert "<wsse:Nonce" in sent, "WSSE 头必须随包发 Nonce，否则设备无法复算摘要"
    assert '<tt:Configuration token="ENC0"' in sent, "token 必须是属性，写成子元素实机拒收"
    assert "<tt:token>" not in sent
    assert "ForcePersistence" not in sent, "成功形状里没有 ForcePersistence，多塞实机拒收"


def test_parse_vbr_rate_control(server):
    cli = _client(server)
    st = cli.get_encoder_configuration("ENC1")
    assert st.rate_mode == "V"
    assert st.bitrate_kbps == 1500
    assert st.peak_bitrate_kbps == 3000
    assert st.gov_length is None


def test_set_body_uses_device_accepted_shape(server):
    """写入形状必须逐字节对上实机唯一接受的那一种（含子元素顺序）。"""
    cli = _client(server)
    st = EncoderSettings(
        encoder_token="ENC0", name="main_enc", encoding="H264",
        width=1280, height=720, frame_rate=15, bitrate_kbps=1500,
        rate_mode="V", peak_bitrate_kbps=3000, quality=4, gov_length=20,
        encoding_interval=1, codec_profile="Main",
    )
    cli.set_encoder_configuration(st)
    sent = server.bodies[-1]
    assert _set_body_rejected(sent) == "", f"请求体不是实机接受的形状：{sent}"
    assert "<tt:FrameRateLimit>15" in sent
    assert "<tt:EncodingInterval>1" in sent
    assert "<tt:BitrateLimit>1500" in sent
    assert "<tt:H264><tt:GovLength>20</tt:GovLength>" in sent
    assert "<tt:H264Profile>Main</tt:H264Profile>" in sent
    # Media1 的 RateControl 没有 CBR/VBR 之分，码率只落在 BitrateLimit 上
    assert "AverageBitrateRate" not in sent
    assert "ConstantBitrateRate" not in sent


def test_set_body_omits_codec_block_for_jpeg(server):
    """JPEG 这类编码没有 GovLength/Profile 块，硬塞实机照样拒收。"""
    cli = _client(server)
    st = EncoderSettings(
        encoder_token="ENC0", encoding="JPEG", width=704, height=576,
        frame_rate=5, bitrate_kbps=512, quality=4,
    )
    cli.set_encoder_configuration(st)
    sent = server.bodies[-1]
    assert "<tt:H264" not in sent and "<tt:GovLength" not in sent
    assert "<tt:Encoding>JPEG</tt:Encoding>" in sent


def test_strict_shape_server_accepts_our_write(server):
    """假服务按实机口味挑剔形状：我们的写入必须一次通过。"""
    server.mode = "strict_set"
    cli = _client(server)
    st = cli.get_encoder_configuration("ENC0")
    st.bitrate_kbps = 1800
    assert cli.set_encoder_configuration(st) == "ENC0"


def test_strict_shape_server_would_reject_the_old_write(server):
    """反向自证：把旧写法（子元素 token + ConstantBitrateRate + ForcePersistence）
    喂给同一个假服务，必须被拒——否则这个守卫是空的。"""
    server.mode = "strict_set"
    legacy = (
        '<tt:Configuration token="ENC0" xmlns:tt="x">'
        "<tt:Name>main_enc</tt:Name>"
        "<tt:token>ENC0</tt:token><tt:Encoding>H264</tt:Encoding>"
        "<tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height>"
        "</tt:Resolution><tt:Quality>4</tt:Quality>"
        "<tt:RateControl><tt:ConstantBitrateRate>2048</tt:ConstantBitrateRate>"
        "</tt:RateControl><tt:BitrateLimit>2048</tt:BitrateLimit>"
        "<tt:GovLength>20</tt:GovLength></tt:Configuration>"
        "<a:ForcePersistence>true</a:ForcePersistence>"
    )
    assert _set_body_rejected(legacy)


def test_options_absent_falls_back_to_default(server):
    server.mode = "no_options"
    cli = _client(server)
    opt = cli.get_encoder_options("ENC0")
    assert opt.source == "default"
    assert opt.resolutions == []
    assert opt.fps_range == (1, 30)
    assert opt.fps_values[0] == 1 and opt.fps_values[-1] == 30


# ---------------------------------------------------------------- 鉴权降级
def test_http_digest_fallback(server):
    server.mode = "digest"
    cli = _client(server)
    info = cli.get_device_information()
    assert info["manufacturer"] == "XM"
    assert cli.auth_mode == "http-digest"
    digest_hits = [b for b in server.bodies]
    assert digest_hits, "重试请求应到达服务端"


# ---------------------------------------------------------------- 错误路径
def test_soap_fault_raises(server):
    server.mode = "fault"
    cli = _client(server)
    with pytest.raises(OnvifError, match="SOAP Fault"):
        cli.get_device_information()


def test_http_400_fault_is_readable(server):
    """设备把 Fault 装在 HTTP 400 里回：界面要看到人话，不许甩一串被截断的 XML。"""
    server.mode = "http400"
    cli = _client(server)
    with pytest.raises(OnvifError) as exc:
        cli.get_device_information()
    msg = str(exc.value)
    assert "HTTP 400" in msg
    assert "SOAP Fault" in msg
    assert "ter:InvalidArgVal/ter:ConfigModify" in msg, "多层 Subcode 要拼全"
    assert "not possible to set" in msg
    assert "<?xml" not in msg, "原始信封不该出现在界面上"


def test_http_404_html_keeps_raw_head(server):
    """404 里装的若不是 SOAP（网关/相机错误页），保留原文开头便于定位是谁在应答。"""
    server.mode = "http404"
    cli = _client(server)
    with pytest.raises(OnvifError) as exc:
        cli.get_device_information()
    msg = str(exc.value)
    assert "HTTP 404" in msg
    assert "404 File Not Found" in msg


def test_html_200_is_not_mistaken_for_onvif(server):
    """端口通 ≠ ONVIF 通：HTTP 200 + HTML 404 必须报错，不许静默返回空字段。"""
    server.mode = "html"
    cli = _client(server)
    with pytest.raises(OnvifError, match="不是 SOAP 信封") as exc:
        cli.get_device_information()
    assert "404 File Not Found" in str(exc.value), "报错要带上设备原文，便于判断是谁在应答"


def test_non_xml_body_raises(server):
    server.mode = "garbage"
    cli = _client(server)
    with pytest.raises(OnvifError, match="不是合法 XML"):
        cli.get_device_information()


def test_unreachable_raises():
    cli = OnvifClient("127.0.0.1", _free_port(), timeout_s=2.0)
    t0 = time.monotonic()
    with pytest.raises(OnvifError, match="不可达"):
        cli.get_device_information()
    assert time.monotonic() - t0 < 6.0, "拒绝/超时都要尽快归一为 OnvifError，不许挂死"


def test_set_rejects_missing_token_and_zero_values():
    cli = OnvifClient("127.0.0.1", 1)
    with pytest.raises(OnvifError, match="token"):
        cli.set_encoder_configuration(
            EncoderSettings(width=640, height=360, frame_rate=25, bitrate_kbps=512)
        )
    with pytest.raises(OnvifError, match="禁止"):
        cli.set_encoder_configuration(
            EncoderSettings(
                encoder_token="ENC0", width=0, height=360,
                frame_rate=25, bitrate_kbps=512,
            )
        )


def test_media_action_whitelist():
    cli = OnvifClient("127.0.0.1", 1)
    with pytest.raises(OnvifError, match="未允许的 media 动作"):
        cli._media("RemoveAllProfiles", "")


# ---------------------------------------------------------------- 值域夹取
def test_clamped_fixes_out_of_range_fps_and_bitrate():
    st = EncoderSettings(
        encoder_token="ENC0", encoding="H264", width=1920, height=1080,
        frame_rate=30, bitrate_kbps=6000,
    )
    out, fixed = st.clamped(_opt())
    assert out.frame_rate == 25
    assert out.bitrate_kbps == 4096
    assert len(fixed) == 2


def test_clamped_picks_closest_resolution():
    st = EncoderSettings(
        encoder_token="ENC0", encoding="H264", width=1288, height=728,
        frame_rate=15, bitrate_kbps=1024,
    )
    out, fixed = st.clamped(_opt())
    assert (out.width, out.height) == (1280, 720)
    assert any("分辨率" in f for f in fixed)


def test_clamped_replaces_unsupported_encoding():
    st = EncoderSettings(
        encoder_token="ENC0", encoding="MJPEG", width=640, height=360,
        frame_rate=10, bitrate_kbps=512,
    )
    out, fixed = st.clamped(_opt())
    assert out.encoding in ("H264", "H265")
    assert any("编码" in f for f in fixed)


def test_clamped_keeps_valid_values_untouched():
    st = EncoderSettings(
        encoder_token="ENC0", encoding="H265", width=1280, height=720,
        frame_rate=15, bitrate_kbps=1024,
    )
    out, fixed = st.clamped(_opt())
    assert fixed == []
    assert (out.width, out.height, out.frame_rate, out.bitrate_kbps) == (
        1280, 720, 15, 1024,
    )


def test_clamped_respects_zero_as_skip():
    st = EncoderSettings(encoder_token="ENC0", width=640, height=360)
    out, fixed = st.clamped(_opt())
    assert fixed == [], "帧率/码率为 0 表示未填，不参与夹取"
    assert out.frame_rate == 0 and out.bitrate_kbps == 0


# -------------------------------------------- 雄迈真机（192.168.1.10:8899）回放
def test_pick_media_url_prefers_media1_over_media2():
    """真机把 media2_service 报在 media_service 前面，本客户端只会发 Media1 动作。"""
    urls = [
        "http://192.168.1.10:8899/onvif/device_service",
        "http://192.168.1.10:8899/onvif/media2_service",
        "http://192.168.1.10:8899/onvif/media_service",
    ]
    assert _pick_media_url(urls) == urls[2]
    assert _pick_media_url(urls[:2]) == "", "只有 media2 时不许冒充命中"


def test_xm_media_url_picks_media1_service(server):
    server.mode = "xm"
    cli = _client(server)
    assert cli.media_url().endswith("/onvif/media_service")
    assert cli.media_from_device is True


def test_xm_profile_token_fallback_when_configurations_empty(server):
    """真机 <Configurations/> 是空的，编码 token 只能退回 Profile token（000/001）。"""
    server.mode = "xm"
    cli = _client(server)
    assert [(p.token, p.name, p.encoder_token) for p in cli.get_profiles()] == [
        ("000", "mainStream", "000"),
        ("001", "subStream", "001"),
    ]


def test_xm_encoder_configuration_read(server):
    server.mode = "xm"
    st = _client(server).get_encoder_configuration("000")
    assert st.encoding == "H265"
    assert (st.width, st.height, st.frame_rate) == (1280, 720, 20)
    assert st.bitrate_kbps == 851
    assert st.gov_length == 20
    assert st.quality == 4.0
    assert st.encoding_interval == 0, "Media1 的抽帧间隔要读回来，写回时才不会冲掉"
    assert st.codec_profile == "Main", "编码档位读回来，写回时才不会凭空造一个"


def test_xm_options_media2_shape(server):
    """Media2 风格值域必须全解析出来——退回默认值域等于把假选项摆给 UI。"""
    server.mode = "xm"
    opt = _client(server).get_encoder_options("000")
    assert opt.source == "device"
    assert opt.encodings == ["H264", "H265"]
    assert opt.resolution_labels() == ["1280x720", "1920x1080", "2304x1296"]
    assert opt.resolutions_for("H265") == [
        (1280, 720),
        (1920, 1080),
        (2304, 1296),
    ]
    assert opt.fps_range == (1, 25)
    assert opt.fps_supported == list(range(1, 26)), "帧率是离散枚举不是连续区间"
    assert opt.fps_values == list(range(1, 26))
    assert opt.bitrate_range == (32, 10240)


def test_xm_read_all_profiles_options(server):
    """探测脚本那条链路：设备信息 → 服务 → Profile → 参数 → 值域一次跑通。"""
    server.mode = "xm"
    cli = _client(server)
    assert cli.get_device_information()["manufacturer"] == "XM"
    assert cli.auth_mode == "wsse"
    for p in cli.get_profiles():
        st = cli.get_encoder_configuration(p.encoder_token)
        assert st.width > 0 and st.frame_rate > 0, p.token
        assert cli.get_encoder_options(p.encoder_token).source == "device"


# ---------------------------------------------------------------- 分档值域夹取
def _xm_opt() -> EncoderOptions:
    return EncoderOptions(
        encodings=["H264", "H265"],
        resolutions=[(1280, 720), (1920, 1080), (2304, 1296)],
        resolutions_by_encoding={
            "H264": [(1280, 720), (1920, 1080), (2304, 1296)],
            "H265": [(1280, 720), (1920, 1080)],
        },
        fps_range=(1, 25),
        fps_supported=list(range(1, 26)),
        bitrate_range=(32, 10240),
        source="device",
    )


def test_clamped_snaps_to_discrete_fps_and_bitrate():
    st = EncoderSettings(
        encoder_token="000", encoding="H265", width=1920, height=1080,
        frame_rate=26, bitrate_kbps=20000,
    )
    out, fixed = st.clamped(_xm_opt())
    assert out.frame_rate == 25
    assert out.bitrate_kbps == 10240
    assert len(fixed) == 2


def test_clamped_uses_resolution_of_selected_encoding():
    """H265 不供 2304x1296，换编码后必须按 H265 的档位夹，而不是用并集。"""
    st = EncoderSettings(
        encoder_token="000", encoding="H265", width=2304, height=1296,
        frame_rate=20, bitrate_kbps=2000,
    )
    out, fixed = st.clamped(_xm_opt())
    assert (out.width, out.height) == (1920, 1080)
    assert any("分辨率" in f for f in fixed)
