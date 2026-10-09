"""AT 行的切分与分类：把字节流变成结构化行，再判定 echo/值行/状态行/杂散行。

只依赖标准库，可被 at_client、日志回放、单元测试共同使用。

手册里的响应形态并不统一，这里全部兼容：
- ``+NAME:value``（多数查询）
- ``+CONNECTED`` / ``+DISCONNECT``（AT+CONN_STATE，无冒号也无 OK）
- ``+AT+CHAN_LIST:8680,8700``（手册 6.3.8 的响应名带 AT+ 前缀）
- ``+TXPOWER:20dbm``（带单位后缀）
- ``OK`` / ``ERROR`` / ``sta:xx is not exist``（自定义失败文本）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

KIND_ECHO = "echo"
KIND_VALUE = "value"
KIND_STATUS = "status"
KIND_OTHER = "other"

#: classify_chunk() 的两种归类（值保持 "at"/"pt"，与通道层统计口径一致）
CHUNK_AT = "at"
CHUNK_PASS = "pt"

STATUS_OK = "OK"
STATUS_ERROR = "ERROR"

_FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?")
_DASH_PAIR_RE = re.compile(r"^\s*(-?\d+)\s*-\s*(-?\d+)\s*$")

LINE_ENDINGS: dict[str, bytes] = {
    "none": b"",
    "cr": b"\r",
    "lf": b"\n",
    "crlf": b"\r\n",
    "sp": b" ",
    "at": b"AT",
}


def ending_bytes(name: str) -> bytes:
    return LINE_ENDINGS.get((name or "crlf").strip().lower(), b"\r\n")


class LineSplitter:
    """增量行切分：\\r / \\n / \\r\\n 混用都能正确断行。"""

    def __init__(self, max_len: int = 4096) -> None:
        self._buf = bytearray()
        self.max_len = int(max_len)

    def feed(self, data: bytes) -> list[str]:
        out: list[str] = []
        for b in data:
            if b in (0x0D, 0x0A):
                if self._buf:
                    out.append(self._take())
            elif b == 0xFF or b == 0x00:
                continue
            else:
                self._buf.append(b)
                if len(self._buf) >= self.max_len:
                    out.append(self._take())
        return out

    def _take(self) -> str:
        line = self._buf.decode("utf-8", "replace").strip()
        self._buf.clear()
        return line

    def pending(self) -> bytes:
        return bytes(self._buf)

    def clear(self) -> None:
        self._buf.clear()


@dataclass
class AtLine:
    kind: str = KIND_OTHER
    name: str = ""
    value: str = ""
    status: str = ""
    text: str = ""
    raw: bytes = field(default=b"")

    @property
    def is_ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def is_error(self) -> bool:
        return self.status == STATUS_ERROR

    def payload(self) -> str:
        """值行优先返回 value，没有冒号时退回整行文本（如 +CONNECTED）。"""
        return self.value if self.value else self.text.lstrip("+").strip()


def normalize_name(text: str) -> str:
    """把 ``+AT+CHAN_LIST:..`` / ``AT+MODE`` / ``+RSSI`` 归一成大写参数名。"""
    s = (text or "").strip().lstrip("+").strip()
    if s.upper().startswith("AT+"):
        s = s[3:]
    return s.partition(":")[0].strip().upper()


def split_value_line(text: str) -> tuple[str, str]:
    """把值行拆成 ``(规范参数名, 正文)``，兼容冒号与空格两种分隔写法。

    电台有两种写法：响应行是 ``+NAME:值``，自发上报是 ``+NAME 值``
    （``+CONNECTED``、``+PAIR_SUCCESS 00:11:22:33:44:55``）。正文里带冒号（MAC
    地址）时，按首个冒号切会把名字切成 ``PAIR_SUCCESS 00``，上层再也认不出这是
    配对结果，所以只在"名字段不含空格"时才认冒号。
    """
    body = str(text or "").strip()
    if body.startswith("+"):
        body = body[1:]
    head, sep, value = body.partition(":")
    if not sep or " " in head:
        head, _space, value = body.partition(" ")
    return normalize_name(head), value.strip()


def classify(
    line: str,
    ok_tokens: tuple[str, ...] = (STATUS_OK,),
    err_tokens: tuple[str, ...] = (STATUS_ERROR, "ERR", "FAIL"),
    command: str | None = None,
) -> AtLine:
    """把一行文本判定为 AtLine。command 为当前对话指令，用于识别回显。"""
    text = (line or "").strip()
    if not text:
        return AtLine(kind=KIND_OTHER, text="")
    upper = text.upper()
    if command is not None and upper == command.strip().upper():
        return AtLine(kind=KIND_ECHO, text=text)
    for token in ok_tokens:
        if upper == str(token).strip().upper():
            return AtLine(kind=KIND_STATUS, status=STATUS_OK, text=text)
    for token in err_tokens:
        if upper.startswith(str(token).strip().upper()):
            return AtLine(kind=KIND_STATUS, status=STATUS_ERROR, text=text)
    if text.startswith("+"):
        name, value = split_value_line(text)
        return AtLine(kind=KIND_VALUE, name=name, value=value, text=text)
    return AtLine(kind=KIND_OTHER, text=text)


def as_text(value: object) -> str:
    """把响应值转成文本，**只有 ``None`` 才算空**。

    解析器原先普遍写 ``str(text or "")``，于是 ``TXPOWER=0``、``ACKTMO=0``、
    ``EVM=0.0``、``CONN_STATE=0`` 这些合法零值全被吞成空串，UI 上显示为空白而不是 0。
    """
    return "" if value is None else str(value)


def parse_int(text: object) -> int | None:
    """从 ``20dbm`` / ``-72.4`` 这类响应里取整数值，失败返回 None。"""
    if isinstance(text, bool):
        return int(text)
    m = re.search(r"-?\d+", as_text(text))
    return int(m.group()) if m else None


def parse_float(text: object) -> float | None:
    if isinstance(text, bool):
        return float(text)
    m = _FLOAT_RE.search(as_text(text))
    return float(m.group()) if m else None


def parse_int_list(text: object) -> list[int]:
    """整数列表：兼容 ``1,2,3`` / ``863500-928000`` / ``1 2 3`` / ``1;2;3``。"""
    raw = as_text(text).strip()
    if not raw:
        return []
    dash = _DASH_PAIR_RE.match(raw)
    if dash:
        return [int(dash.group(1)), int(dash.group(2))]
    out: list[int] = []
    for tok in re.split(r"[,;/\s]+", raw):
        if not tok:
            continue
        try:
            out.append(int(float(tok)))
        except ValueError:
            continue
    return out


def parse_bool_state(text: object) -> bool | None:
    """连接状态类响应：CONNECTED/1/YES -> True，DISCONNECT/0/NO -> False。"""
    if isinstance(text, bool):
        return text
    token = normalize_name(as_text(text)) or as_text(text).strip().upper()
    if not token:
        return None
    if token in ("CONNECTED", "CONNECT", "1", "YES", "ON", "UP", "TRUE"):
        return True
    if token in ("DISCONNECT", "DISCONNECTED", "DISCONNECTING", "0", "NO", "OFF", "DOWN", "FALSE"):
        return False
    if "DISCONNECT" in token or "UNCONNECT" in token:
        return False
    if "CONNECT" in token:
        return True
    num = parse_int(token)
    return None if num is None else num > 0


def parse_kv_pairs(text: object) -> dict[str, str]:
    """把 ``mode=ap,bw=8,chan=8680`` 或空格分隔的 ``k=v`` 解析成字典。"""
    raw = as_text(text).strip().lstrip("+").strip()
    out: dict[str, str] = {}
    if "=" not in raw:
        return out
    for tok in re.split(r"[,;\s]+", raw):
        if "=" not in tok:
            continue
        key, _, val = tok.partition("=")
        key = key.strip().lower()
        if key:
            out[key] = val.strip()
    return out


def is_at_text_bytes(data: bytes) -> bool:
    """这段字节是否全由可打印 ASCII（允许 \\r\\n）组成——"文本 or 二进制"粗筛。

    电台的 AT 自述行（``+MODE:ap`` / ``OK`` / ``ERROR``）和命令回显都是纯文本；
    反过来，只要出现 0x00/0xFF 这类控制字节或非 ASCII，就一定不是电台在说话。
    AT 请求窗口内部分流（见 channel.ChannelDemux）用它做粗筛，空块返回 False。
    """
    if not data:
        return False
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return False
    for ch in text:
        if ch in "\r\n":
            continue
        if not 0x20 <= ord(ch) <= 0x7E:
            return False
    return True


def classify_chunk(data: bytes, markers: tuple[str, ...] = ("+", "AT+")) -> str:
    """判断一段原始字节更像 AT 文本还是透传数据（共享 485 通道的分流依据）。

    规则保守：必须是纯可打印 ASCII（允许 \\r\\n，见 is_at_text_bytes），且
    **任一非空行**以 ``+`` / ``AT+`` / OK / ERROR 这些电台自述前缀开头，才判成 AT。
    带 0x00/0xFF 的二进制帧、以及设备发的 ``@DEV ...`` 一律判成透传。
    """
    if not is_at_text_bytes(data):
        return CHUNK_PASS
    text = data.decode("ascii")
    upper = text.upper()
    for raw in upper.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        for marker in markers:
            token = marker.strip().upper()
            if token and line.startswith(token):
                return CHUNK_AT
    return CHUNK_PASS


def format_at(name: str, value: str | None = None, query: bool = False) -> str:
    """拼出 AT 命令行（不含行尾）。query=True 走手册的 ``AT+X?`` 形式。"""
    if query:
        return f"AT+{name}?"
    if value is None or value == "":
        return f"AT+{name}"
    return f"AT+{name}={value}"


def split_command(text: str) -> tuple[str, str | None, bool]:
    """反解一条 AT 行 -> (参数名, 值, 是否查询)；非 AT 行返回 ("", None, False)。"""
    s = (text or "").strip()
    if not s.upper().startswith("AT+"):
        return "", None, False
    body = s[3:]
    if body.endswith("?"):
        return body[:-1].strip().upper(), None, True
    head, sep, value = body.partition("=")
    if sep == "":
        return head.strip().upper(), None, False
    return head.strip().upper(), value, False
