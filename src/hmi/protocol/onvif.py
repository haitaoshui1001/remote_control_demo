"""最小 ONVIF 客户端（纯标准库）：远程读/写摄像头编码参数。

只做本项目需要的四个动作：设备信息（连通性+鉴权探测）、Profile 列表、
编码参数读取、编码参数写入。合法值域（分辨率档位、帧率区间、码率区间）
一律向设备问 ``GetVideoEncoderConfigurationOptions``，不硬编码——雄迈
固件实际支持的档位只有设备自己说了算。

为什么不引 onvif-zeep：那串依赖（zeep/lxml/requests）会把 PyInstaller
打包和离线安装复杂化，而我们只需四条 SOAP 调用，``urllib`` + ``xml.etree``
手写信封即可，测试也能完全离线（假 HTTP 服务回放录好的应答）。

鉴权按 ONVIF 惯例做两级尝试：先 WS-Security UsernameToken（PasswordDigest，
走内网/图传桥接的常规做法），设备若回 401 且带 ``Digest`` 挑战，再改走
HTTP Digest。哪种成功记在 ``auth_mode``，供探测脚本汇报。

摄像头挂在远端图传模块之后，本客户端只做按固定 IP 直连——与 RTSP 取流
同一个 camera_ip。（实机验证：WS-Discovery 组播其实能穿过 EWD611 空口，
将来要做设备自动发现可以用它，但配置参数这件事直连更省事。）
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime

__all__ = [
    "EncoderOptions",
    "EncoderSettings",
    "OnvifClient",
    "OnvifError",
    "OnvifProfile",
]

_NS_S = "http://www.w3.org/2003/05/soap-envelope"
_NS_DEV = "http://www.onvif.org/ver10/device/wsdl"
_NS_TRT = "http://www.onvif.org/ver10/media/wsdl"
_NS_TT = "http://www.onvif.org/ver10/schema"
_NS_WSSE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-secext-1.0.xsd"
)
_NS_WSU = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-utility-1.0.xsd"
)
_DIGEST_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
_NONCE_ENC = _NS_WSSE + "#Base64Binary"

_MEDIA_ACTIONS = (
    "GetProfiles",
    "GetVideoEncoderConfiguration",
    "GetVideoEncoderConfigurationOptions",
    "SetVideoEncoderConfiguration",
)

# Options 应答里代表"某一编码的分档值域"的名字（Media1 用块名，Media2 用
# <Encoding> 文本，两边都先经 _norm_encoding 归一到这些名字上）。
_ENCODING_NAMES = ("H264", "H265", "MPEG4", "JPEG", "JPEG2000", "AV1")

# 各厂商写法的别名表：Media1 的块名是 ``Mpeg4``，私有 JSON 里 HEVC 与 H265 混用，
# 不归一就会出现"设备支持 H265 但下拉里找不到"的假象。
_ENCODING_ALIASES = {
    "AVC": "H264",
    "H264": "H264",
    "HEVC": "H265",
    "H265": "H265",
    "MPEG4": "MPEG4",
    "MP4V": "MPEG4",
    "JPEG2000": "JPEG2000",
    "JPEG": "JPEG",
    "AV1": "AV1",
}


def _norm_encoding(raw: str) -> str:
    """编码名归一：去掉 ``.`` ``-`` 等分隔符再查别名，认不出的原样大写返回。"""
    key = re.sub(r"[^A-Z0-9]", "", (raw or "").upper())
    return _ENCODING_ALIASES.get(key, key)


class OnvifError(RuntimeError):
    """ONVIF 调用失败：网络不通、鉴权被拒、SOAP Fault、应答不成样子都归到这里。"""


@dataclass
class OnvifProfile:
    token: str
    name: str
    encoder_token: str = ""


@dataclass
class EncoderSettings:
    """一路码流的编码参数（改哪个码流由 encoder_token 决定）。"""

    encoder_token: str = ""
    name: str = ""
    encoding: str = "H264"
    width: int = 0
    height: int = 0
    frame_rate: int = 0
    bitrate_kbps: int = 0
    rate_mode: str = "C"  # C = 恒定码率（CBR），V = 可变码率（VBR）；仅读侧推断
    quality: float | None = None
    gov_length: int | None = None
    peak_bitrate_kbps: int | None = None
    encoding_interval: int = 0  # Media1 RateControl 的抽帧间隔，0 = 不抽帧
    codec_profile: str = "Main"  # H264Profile / H265Profile 档位
    # 设备"真实"编码：厂商私有字段报的档位。TP-Link 的 ``tt:Encoding`` 恒为占位
    # 值 H264，真值只在 ``anyAttribute`` 的 JSON 里（encode_type=H265）。空串表示
    # 没有私有字段、与 ``encoding`` 相同。只在读侧与 UI 使用，写回仍按 ``encoding``
    # 原样回传，避免把 H.265 悄悄改成 H.264。
    encoding_actual: str = ""

    def clamped(self, opt: EncoderOptions) -> tuple[EncoderSettings, list[str]]:
        """用设备自报的值域过滤目标参数，返回（修正后的设置，修正说明列表）。"""
        fixed: list[str] = []
        out = EncoderSettings(**self.__dict__)
        # 先定编码：分辨率档位是按编码给的，编码换了合法档位列表也跟着换
        if opt.encodings and out.encoding not in opt.encodings:
            fixed.append(f"编码 {out.encoding} 不受支持，改为 {opt.encodings[0]}")
            out.encoding = opt.encodings[0]
        allowed = opt.resolutions_for(out.encoding)
        if allowed and (out.width, out.height) not in allowed:
            best = min(allowed, key=lambda r: abs(r[0] * r[1] - out.width * out.height))
            fixed.append(
                f"分辨率 {out.width}x{out.height} 不在设备支持列表，改为 {best[0]}x{best[1]}"
            )
            out.width, out.height = best
        if out.frame_rate:
            lo, hi = opt.fps_range
            cl = int(min(max(out.frame_rate, lo), hi))
            if opt.fps_supported and out.frame_rate not in opt.fps_supported:
                cl = min(opt.fps_supported, key=lambda v: abs(v - out.frame_rate))
                fixed.append(
                    f"帧率 {out.frame_rate} 不是设备档位（{min(opt.fps_supported)}"
                    f"~{max(opt.fps_supported)}），改为 {cl}"
                )
                out.frame_rate = cl
            elif cl != out.frame_rate:
                fixed.append(f"帧率 {out.frame_rate} 越界（{lo}~{hi}），改为 {cl}")
                out.frame_rate = cl
        if out.bitrate_kbps:
            lo, hi = opt.bitrate_range
            cl = int(min(max(out.bitrate_kbps, lo), hi))
            if cl != out.bitrate_kbps:
                fixed.append(f"码率 {out.bitrate_kbps}kbps 越界（{lo}~{hi}），改为 {cl}")
                out.bitrate_kbps = cl
        return out, fixed


@dataclass
class EncoderOptions:
    """设备自报的合法值域：UI 的下拉与 spin 范围全部由此而来。

    ``resolutions`` 是所有编码的并集，``resolutions_by_encoding`` 保留分编码
    的档位——雄迈实机 H264 与 H265 支持档位可能不同，换了编码就得换列表。
    """

    encodings: list[str] = field(default_factory=lambda: ["H264"])
    resolutions: list[tuple[int, int]] = field(default_factory=list)
    resolutions_by_encoding: dict[str, list[tuple[int, int]]] = field(
        default_factory=dict
    )
    fps_range: tuple[int, int] = (1, 30)
    fps_supported: list[int] = field(default_factory=list)
    bitrate_range: tuple[int, int] = (16, 8192)
    source: str = "default"  # device = 设备实报，default = 没报、用保守默认值域

    def resolutions_for(self, encoding: str) -> list[tuple[int, int]]:
        """该编码支持的档位；设备没分档时退回并集。"""
        rows = self.resolutions_by_encoding.get(str(encoding).upper())
        return rows or self.resolutions

    @property
    def fps_values(self) -> list[int]:
        if self.fps_supported:
            return list(self.fps_supported)
        return list(range(self.fps_range[0], self.fps_range[1] + 1))

    def resolution_labels(self) -> list[str]:
        return [f"{w}x{h}" for w, h in self.resolutions]


def _local(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _find(el: ET.Element, name: str) -> ET.Element | None:
    if _local(el) == name:
        return el
    for child in el:
        hit = _find(child, name)
        if hit is not None:
            return hit
    return None


def _findall(el: ET.Element, name: str) -> list[ET.Element]:
    out: list[ET.Element] = []
    for child in el:
        if _local(child) == name:
            out.append(child)
        else:
            out.extend(_findall(child, name))
    return out


def _num(el: ET.Element | None, name: str, default: int = 0) -> int:
    hit = _find(el, name) if el is not None else None
    if hit is None or not (hit.text or "").strip():
        return default
    try:
        return int(float(hit.text))
    except ValueError:
        return default


def _text_of(el: ET.Element | None, name: str, default: str = "") -> str:
    hit = _find(el, name) if el is not None else None
    if hit is None or not (hit.text or "").strip():
        return default
    return (hit.text or "").strip()


def _ints_in_text(text: str) -> list[int]:
    """把 ``"25 24 23 …"`` 这类空格分隔的枚举拆成整数列表（设备实报帧率档）。"""
    out: list[int] = []
    for part in text.split():
        try:
            out.append(int(float(part)))
        except ValueError:
            continue
    return out


def _private_video(cfg: ET.Element, token: str) -> dict:
    """取厂商私有 JSON 里本路码流的配置字典，取不到返回空字典。

    TP-Link 把真配置塞在 ``trt:Configuration`` 的 ``anyAttribute`` **属性**上
    （一段转义 JSON，形如 ``{"video":{"main":{...}},"error_code":0}``），标准
    ``tt:Encoding`` 只是占位值。字典按编码 token 分组，token 对不上就退回首项。
    """
    raw = (cfg.get("anyAttribute") or "").strip()
    if not raw.startswith("{"):
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    video = data.get("video")
    if not isinstance(video, dict):
        return {}
    rows = [v for v in video.values() if isinstance(v, dict)]
    if not rows:
        return {}
    own = video.get(token)
    if isinstance(own, dict) and own:
        return own
    return rows[0]


def _rate_mode(raw: str) -> str:
    """私有字段里的码率模式（cbr/vbr）归一为 C/V，认不出返回空串。"""
    key = (raw or "").strip().lower()
    if key in ("cbr", "const", "constant"):
        return "C"
    if key in ("vbr", "var", "variable"):
        return "V"
    return ""


def _option_blocks(root: ET.Element):
    """把两种 Options 形状统一成 (编码名, 值域元素, 属性宿主)。

    Media1 是一块一编码：``<Options><H264>…</H264><JPEG>…</JPEG></Options>``；
    雄迈实机回的是 Media2 风格——并列多个 ``<Options>``，编码写在子元素
    ``<Encoding>`` 里，支持的帧率枚举写在 ``FrameRatesSupported`` 属性上。
    只按 Media1 形状找 ``<H264>`` 块，实机就会一样都取不到、全体退回默认值域。

    ``<Extension>`` 里也可能带编码块（TP-Link 把 BitrateRange 放这儿），有些固件
    只在 Extension 里报 H265，所以两层都要扫；同名块会出现多次，由调用方合并值域。
    """
    for opt in _findall(root, "Options"):
        blocks = [c for c in opt if _norm_encoding(_local(c)) in _ENCODING_NAMES]
        for ext in opt:
            if _local(ext) != "Extension":
                continue
            blocks.extend(
                c for c in ext if _norm_encoding(_local(c)) in _ENCODING_NAMES
            )
        if blocks:
            for c in blocks:
                yield _norm_encoding(_local(c)), c, opt
            continue
        enc = _norm_encoding(_text_of(opt, "Encoding"))
        if enc:
            yield enc, opt, opt


def _pick_media_url(urls: list[str]) -> str:
    """在设备自报的服务地址里挑 Media1 媒体服务，挑不到返回空串。"""
    for url in urls:
        if url.rstrip("/").lower().endswith("/media_service"):
            return url
    for url in urls:
        low = url.lower()
        if "media" in low and "media2" not in low:
            return url
    return ""


def _soap_envelope(body: str, header: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{_NS_S}">'
        f"<s:Header>{header}</s:Header>"
        f"<s:Body>{body}</s:Body>"
        "</s:Envelope>"
    ).encode()


def _wsse_header(user: str, password: str) -> str:
    """WS-Security UsernameToken / PasswordDigest：sha1(nonce+created+password)。"""
    created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = secrets.token_bytes(16)
    raw = nonce + created.encode("utf-8") + password.encode("utf-8")
    digest = base64.b64encode(hashlib.sha1(raw).digest()).decode("ascii")
    b64_nonce = base64.b64encode(nonce).decode("ascii")
    return (
        f'<wsse:Security xmlns:wsse="{_NS_WSSE}" xmlns:wsu="{_NS_WSU}"'
        ' s:mustUnderstand="1">'
        "<wsse:UsernameToken>"
        f"<wsse:Username>{user}</wsse:Username>"
        f'<wsse:Password Type="{_DIGEST_TYPE}">{digest}</wsse:Password>'
        f'<wsse:Nonce EncodingType="{_NONCE_ENC}">{b64_nonce}</wsse:Nonce>'
        f"<wsu:Created>{created}</wsu:Created>"
        "</wsse:UsernameToken>"
        "</wsse:Security>"
    )


def _head(data: bytes, count: int = 160) -> str:
    return data[:count].decode("utf-8", "replace")


def _codec_block(encoding: str) -> str:
    """编码名对应的 Media1 编码参数块名；JPEG 这类没有块的设备返回空串。"""
    up = (encoding or "").upper().replace("-", "")
    if up in ("H264", "AVC"):
        return "H264"
    if up in ("H265", "HEVC"):
        return "H265"
    if up in ("MPEG4",):
        return "Mpeg4"
    return ""


def _fault_message(url: str, fault: ET.Element, http_status: int = 0) -> str:
    """把 SOAP Fault 翻成一句人话。

    错误类别在 1.1 里是 ``Code/Value`` 加好几层 ``Subcode/Value``（实机报的
    ``ter:InvalidArgVal`` ``ter:ConfigModify`` 就在其中），只取第一个值会把
    真正的原因丢掉，所以把所有层级的 Value 拼成一串给出。
    """
    codes: list[str] = []
    for node in _findall(fault, "Value"):
        txt = (node.text or "").strip()
        if txt and txt not in codes:
            codes.append(txt)
    code = "/".join(codes) if codes else (_text_of(fault, "faultCode") or "?")
    text = (
        _text_of(fault, "Text")
        or _text_of(fault, "Reason")
        or _text_of(fault, "faultString")
        or "设备返回 SOAP Fault"
    )
    status = f" HTTP {http_status}" if http_status else ""
    return f"{url}{status} SOAP Fault（{code}）：{text}"


def _error_message(url: str, status: int, reason: str, data: bytes) -> str:
    """非 2xx 应答也要尽量说人话：设备常在 400/500 里带完整 SOAP Fault。

    直接把 XML 前缀甩界面上只会得到一串被截断的标签名，看不出改了什么不合法。
    """
    if data:
        try:
            root = ET.fromstring(data)
        except (ET.ParseError, ImportError):
            root = None
        if root is not None and _local(root) == "Envelope":
            fault = _find(root, "Fault")
            if fault is not None:
                return _fault_message(url, fault, status)
        return f"{url} HTTP {status}：{_head(data)}"
    return f"{url} HTTP {status}：{reason}"


def _fromstring(data: bytes) -> ET.Element:
    """``ET.fromstring`` 的包装：解析器缺失也翻译成 ``OnvifError``。

    标准库要到真正建树时才加载 ``pyexpat``，此时抛的是 ``ImportError``——
    它既不是 ``ParseError`` 也不是 ``OnvifError``，会一路逃出上层错误处理、
    打死工作线程，界面只表现为"读取中"永远不返回。
    """
    try:
        return ET.fromstring(data)
    except ImportError as exc:  # pragma: no cover - 仅解析器不可用时走到
        raise OnvifError(
            f"XML 解析器不可用（{exc}）：交付包内 libexpat.dll 与 pyexpat.pyd "
            "不匹配，需重新打包"
        ) from exc


def _parse_xml(data: bytes, url: str) -> ET.Element:
    try:
        root = _fromstring(data)
    except ET.ParseError as exc:
        raise OnvifError(
            f"{url} 应答不是合法 XML：{exc}（开头：{_head(data)!r}）"
        ) from exc
    if _local(root) != "Envelope":
        # 雄迈这类嵌入式 Web 服务对未挂载的路径常回 HTTP 200 + HTML 404 页，
        # 而那页恰好是合法 XML。不校验根元素就会误判成"连通但字段全空"。
        raise OnvifError(f"{url} 应答不是 SOAP 信封（开头：{_head(data)!r}）")
    fault = _find(root, "Fault")
    if fault is not None:
        raise OnvifError(_fault_message(url, fault))
    return root


class OnvifClient:
    """按 IP 直连的最小 ONVIF 客户端；所有方法阻塞，由上层放工作线程里调。"""

    def __init__(
        self,
        host: str,
        port: int = 80,
        user: str = "admin",
        password: str = "",
        timeout_s: float = 8.0,
        device_path: str = "/onvif/device_service",
    ) -> None:
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.timeout_s = float(timeout_s)
        self.device_url = f"http://{host}:{self.port}{device_path}"
        self._media_url: str | None = None
        self.auth_mode = ""
        # 调试钩子：探测脚本设成 [] 后，每个成功应答按 (动作, 原始字节) 记录
        self.trace: list[tuple[str, bytes]] | None = None
        self.media_from_device = False

    # ------------------------------------------------------------------ HTTP/SOAP
    def _post(self, url: str, action_ns: str, action: str, body: str) -> ET.Element:
        envelope = _soap_envelope(
            f'<a:{action} xmlns:a="{action_ns}" xmlns:tt="{_NS_TT}">{body}</a:{action}>',
            _wsse_header(self.user, self.password),
        )
        headers = {
            "Content-Type": "application/soap+xml; charset=utf-8",
            "SOAPAction": f'"{action_ns}/{action}"',
        }

        def _fire(opener: urllib.request.OpenerDirector) -> bytes:
            req = urllib.request.Request(url, data=envelope, headers=headers, method="POST")
            with opener.open(req, timeout=self.timeout_s) as resp:
                return resp.read()

        try:
            data = _fire(urllib.request.build_opener())
            self.auth_mode = self.auth_mode or "wsse"
        except urllib.error.HTTPError as exc:
            challenge = str(exc.headers.get("WWW-Authenticate", ""))
            if exc.code != 401 or "digest" not in challenge.lower():
                try:
                    payload = exc.read()
                except Exception:  # noqa: BLE001 - 应答体读不到就只报状态行
                    payload = b""
                raise OnvifError(
                    _error_message(url, exc.code, str(exc.reason), payload)
                ) from exc
            password_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            password_mgr.add_password(None, url, self.user, self.password)
            handler = urllib.request.HTTPDigestAuthHandler(password_mgr)
            try:
                data = _fire(urllib.request.build_opener(handler))
            except (urllib.error.URLError, OSError, ValueError) as exc2:
                raise OnvifError(f"{url} HTTP Digest 重试失败：{exc2}") from exc2
            self.auth_mode = self.auth_mode or "http-digest"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise OnvifError(f"{url} 不可达：{exc}") from exc
        if self.trace is not None:
            self.trace.append((action, data))
        return _parse_xml(data, url)

    def _device(self, action: str, body: str = "") -> ET.Element:
        return self._post(self.device_url, _NS_DEV, action, body)

    def _media(self, action: str, body: str) -> ET.Element:
        if action not in _MEDIA_ACTIONS:
            raise OnvifError(f"未允许的 media 动作：{action}")
        return self._post(self.media_url(), _NS_TRT, action, body)

    # ------------------------------------------------------------------ 服务地址
    def media_url(self) -> str:
        """媒体服务地址：向设备 GetServices 问一次，问不到按雄迈常见路径猜。

        实机两个都报（``media_service`` 与 ``media2_service``），本客户端发的
        是 Media1 动作，必须挑中 Media1 那个——挑错了在严格固件上直接 Fault。
        """
        if self._media_url:
            return self._media_url
        guess = f"http://{self.host}:{self.port}/onvif/media_service"
        self.media_from_device = False
        urls: list[str] = []
        try:
            root = self._device("GetServices")
            urls = [(el.text or "").strip() for el in _findall(root, "XAddr")]
        except OnvifError:
            urls = []
        picked = _pick_media_url([u for u in urls if u])
        if picked:
            guess = picked
            self.media_from_device = True
        self._media_url = guess
        return guess

    def set_media_url(self, url: str) -> None:
        self._media_url = url

    # ------------------------------------------------------------------ 四个动作
    def get_device_information(self) -> dict[str, str]:
        root = self._device("GetDeviceInformation")
        keys = (
            ("Manufacturer", "manufacturer"),
            ("Model", "model"),
            ("FirmwareVersion", "firmware"),
            ("SerialNumber", "serial"),
        )
        return {out: _text_of(root, src) for src, out in keys}

    def get_profiles(self) -> list[OnvifProfile]:
        root = self._media("GetProfiles", "")
        profiles: list[OnvifProfile] = []
        for p in _findall(root, "Profiles"):
            token = p.get("token") or _text_of(p, "token")
            name = _text_of(p, "Name") or token
            enc = _find(p, "VideoEncoderConfiguration")
            enc_token = ""
            if enc is not None:
                enc_token = enc.get("token") or _text_of(enc, "token")
            if token:
                profiles.append(OnvifProfile(token, name, enc_token or token))
        return profiles

    def get_encoder_configuration(self, token: str) -> EncoderSettings:
        root = self._media(
            "GetVideoEncoderConfiguration",
            f"<a:ConfigurationToken>{token}</a:ConfigurationToken>",
        )
        cfg = _find(root, "Configuration")
        if cfg is None:
            raise OnvifError(f"{token} 没有编码配置应答")
        st = EncoderSettings(encoder_token=token)
        st.encoding = _text_of(cfg, "Encoding", "H264")
        st.width = _num(cfg, "Width")
        st.height = _num(cfg, "Height")
        st.frame_rate = _num(cfg, "FrameRateLimit")
        st.bitrate_kbps = _num(cfg, "BitrateLimit")
        st.name = _text_of(cfg, "Name")
        rc = _find(cfg, "RateControl")
        if rc is not None:
            st.encoding_interval = _num(rc, "EncodingInterval")
            if _find(rc, "ConstantBitrateRate") is not None:
                st.rate_mode = "C"
                st.bitrate_kbps = _num(rc, "ConstantBitrateRate", st.bitrate_kbps)
            elif _find(rc, "AverageBitrateRate") is not None:
                st.rate_mode = "V"
                st.bitrate_kbps = _num(rc, "AverageBitrateRate", st.bitrate_kbps)
                st.peak_bitrate_kbps = _num(rc, "PeakBitrateRate")
        gov = _find(cfg, "GovLength")
        if gov is not None and (gov.text or "").strip():
            st.gov_length = _num(cfg, "GovLength")
        profile = _find(cfg, "H264Profile")
        if profile is None:
            profile = _find(cfg, "H265Profile")
        if profile is not None and (profile.text or "").strip():
            st.codec_profile = (profile.text or "").strip()
        quality = _text_of(cfg, "Quality")
        if quality:
            try:
                st.quality = float(quality)
            except ValueError:
                st.quality = None
        st.encoding_actual = st.encoding
        priv = _private_video(cfg, token)
        if priv:
            # 厂商私有字段优先：TP-Link 的 tt:Encoding 恒为 H264 占位，真档位与
            # 码率模式只在这段 JSON 里（encode_type / bitrate_type）。注意
            # ``encoding`` 保持设备自报值不动——写回要原样回传，改的是展示值。
            enc = _norm_encoding(str(priv.get("encode_type") or ""))
            if enc:
                st.encoding_actual = enc
            mode = _rate_mode(str(priv.get("bitrate_type") or ""))
            if mode:
                st.rate_mode = mode
        return st

    def get_encoder_options(self, token: str) -> EncoderOptions:
        root = self._media(
            "GetVideoEncoderConfigurationOptions",
            f"<a:ConfigurationToken>{token}</a:ConfigurationToken>",
        )
        encodings: list[str] = []
        resolutions: list[tuple[int, int]] = []
        by_encoding: dict[str, list[tuple[int, int]]] = {}
        fps: list[int] = []
        fps_min, fps_max = 0, 0
        bitrate: tuple[int, int] | None = None
        for name, block, owner in _option_blocks(root):
            if name not in encodings:
                encodings.append(name)
            rows: list[tuple[int, int]] = []
            for ra in _findall(block, "ResolutionsAvailable"):
                w, h = _num(ra, "Width"), _num(ra, "Height")
                if w and h and (w, h) not in rows:
                    rows.append((w, h))
            if rows:
                bucket = by_encoding.setdefault(name, [])
                for r in rows:
                    if r not in bucket:
                        bucket.append(r)
                    if r not in resolutions:
                        resolutions.append(r)
            # 帧率：Media2 风格给枚举清单，Media1 给 Min/Max 区间，两种都收
            listed = _ints_in_text(owner.get("FrameRatesSupported", ""))
            rng = _find(block, "FrameRateRange")
            lo, hi = (_num(rng, "Min"), _num(rng, "Max")) if rng is not None else (0, 0)
            if listed:
                fps.extend(v for v in listed if v not in fps)
                lo = min(lo, min(listed)) if lo else min(listed)
                hi = max(hi, max(listed))
            elif lo and hi >= lo:
                fps.extend(v for v in range(lo, hi + 1) if v not in fps)
            if lo:
                fps_min = lo if not fps_min else min(fps_min, lo)
            if hi:
                fps_max = max(fps_max, hi)
            br = _find(block, "BitrateRange")
            if br is None:
                # Media1 把码率区间挂在 Options 下的 RateControl 里，不在编码块内
                br = _find(owner, "BitrateRange")
            if br is not None:
                b_lo, b_hi = _num(br, "Min"), _num(br, "Max")
                if b_lo and b_hi >= b_lo > 0:
                    bitrate = (
                        (b_lo, b_hi)
                        if bitrate is None
                        else (min(bitrate[0], b_lo), max(bitrate[1], b_hi))
                    )
        if not encodings and not resolutions and not bitrate:
            return EncoderOptions()
        out = EncoderOptions(encodings=encodings or ["H264"])
        if resolutions:
            out.resolutions = sorted(resolutions, key=lambda r: r[0] * r[1])
            out.resolutions_by_encoding = {
                k: sorted(v, key=lambda r: r[0] * r[1])
                for k, v in by_encoding.items()
            }
            out.source = "device"
        if fps:
            out.fps_supported = sorted(set(fps))
        if fps_min and fps_max >= fps_min:
            out.fps_range = (fps_min, fps_max)
            out.source = "device"
        if bitrate:
            out.bitrate_range = bitrate
            out.source = "device"
        return out

    def set_encoder_configuration(self, st: EncoderSettings) -> str:
        """把编码参数写回设备。

        实机（雄迈固件）只认它自己 Get 回来的形状，任何"标准扩展写法"都会被判
        ``ter:InvalidArgVal / ter:ConfigModify``，实测过的坑：

        * token 是 ``tt:Configuration`` 的**属性**，写成 ``<tt:token>`` 子元素被拒；
        * 子元素顺序固定为 Name → UseCount → Encoding → Resolution → Quality →
          RateControl → 编码块；
        * RateControl 用 Media1 三件套 ``FrameRateLimit / EncodingInterval /
          BitrateLimit``；``ConstantBitrateRate``、``AverageBitrateRate`` 是 Media2
          的字段，写进来就非法；
        * ``GovLength`` 与编码档位必须嵌在 ``tt:H264`` / ``tt:H265`` 块里，挂在顶层
          非法；
        * 不要额外塞 ``ForcePersistence``，成功形状里没有。

        所以这里不区分 CBR/VBR——Media1 的写入形状里压根没有这个概念，码率落在
        ``BitrateLimit`` 上，模式只在读取时用于回显。
        """
        if not st.encoder_token:
            raise OnvifError("缺少 encoder token，先读取再修改")
        if not st.width or not st.height or not st.frame_rate or not st.bitrate_kbps:
            raise OnvifError("分辨率/帧率/码率都必须有值，禁止往设备写 0")
        parts: list[str] = []
        if st.name:
            parts.append(f"<tt:Name>{st.name}</tt:Name>")
        parts.append("<tt:UseCount>1</tt:UseCount>")
        parts.append(f"<tt:Encoding>{st.encoding}</tt:Encoding>")
        parts.append(
            f"<tt:Resolution><tt:Width>{st.width}</tt:Width>"
            f"<tt:Height>{st.height}</tt:Height></tt:Resolution>"
        )
        if st.quality is not None:
            q = int(st.quality) if float(st.quality).is_integer() else st.quality
            parts.append(f"<tt:Quality>{q}</tt:Quality>")
        parts.append(
            "<tt:RateControl>"
            f"<tt:FrameRateLimit>{st.frame_rate}</tt:FrameRateLimit>"
            f"<tt:EncodingInterval>{int(st.encoding_interval)}</tt:EncodingInterval>"
            f"<tt:BitrateLimit>{st.bitrate_kbps}</tt:BitrateLimit>"
            "</tt:RateControl>"
        )
        block = _codec_block(st.encoding)
        if block:
            inner = []
            if st.gov_length:
                # 设备没报档位长度时不猜值（写 0 可能被当成"不关键帧"），
                # GovLength 在 Media1 里本就是可选元素
                inner.append(f"<tt:GovLength>{int(st.gov_length)}</tt:GovLength>")
            profile = st.codec_profile or "Main"
            inner.append(f"<tt:{block}Profile>{profile}</tt:{block}Profile>")
            parts.append(f"<tt:{block}>" + "".join(inner) + f"</tt:{block}>")
        body = (
            f'<tt:Configuration token="{st.encoder_token}" xmlns:tt="{_NS_TT}">'
            + "".join(parts)
            + "</tt:Configuration>"
        )
        root = self._media("SetVideoEncoderConfiguration", body)
        return _text_of(root, "token") or st.encoder_token
