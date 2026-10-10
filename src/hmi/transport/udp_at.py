"""网口 AT 通道：复刻厂商 netat.exe 的 UDP 二进制成帧 + 发现握手协议。

协议事实（2026-10 用户实机完整抓包 capture0931 逐字节解码，netat.exe 同时刻
三条指令全部应答、本上位机裸 ASCII 版本全部超时，据此定案）：

- **UDP 载荷不是裸 ASCII**，而是 ``op(1B) | 00(1B) | len(1B) | dstMAC(6B) |
  srcMAC(6B) | ASCII(len B)``，头 15 字节，len 为其后 ASCII 的字节数；
- 操作码：``01`` 发现请求 / ``02`` 发现响应 / ``03`` AT 请求 / ``04`` AT 响应；
- 地址形态与上一版一致：目的仍是二层/三层全广播，UDP 双向同端口 56789，
  模块 IP 恒 ``0.0.0.0``——两个 MAC 字段是**应用层寻址**，模块靠 dst MAC
  判断"这条是点我的"，靠 src MAC 知道"该回给谁"；
- **前期必须握手**：netat.exe 每 ~500ms 重发一次发现请求（发现+保活），
  AT 请求的 dstMAC 必须填已发现模块的 MAC；裸 ASCII 或未经发现的帧，模块
  一概不理——这是上一版"重试 3/3 全超时"的根因；
- AT 文本**不带行尾**（抓包 ``at+txpower?`` 恰为 11 字节）；
- AT 响应**无回显**，ASCII 只有结果行 + ``OK``（``+TXPOWER:6dbm\\r\\nOK\\r\\n``），
  CRLF 只出现在响应行尾，交给 protocol 层按行切分。

本机 ID（src MAC）不是网卡真实 MAC：抓包显示 netat 用 ``fa-7e-b9-1f-58-5d``
（首字节本地管理位置 1），发包网卡却是 ``c4-ef-bb-7a-07-c8``，所以本通道
自选一个稳定的 6 字节标识（可用 ``udp_at.host_id`` 固定）。
"""

from __future__ import annotations

import os
import socket
import threading
import time
from typing import Any

from ..core.config import AtCfg, UdpAtCfg
from ..core.models import TransportState
from .base import ByteTransport

#: 抓包实锤：模块与 PC 双向共用这一个 UDP 端口。
DEFAULT_UDP_PORT = 56789
BROADCAST_ADDR = "255.255.255.255"

#: 帧头长度与单帧 ASCII 上限（len 字段只有 1 字节）。
HEADER_LEN = 15
MAX_TEXT_LEN = 255

OP_DISCOVER = 0x01
OP_DISCOVER_ACK = 0x02
OP_AT = 0x03
OP_AT_ACK = 0x04
OPS = (OP_DISCOVER, OP_DISCOVER_ACK, OP_AT, OP_AT_ACK)

BROADCAST_MAC = b"\xff\xff\xff\xff\xff\xff"


def parse_mac(text: str) -> bytes:
    """把 ``96-a1-3e-7d-83-88`` / ``96:a1:...`` / ``96a1...`` 解析成 6 字节。"""
    cleaned = str(text or "").strip().replace(":", "").replace("-", "").replace(" ", "")
    raw = bytes.fromhex(cleaned)
    if len(raw) != 6:
        raise ValueError(f"MAC 需为 6 字节，得到 {len(raw)} 字节：{text!r}")
    return raw


def format_mac(raw: bytes) -> str:
    """按 netat.exe 控制台的连字符小写形式输出，便于用户对照。"""
    return "-".join(f"{b:02x}" for b in raw)


def new_host_id() -> bytes:
    """自选一个稳定的 6 字节本机标识，首字节置"本地管理"位（与 netat 一致）。"""
    return bytes([(os.urandom(1)[0] & 0xFC) | 0x02]) + os.urandom(5)


def build_frame(op: int, dst: bytes, src: bytes, text: bytes = b"") -> bytes:
    """按抓包格式组帧：``op | 00 | len | dst6 | src6 | ASCII``。"""
    if len(text) > MAX_TEXT_LEN:
        raise ValueError(f"AT 文本 {len(text)} 字节，超出单帧上限 {MAX_TEXT_LEN}")
    return bytes([op, 0, len(text)]) + dst + src + text


def parse_frame(data: bytes) -> tuple[int, bytes, bytes, bytes] | None:
    """解析一帧；不是本协议的包（长度不足/操作码不认识/被截断）回 None。"""
    buf = bytes(data)
    if len(buf) < HEADER_LEN:
        return None
    op = buf[0]
    if op not in OPS:
        return None
    length = buf[2]
    if len(buf) < HEADER_LEN + length:
        return None
    return op, buf[3:9], buf[9:15], buf[HEADER_LEN : HEADER_LEN + length]


