"""ONVIF 实机探测：确认摄像头的服务端口、鉴权方式和码流参数值域。

IPG-85HE20PY 走雄迈方案，ONVIF 端口不保证是 80，且 WSSE/HTTP Digest
形态未知——先探测再配置。脚本会：
1. 逐个端口尝试 GetDeviceInformation（默认 80/2020/8000/8080/8888/8899）；
2. 在第一个通的端口上报告鉴权方式（wsse / http-digest）、media 服务地址；
3. 列出全部 Profiles 及各档的当前编码参数和设备自报的合法值域
   （按编码分别给出的分辨率档位、帧率档位、码率区间），供填 UI 或改配置参考。

用法：
    python tools/probe_onvif.py                 # 用 config/app.yaml 的 video 段
    python tools/probe_onvif.py 192.168.1.10    # 指定 IP
    python tools/probe_onvif.py 192.168.1.10 admin 密码   # 指定账号
    python tools/probe_onvif.py 192.168.1.10 admin --raw  # 打印设备原始应答
    python tools/probe_onvif.py 192.168.1.10 --sweep      # 端口/路径全扫，定位 endpoint

--raw 用于诊断"端口通但字段全空 / 0 个 Profile"：把每个 SOAP 请求的
原始 XML 应答头部打出来，直接看设备回的是不是标准 ONVIF Envelope。

--sweep 用于"ONVIF 端口/路径未知"：TCP 端口开放表 → WS-Discovery 自报
地址 → 各端口 GET / 指纹 → 常见 ONVIF 路径矩阵，一次性定位真 endpoint。
"""

from __future__ import annotations

import re
import select
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
DEPS = ROOT / ".deps"
if DEPS.is_dir() and str(DEPS) not in sys.path:
    sys.path.append(str(DEPS))

from hmi.core.config import load_config  # noqa: E402
from hmi.protocol.onvif import OnvifClient, OnvifError  # noqa: E402

DEFAULT_PORTS = (80, 2020, 8000, 8080, 8888, 8899)

# --sweep 候选：雄迈实机把 ONVIF 单独放在 8899，媒体私有协议在 34567
SWEEP_PORTS = (80, 554, 888, 3456, 3702, 8000, 8080, 8888, 8899, 9000, 34567)
ONVIF_PATHS = (
    "/onvif/device_service",
    "/onvif",
    "/onvif/devicemgmt",
    "/onvif/DeviceService",
    "/DeviceService",
    "/onvif/device",
)

_WSD_PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
  xmlns:a="http://www.w3.org/2005/08/addressing"
  xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
  xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <s:Header>
    <a:MessageID>urn:uuid:{mid}</a:MessageID>
    <a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>
    <a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>
  </s:Header>
  <s:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></s:Body>
