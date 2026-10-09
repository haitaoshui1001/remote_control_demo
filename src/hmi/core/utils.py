"""通用工具：十六进制、时间、速率计量、环形缓冲、CRC。

本模块不依赖任何第三方库，供全项目复用。
"""

from __future__ import annotations

import binascii
import re
import time
from collections import deque
from collections.abc import Iterable, Iterator
from datetime import datetime

_HEX_CLEAN_RE = re.compile(r"(0[xX][0-9a-fA-F]{1,2}|[0-9a-fA-F]{2})")


def now_ms() -> float:
    """单调毫秒时间戳，用于测 RTT 与超时。"""
    return time.monotonic() * 1000.0


def epoch_ms() -> float:
    return time.time() * 1000.0


def stamp(ts: float | None = None, with_ms: bool = True) -> str:
    """把 epoch 秒格式化为可读时间串。"""
    dt = datetime.fromtimestamp(ts / 1000.0 if ts is not None else time.time())
    return dt.strftime("%H:%M:%S.%f")[:-3] if with_ms else dt.strftime("%H:%M:%S")


def hex_to_bytes(text: str) -> bytes:
    """宽容解析十六进制串：允许空格/逗号/换行/0x 前缀/\\x41 写法。

    奇数个裸 hex 字符时右侧补 0，与常见调试助手行为一致。
    """
    if text is None:
        return b""
    body = text.strip()
    if not body:
        return b""
    body = body.replace("\\x", " ").replace("0x", " ").replace("0X", " ")
    body = re.sub(r"[\s,;:]+", " ", body).strip()
    tokens = body.split(" ")
    out = bytearray()
    for tok in tokens:
        if not tok:
            continue
        if not re.fullmatch(r"[0-9a-fA-F]+", tok):
            raise ValueError(f"非法十六进制片段: {tok!r}")
        if len(tok) % 2 == 1:
            tok = tok + "0"
        out.extend(binascii.unhexlify(tok))
    return bytes(out)


def bytes_to_hex(data: bytes, group: int = 1, upper: bool = True) -> str:
    """bytes -> 分组十六进制串，group=2 时输出 '4142 4344'。"""
    if data is None:
        return ""
    h = data.hex().upper() if upper else data.hex().lower()
    if group <= 1:
        return " ".join(h[i : i + 2] for i in range(0, len(h), 2))
    size = group * 2
    return " ".join(h[i : i + size] for i in range(0, len(h), size))


def ascii_repr(data: bytes, limit: int = 400) -> str:
    """可打印化表示，不可打印字符转 '.'，用于收发窗口。"""
    if not data:
        return ""
    chars = []
    for b in data[:limit]:
        chars.append(chr(b) if 32 <= b < 127 else ".")
    tail = "" if len(data) <= limit else f"...(+{len(data) - limit})"
    return "".join(chars) + tail


def to_bytes(payload: str, mode: str = "hex") -> bytes:
    """按模板模式编码指令：hex / ascii / utf8。"""
    if payload is None:
        return b""
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    mode = (mode or "hex").lower()
    if mode == "hex":
        return hex_to_bytes(payload)
    if mode in ("ascii", "utf8", "text", "str"):
        text = payload
        text = _expand_escapes(text)
        return text.encode("ascii" if mode == "ascii" else "utf-8", errors="replace")
    raise ValueError(f"未知编码模式: {mode}")


def _expand_escapes(text: str) -> str:
    """支持 \\r \\n \\t \\\\xHH 转义，便于书写 ASCII 指令。"""
    repl = {"\\r": "\r", "\\n": "\n", "\\t": "\t", "\\\\": "\\"}
    out, i = [], 0
    while i < len(text):
        matched = False
        for k, v in repl.items():
            if text.startswith(k, i):
                out.append(v)
                i += len(k)
                matched = True
                break
        if not matched:
            out.append(text[i])
            i += 1
    return "".join(out)


def add_crlf(payload: bytes, suffix: str = "crlf") -> bytes:
    """按设置追加结束符，none/crlf/cr/lf/sp/brackets。"""
    suffix = (suffix or "none").lower()
    table = {
        "none": b"",
        "crlf": b"\r\n",
        "cr": b"\r",
        "lf": b"\n",
        "sp": b" ",
        "at": b"AT",
    }
    if suffix not in table:
        raise ValueError(f"未知结束符: {suffix}")
    return payload + table[suffix]


def crc16_modbus(data: bytes) -> int:
    """标准 Modbus CRC16，返回低字节在前需要调用方处理。"""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def append_crc16(data: bytes, low_first: bool = True) -> bytes:
    crc = crc16_modbus(data)
    lo, hi = crc & 0xFF, (crc >> 8) & 0xFF
    return data + bytes([lo, hi]) if low_first else data + bytes([hi, lo])


def verify_crc16(data: bytes, low_first: bool = True) -> bool:
    if len(data) < 3:
        return False
    body, tail = data[:-2], data[-2:]
    crc = crc16_modbus(body)
    expect = (
        bytes([crc & 0xFF, (crc >> 8) & 0xFF])
        if low_first
        else bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    )
    return tail == expect


def safe_int(text: object, default: int | None = None) -> int | None:
    """从响应串中稳健取整数，失败返回 default。"""
    if isinstance(text, bool):
        return int(text)
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        m = re.search(r"-?\d+", "" if text is None else str(text))
        return int(m.group()) if m else default


class RateMeter:
    """滑动窗口速率计量器（字节/秒、帧/秒）。"""

    def __init__(self, window_s: float = 5.0) -> None:
        self.window_s = float(window_s)
        self._items: deque[tuple[float, int]] = deque(maxlen=20000)

    def add(self, amount: int = 1, ts: float | None = None) -> None:
        self._items.append((ts if ts is not None else time.monotonic(), int(amount)))

    def rate(self, ts: float | None = None) -> float:
        ref = ts if ts is not None else time.monotonic()
        cutoff = ref - self.window_s
        while self._items and self._items[0][0] < cutoff:
            self._items.popleft()
        span = max(ref - cutoff, 1e-6)
        total = sum(v for _, v in self._items)
        return total / span

    def count(self) -> int:
        return len(self._items)


class RingBuffer:
    """固定容量环形缓冲，带单调序号，供 UI 增量拉取近期报文。"""

    def __init__(self, maxlen: int = 2000) -> None:
        self._buf: deque[tuple[int, object]] = deque(maxlen=max(1, int(maxlen)))
        self._next_id = 0

    def push(self, item: object) -> int:
        self._next_id += 1
        self._buf.append((self._next_id, item))
        return self._next_id

    def snapshot(self) -> list:
        return [it for _, it in self._buf]

    def iter_new(self, seen_id: int) -> Iterator[tuple[int, object]]:
        for idx, it in self._buf:
            if idx > seen_id:
                yield idx, it

    def last_id(self) -> int:
        return self._buf[-1][0] if self._buf else 0

    def clear(self) -> None:
        self._buf.clear()

    def __len__(self) -> int:
        return len(self._buf)


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024.0:
            return f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"


def parse_bool(text: object, default: bool = False) -> bool:
    s = str(text).strip().lower()
    if s in ("1", "true", "on", "yes", "ok"):
        return True
    if s in ("0", "false", "off", "no", "none"):
        return False
    return default


def flatten(values: Iterable[object]) -> list[object]:
    out: list[object] = []
    for v in values:
        if isinstance(v, (list, tuple, set)):
            out.extend(flatten(v))
        else:
            out.append(v)
    return out