def list_local_ips() -> list[str]:
    """列出本机非回环 IPv4，供绑定网卡使用。"""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.168.1.1", 53))
        ips.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    return sorted(ip for ip in ips if not ip.startswith("127."))


class UdpAtTransport(ByteTransport):
    """与模块网口对接的 UDP 成帧通道：本类负责组帧/去帧与发现握手，AT 语义在 protocol 层。"""

    #: 本通道支持应用层定向：write(data, target_mac=...) 按帧头 dst MAC 选模块。
    supports_target = True

    def __init__(self, at_cfg: AtCfg, udp_cfg: UdpAtCfg, *, name: str = "udp_at") -> None:
        super().__init__(name)
        self.at = at_cfg
        self.cfg = udp_cfg
        self.sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ka_thread: threading.Thread | None = None
        self._recv_lock = threading.Lock()
        self.last_source = ""
        self.bound_addr = ("", 0)
        self.host_id = self._parse_cfg_mac(udp_cfg.host_id) or new_host_id()
        self._fixed_target = self._parse_cfg_mac(udp_cfg.target_mac)
        self._modules: dict[bytes, tuple[float, str]] = {}
        self._modules_lock = threading.Lock()
        self.discovery_sent = 0
        self.frames_bad = 0
        self.frames_foreign = 0
        self.frames_other_src = 0
        self._at_target = b""

    # ------------------------------------------------------------------ 起停
    def open(self) -> None:
        with self._lock:
            if self._state is TransportState.OPEN:
                return
            if not self.cfg.broadcast_scan and not self._fixed_target:
                raise RuntimeError(
                    "网口 AT 必须先发现模块才能定 AT 帧目标 MAC："
                    "udp_at.broadcast_scan 已关闭且 udp_at.target_mac 为空，无法打开通道"
                )
            self._set_state(TransportState.OPENING, "绑定广播端口")
            try:
                sock = self._make_socket()
            except OSError as exc:
                self._note_error(f"创建/绑定 UDP 广播套接字失败: {exc}")
                self._set_state(TransportState.ERROR, str(exc))
                raise
            self.sock = sock
            self.bound_addr = sock.getsockname()
            self._set_state(
                TransportState.OPEN,
                f"{self.bound_addr[0]}:{self.bound_addr[1]} -> 广播:{self._port()}"
                f"（本机 {format_mac(self.host_id)}）",
            )
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name=f"hmi-{self.name}-rx", daemon=True
            )
            self._thread.start()
            if self.cfg.broadcast_scan:
                self._ka_thread = threading.Thread(
                    target=self._keepalive_loop, name=f"hmi-{self.name}-ka", daemon=True
                )
                self._ka_thread.start()

    def close(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        ka_thread, self._ka_thread = self._ka_thread, None
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        for th in (thread, ka_thread):
            if th is not None and th.is_alive():
                th.join(timeout=1.0)
        self._set_state(TransportState.CLOSED, "已关闭")

    # ------------------------------------------------------------------ 收发
    def write(self, data: bytes, target_mac: bytes | str | None = None) -> int:
        """把 AT 文本按行切成 op3 帧，定向到目标模块发出；返回送出的 ASCII 字节数。

        ``target_mac`` 支持 ``96-a1-3e-7d-83-88`` 文本或 6 字节：显式给出即定向该
        模块（多模块场景由上层 UI 选择）；缺省时回退配置固定目标 → TTL 内在线的
        第一个模块 → 现场发现握手。
        """
        sock = self.sock
        if sock is None:
            raise OSError("UDP-AT 通道未打开")
        want = self._parse_target(target_mac)
        if want:
            # 定向目标必须"确认在线"才发：先查 TTL 内模块表，不在表里补一次
            # 定向握手；仍然拿不到就明确失败。绝不能把帧静默发给一台不存在的 MAC
            # 并顺手把 _at_target 设成它——那会让另一台活着的模块的 op4 应答被
            # frames_other_src 全部丢弃，表现就是"能发现两台但一台也读不到"。
            if want == BROADCAST_MAC:
                target = want
            else:
                target = want if self._has_module(want) else self._handshake(
                    self.cfg.probe_timeout_ms, want
                )
            if not target:
                raise OSError(
                    f"目标模块离线：{format_mac(want)}"
                    f"（{self._ttl():.1f}s 内未见其应答），已取消发送；"
                    "请在电台配置页重新“发现/刷新”或改选在线模块"
                )
        else:
            target = self._current_target() or self._handshake(self.cfg.probe_timeout_ms)
        if not target:
            target = BROADCAST_MAC
            self.log.warning(
                "发现握手未拿到模块 MAC，AT 帧目标暂用全 F（模块大概率不作答）——"
                "请确认电台已上电唤醒、本机网卡与模块同网段"
            )
        # 记录本轮期望的应答源，收线程据此丢弃其它模块的迟到/串扰 op4。
        self._at_target = b"" if target == BROADCAST_MAC else target
        written = 0
        for line in self._split_lines(data):
            try:
                frame = build_frame(OP_AT, target, self.host_id, line)
            except ValueError as exc:
                raise OSError(str(exc)) from exc
            with self._recv_lock:
                sock.sendto(frame, (BROADCAST_ADDR, self._port()))
            self._note_write(frame)
            written += len(line)
        return written

    def discover(self, timeout_ms: int | None = None) -> int:
        """主动做一次发现握手，返回在线模块数；模块 MAC 列表见 ``discovered_macs()``。

        通道已打开时只等收线程把 op2 递进模块表（不自己 recvfrom，避免抢包）；
        未打开时临时建套接字自问自收，供 tools/probe_udp_at.py 与"探测"按钮使用。
        """
        timeout_ms = timeout_ms or self.cfg.probe_timeout_ms
        if self.is_open():
            deadline = time.monotonic() + timeout_ms / 1000.0
            self._send_discovery()
            while not self._modules and time.monotonic() < deadline:
                self._send_discovery()
                self._stop.wait(0.1)
            return len(self._modules)
        try:
            sock = self._make_socket()
        except OSError as exc:
            self.log.warning("探测套接字失败: %s", exc)
            return 0
        found: set[bytes] = set()
        try:
            deadline = time.monotonic() + timeout_ms / 1000.0
            frame = build_frame(OP_DISCOVER, BROADCAST_MAC, self.host_id)
            while time.monotonic() < deadline:
                try:
                    sock.sendto(frame, (BROADCAST_ADDR, self._port()))
                except OSError as exc:
                    self.log.warning("发现包发送失败: %s", exc)
                    break
                stop_at = time.monotonic() + 0.1
                while time.monotonic() < stop_at:
                    try:
                        data, addr = sock.recvfrom(2048)
                    except TimeoutError:
                        break
                    except OSError:
                        return len(found)
                    parsed = parse_frame(data)
                    if parsed is None:
                        continue
                    op, dst, src, _ = parsed
                    if op is not OP_DISCOVER_ACK or dst != self.host_id:
                        continue
                    found.add(src)
                    self._remember_module(src, addr[0])
        finally:
            sock.close()
        if found:
            self.log.info("发现 %d 个模块：%s", len(found), self.discovered_macs())
        else:
            self.log.warning("发现握手无模块响应（op2 未收到）")
        return len(found)

    def discovered_macs(self) -> list[str]:
        with self._modules_lock:
            return [format_mac(mac) for mac in self._modules]

    def modules(self) -> list[dict[str, Any]]:
        """带在线状态的模块表：mac/online/seen_ago_s/source_ip，按发现先后排序。"""
        now = time.monotonic()
        ttl = self._ttl()
        with self._modules_lock:
            items = list(self._modules.items())
        out: list[dict[str, Any]] = []
        for mac, (seen, ip) in items:
            out.append(
                {
                    "mac": format_mac(mac),
                    "online": (now - seen) <= ttl,
                    "seen_ago_s": round(max(0.0, now - seen), 1),
                    "source_ip": ip,
                }
            )
        return out

    def info(self) -> dict[str, Any]:
        base = super().info()
        target = self._current_target()
        base.update(
            {
                "broadcast": BROADCAST_ADDR,
                "port": self._port(),
                "bind": f"{self.bound_addr[0]}:{self.bound_addr[1]}",
                "host_id": format_mac(self.host_id),
                "target_mac": format_mac(target) if target else "",
                "modules": self.discovered_macs(),
                "module_status": self.modules(),
                "discovery_sent": self.discovery_sent,
                "discovery_interval_ms": int(self.cfg.discovery_interval_ms),
                "frames_bad": self.frames_bad,
                "frames_foreign": self.frames_foreign,
                "frames_other_src": self.frames_other_src,
                "local_ips": list_local_ips(),
                "last_source": self.last_source,
            }
        )
        return base

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _parse_cfg_mac(value: str) -> bytes:
        text = str(value or "").strip()
        if not text:
            return b""
        try:
            return parse_mac(text)
        except ValueError:
            return b""

    @classmethod
    def _parse_target(cls, value: bytes | str | None) -> bytes:
        """把 write(target_mac=...) 的入参清洗成 6 字节；None/空/非法一律回退 b""。"""
        if value is None:
            return b""
        if isinstance(value, (bytes, bytearray)):
            raw = bytes(value)
            return raw if len(raw) == 6 else b""
        return cls._parse_cfg_mac(str(value))

    def _ttl(self) -> float:
        """模块"在线"判定窗口：至少 2s，或 4 个发现节拍。"""
        return max(2.0, max(50, int(self.cfg.discovery_interval_ms)) / 1000.0 * 4)

    def _has_module(self, mac: bytes) -> bool:
        now = time.monotonic()
        with self._modules_lock:
            seen = self._modules.get(mac)
        return seen is not None and (now - seen[0]) <= self._ttl()

    def _port(self) -> int:
        return int(self.cfg.target_port) or DEFAULT_UDP_PORT

    def _make_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        local_ip = self.cfg.local_bind_ip or "0.0.0.0"
        local_port = int(self.cfg.local_port) or DEFAULT_UDP_PORT
        sock.bind((local_ip, local_port))
        sock.settimeout(0.2)
        return sock

    @staticmethod
    def _split_lines(data: bytes) -> list[bytes]:
        """剥掉行尾并按行拆分——抓包实锤：op3 的 ASCII 不带 CRLF。"""
        text = data.decode("utf-8", "replace").replace("\r", "\n")
        out: list[bytes] = []
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped:
                out.append(stripped.encode("utf-8", "replace"))
        return out

    def _keepalive_loop(self) -> None:
        """复刻 netat.exe：按 ~500ms 节拍重发发现请求，兼作保活。"""
        interval = max(0.05, int(self.cfg.discovery_interval_ms) / 1000.0)
        while not self._stop.is_set():
            self._send_discovery()
            self._stop.wait(interval)

    def _send_discovery(self) -> None:
        sock = self.sock
        if sock is None:
            return
        frame = build_frame(OP_DISCOVER, BROADCAST_MAC, self.host_id)
        try:
            with self._recv_lock:
                sock.sendto(frame, (BROADCAST_ADDR, self._port()))
            self.discovery_sent += 1
        except OSError as exc:
            self._note_error(f"发现包发送失败: {exc}")

    def _handshake(self, timeout_ms: int, want: bytes = b"") -> bytes:
        """发 AT 前若还没发现模块，就地等一轮 op2（依赖收线程投递模块表）。

        ``want`` 非空时只等该 MAC 出现在模块表（多模块定向场景），否则等任意在线模块。
        """
        if not self.cfg.broadcast_scan:
            return self._fixed_target
        deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000.0
        self._send_discovery()
        while time.monotonic() < deadline:
            if want:
                if self._has_module(want):
                    return want
            else:
                target = self._current_target()
                if target:
                    return target
            self._stop.wait(0.05)
        if want:
            return want if self._has_module(want) else b""
        return self._current_target()

    def _current_target(self) -> bytes:
        if self._fixed_target:
            return self._fixed_target
        now = time.monotonic()
        ttl = self._ttl()
        with self._modules_lock:
            for mac, (seen, _ip) in self._modules.items():
                if now - seen <= ttl:
                    return mac
        return b""

    def _remember_module(self, mac: bytes, ip: str) -> None:
        if not mac:
            return
        with self._modules_lock:
            fresh = mac not in self._modules
            self._modules[mac] = (time.monotonic(), ip)
        self.last_source = ip
        if fresh:
            self.log.info("发现模块 %s（响应源 %s）", format_mac(mac), ip)

    def _loop(self) -> None:
        while not self._stop.is_set():
            sock = self.sock
            if sock is None:
                break
            try:
                data, addr = sock.recvfrom(2048)
            except TimeoutError:
                continue
            except OSError as exc:
                if self._stop.is_set():
                    # 关闭时 socket 被并发 close，recvfrom 抛 10038 属正常退出路径。
                    self.log.debug("收包线程随关闭退出：%s", exc)
                    break
                self._note_error(f"UDP 收包异常: {exc}")
                self._set_state(TransportState.ERROR, str(exc))
                break
            parsed = parse_frame(data)
            if parsed is None:
                self.frames_bad += 1
                self.log.debug("忽略非本协议包（%d 字节，首字节 0x%02X）", len(data), data[0])
                continue
            op, dst, src, text = parsed
            if op is OP_DISCOVER:
                continue
            if dst != self.host_id:
                self.frames_foreign += 1
                self.log.debug("忽略发给其它主机的帧 op=0x%02X dst=%s", op, format_mac(dst))
                continue
            if op is OP_DISCOVER_ACK:
                self._remember_module(src, addr[0])
                continue
            if op is not OP_AT_ACK:
                continue
            self.last_source = addr[0]
            expected = self._at_target
            if expected and src != expected:
                self.frames_other_src += 1
                self.log.debug(
                    "丢弃未选中模块的 op4：src=%s（当前目标 %s）",
                    format_mac(src), format_mac(expected),
                )
                continue
            self._emit_bytes(text)
        self._set_state(TransportState.CLOSED, "UDP 读线程退出")