</s:Envelope>"""


def _short(text: str, count: int = 120) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= count else flat[:count] + "…"



def dump_profile(cli: OnvifClient, token: str, label: str) -> None:
    print(f"\n--- Profile [{label}] token={token}")
    try:
        st = cli.get_encoder_configuration(token)
    except OnvifError as exc:
        print(f"  当前参数读取失败：{exc}")
        return
    print(
        f"  当前：{st.encoding} {st.width}x{st.height} "
        f"{st.frame_rate}fps {st.bitrate_kbps}kbps "
        f"模式={st.rate_mode or '?'} gov={st.gov_length or '?'}"
    )
    try:
        opt = cli.get_encoder_options(token)
    except OnvifError as exc:
        print(f"  值域读取失败：{exc}")
        return
    src = "设备实报" if opt.source == "device" else "保守默认（设备没回 Options）"
    print(f"  值域来源：{src}")
    print(f"  编码格式：{', '.join(opt.encodings) or '(未报)'}")
    if opt.resolutions_by_encoding:
        for enc in opt.encodings:
            rows = opt.resolutions_for(enc)
            tiers = ", ".join(f"{w}x{h}" for w, h in rows) or "(未报)"
            print(f"    {enc} 档位：{tiers}")
    else:
        labels = ", ".join(opt.resolution_labels()) or "(未报)"
        print(f"    全部档位：{labels}")
    if opt.fps_supported:
        print(
            f"  帧率档位：{opt.fps_range[0]}~{opt.fps_range[1]}"
            f"（离散 {len(opt.fps_supported)} 档，非连续区间）"
        )
    else:
        print(f"  帧率区间：{opt.fps_range[0]}~{opt.fps_range[1]}")
    print(
        f"  码率区间：{opt.bitrate_range[0]}~{opt.bitrate_range[1]}kbps"
    )


def _tcp_open(ip: str, port: int, timeout: float = 0.6) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((ip, port)) == 0


def _title_of(body: bytes) -> str:
    hit = re.search(rb"<title>(.*?)</title>", body, re.S | re.I)
    if hit is None:
        return ""
    return hit.group(1).decode("utf-8", "replace").strip()[:48]


def http_fingerprint(ip: str, port: int, timeout: float = 2.5) -> str:
    """GET / 看是谁在应答：相机网页、图传模块网页，还是只会说私有协议。"""
    try:
        req = urllib.request.Request(
            f"http://{ip}:{port}/", headers={"User-Agent": "probe-onvif"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(4096)
            server = resp.headers.get("Server") or "?"
            return f"HTTP {resp.status} Server={server} 标题={_title_of(body) or '(无)'}"
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code} Server={exc.headers.get('Server') or '?'}"
    except Exception as exc:  # 探测脚本：任何异常都只作报告，不许中断整轮扫描
        return f"非 HTTP 应答：{type(exc).__name__}: {_short(str(exc), 60)}"


def wsd_probe(ip: str, wait_s: float = 2.5) -> list[str]:
    """WS-Discovery：组播 + 单播发 Probe，收设备自报的 XAddrs（最权威的 endpoint）。"""
    msg = _WSD_PROBE.format(mid=uuid.uuid4()).encode()
    socks: list[socket.socket] = []
    free = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        free.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        free.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 5)
        free.bind(("0.0.0.0", 0))
        socks.append(free)
    except OSError:
        free.close()
    listen = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:  # 尽力占 3702 收组播回包；被别的程序占了就跳过
        listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listen.bind(("0.0.0.0", 3702))
        listen.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_ADD_MEMBERSHIP,
            socket.inet_aton("239.252.252.252") + socket.inet_aton("0.0.0.0"),
        )
        socks.append(listen)
    except OSError:
        listen.close()
    for sock in socks:
        for peer in (("239.252.252.252", 3702), (ip, 3702)):
            try:
                sock.sendto(msg, peer)
            except OSError:
                pass
    found: list[str] = []
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline and socks:
        ready, _, _ = select.select(socks, [], [], 0.3)
        for sock in ready:
            try:
                data, peer = sock.recvfrom(65535)
            except OSError:
                continue
            for blob in re.findall(rb"XAddrs>([^<]+)<", data):
                for url in blob.decode("utf-8", "replace").split():
                    found.append(f"{url}  ← 来自 {peer[0]}")
    for sock in socks:
        sock.close()
    return found


def try_onvif_path(
    ip: str, port: int, path: str, user: str, password: str
) -> tuple[bool, str]:
    """在 (端口, 路径) 上打一发 GetDeviceInformation，报告是不是真 ONVIF。"""
    cli = OnvifClient(
        ip, port=port, user=user, password=password, timeout_s=3.0, device_path=path
    )
    try:
        info = cli.get_device_information()
    except OnvifError as exc:
        return False, _short(str(exc))
    fields = " ".join(
        f"{label}={info.get(key) or '(空)'}"
        for label, key in (("厂商", "manufacturer"), ("型号", "model"), ("固件", "firmware"))
    )
    note = "" if any(info.values()) else "  ← 通了但字段全空"
    return True, f"鉴权={cli.auth_mode} {fields}{note}"


def run_sweep(ip: str, user: str, password: str) -> int:
    print(f"\n===== 扫描 {ip}：端口 → WS-Discovery → HTTP 指纹 → ONVIF 路径 =====")
    print("\n[1] TCP 端口")
    opened = [p for p in SWEEP_PORTS if _tcp_open(ip, p)]
    for p in SWEEP_PORTS:
        print(f"  {p:>6} {'开' if p in opened else '-'}")
    if not opened:
        print("一个端口都不开——IP 不对或链路未通。")
        return 1

    print("\n[2] WS-Discovery（组播 + 单播 3702，等 2.5 秒）")
    addrs = list(dict.fromkeys(wsd_probe(ip)))
    if addrs:
        for addr in addrs:
            print(f"  {addr}")
    else:
        print("  无应答（不代表设备没有 ONVIF，仍可按 IP 直连探测）")

    print("\n[3] HTTP 指纹（GET /）")
    for p in opened:
        print(f"  {p:>6} {http_fingerprint(ip, p)}")

    print("\n[4] ONVIF 路径矩阵（POST GetDeviceInformation）")
    hit: tuple[int, str] | None = None
    for p in opened:
        if p in (554, 3702, 34567):  # RTSP / WSD / 私有协议，不说 HTTP
            continue
        for path in ONVIF_PATHS:
            good, detail = try_onvif_path(ip, p, path, user, password)
            print(f"  {'✓' if good else ' '} :{p:<6}{path:<26} {detail}")
            if not good:
                continue
            if hit is None:
                hit = (p, path)
            break
    if hit is None:
        print("\n没有一条路径回 SOAP——ONVIF 未开启，或端口/路径都不在候选里。")
        return 1
    port, path = hit
    print(f"\n结论：ONVIF endpoint = http://{ip}:{port}{path}")
    print(f"把 config/app.yaml 的 video.onvif_port 填 {port}"
          + ("" if path == "/onvif/device_service" else f"（路径非默认：{path}）"))
    return 0


def dump_raw(cli: OnvifClient, head: int = 600) -> None:
    """打印本次运行捕获的设备原始 XML 应答（每条截前 head 字符）。"""
    for action, data in cli.trace or []:
        text = data.decode("utf-8", "replace")
        print(f"\n===== [{action}] {len(data)} 字节 =====")
        print(text if len(text) <= head else text[:head] + f"\n...(共 {len(text)} 字符)")


def main() -> int:
    cfg = load_config()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    raw = "--raw" in flags
    sweep = "--sweep" in flags
    ip = args[0] if len(args) > 0 else cfg.video.camera_ip
    user = args[1] if len(args) > 1 else cfg.video.user
    password = args[2] if len(args) > 2 else cfg.video.password
    if not ip:
        print("未指定 IP，且 config/app.yaml 的 video.camera_ip 为空")
        return 2
    if sweep:
        return run_sweep(ip, user, password)

    print(f"目标 {ip} 用户 {user!r}（端口扫描：{DEFAULT_PORTS}）")
    live: OnvifClient | None = None
    for port in DEFAULT_PORTS:
        cli = OnvifClient(ip, port=port, user=user, password=password,
                          timeout_s=4.0)
        if raw:
            cli.trace = []
        try:
            info = cli.get_device_information()
        except OnvifError as exc:
            print(f"[{port:>5}] {_short(str(exc))}")
            continue
        print(
            f"[{port:>5}] 通！鉴权={cli.auth_mode} "
            f"厂商={info.get('manufacturer', '?')} "
            f"型号={info.get('model', '?')} "
            f"固件={info.get('firmware', '?')}"
        )
        if raw:
            dump_raw(cli)
        if live is None:
            live = cli
    if live is None:
        print("\n所有端口都不通——确认摄像头已上电、IP 正确、网口桥接在位；"
              "也可先跑 --sweep（实机验证 WS-Discovery 组播能穿过 EWD611 空口，"
              "让设备自报 endpoint）。")
        return 1

    if raw:
        live.trace = []
    try:
        url = live.media_url()
        src = "设备实报" if live.media_from_device else "猜测兜底（GetServices 没报）"
        print(f"\nmedia 服务：{url}  [{src}]")
    except OnvifError as exc:
        print(f"\nGetServices 失败：{exc}")
    try:
        profiles = live.get_profiles()
    except OnvifError as exc:
        print(f"GetProfiles 失败：{exc}")
        if raw:
            dump_raw(live)
        return 1
    print(f"共 {len(profiles)} 个 Profile")
    for p in profiles:
        if p.encoder_token:
            dump_profile(live, p.encoder_token, p.name or p.token)
        else:
            print(f"\n--- Profile [{p.name or p.token}]：无 VideoEncoder 引用")
    if raw:
        dump_raw(live)
    print("\n把上面【端口 + 鉴权方式】记下来，填入 video.onvif_port 即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
