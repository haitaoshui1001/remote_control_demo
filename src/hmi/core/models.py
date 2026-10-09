"""契约层数据模型。

所有层的函数签名只使用本模块的数据类，避免 UI 直接依赖硬件对象。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class TransportState(StrEnum):
    CLOSED = "closed"
    OPENING = "opening"
    OPEN = "open"
    ERROR = "error"


class AtChannel(StrEnum):
    SERIAL = "serial"
    UDP = "udp"


class LinkRole(StrEnum):
    AP = "ap"
    STA = "sta"
    GROUP = "group"
    APSTA = "apsta"


class Endpoint(StrEnum):
    LOCAL = "local"
    REMOTE = "remote"


class Direction(StrEnum):
    TX = "tx"
    RX = "rx"


class AlarmLevel(StrEnum):
    INFO = "info"
    WARN = "warn"
    CRIT = "crit"


@dataclass
class FrameMeta:
    """一帧视频的元信息（在解码线程产生，随帧发布）。"""

    width: int = 0
    height: int = 0
    codec: str = ""
    pix_fmt: str = ""
    pts: int | None = None
    pts_time: float | None = None
    dts_time: float | None = None
    key_frame: bool = False
    capture_ts: float = field(default_factory=time.time)


@dataclass
class VideoStats:
    """视频链路运行指标，驱动 OSD 与状态栏。"""

    connected: bool = False
    url: str = ""
    resolution: str = ""
    codec: str = ""
    fps_decode: float = 0.0
    fps_render: float = 0.0
    bitrate_kbps: float = 0.0
    frames_decoded: int = 0
    frames_rendered: int = 0
    frames_dropped: int = 0
    frames_merged: int = 0
    frames_skipped: int = 0
    decode_ms: float = 0.0
    end_to_end_ms: float = 0.0
    last_frame_ts: float = 0.0
    last_frame_age_ms: float = 0.0
    reconnects: int = 0
    errors: int = 0
    last_error: str = ""
    stream_index: int | None = None


@dataclass
class AtExchange:
    """一次 AT 交互的完整记录（含未请求上报）。"""

    line: str = ""
    kind: str = "exec"
    name: str = ""
    ts: float = field(default_factory=time.time)
    rtt_ms: float = 0.0
    ok: bool = True
    response: list[str] = field(default_factory=list)
    channel: str = ""
    endpoint: str = ""
    error: str = ""

    def text(self) -> str:
        return "\r\n".join([self.line] + self.response)


@dataclass
class RadioParams:
    """电台可配置参数集合，字段名与 AT 指令一一对应。

    值语义：None 表示"未读取/不下发"，避免下发用户未触碰的字段。
    """

    mode: str | None = None
    ssid: str | None = None
    keymgmt: str | None = None
    psk: str | None = None
    r_ssid: str | None = None
    r_psk: str | None = None
    bss_bw: int | None = None
    freq_range: list[int] | None = None
    chan_list: list[int] | None = None
    txpower: int | None = None
    tx_mcs: int | None = None
    acktmo: int | None = None
    heart_int: int | None = None
    group_addr: str | None = None
    group_aid: int | None = None
    sysdbg_lmac: int | None = None
    sysdbg_wnb: int | None = None
    raw: dict[str, str] = field(default_factory=dict)

    def touched(self) -> dict[str, Any]:
        """筛出"确实要下发"的字段。

        空串与空集合也算没填：读一个写保护字段（``R_PSK`` 这类密码）时模块回的是空值，
        留着它 ``plan()`` 会生成 ``AT+R_PSK=`` 这种非法行，一处校验不过就把整批下发带崩。
        真要清空某个字段，请在界面上显式下发该字段的"清除"动作，而不是靠留白表达。
        """
        out: dict[str, Any] = {}
        for key, value in asdict(self).items():
            if key == "raw" or value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            if isinstance(value, list | tuple | dict) and not value:
                continue
            out[key] = value
        return out


@dataclass
class StaInfo:
    """AP 侧看到的某个从机节点信息。"""

    index: int = 0
    mac: str = ""
    rssi_dbm: float | None = None
    evm_db: float | None = None
    tx_mcs: int | None = None
    rx_mcs: int | None = None
    tx_packets: int | None = None
    rx_packets: int | None = None
    lost_packets: int | None = None
    raw: str = ""


@dataclass
class LinkSnapshot:
    """信道关键参数快照，一次轮询产生一条。"""

    ts: float = field(default_factory=time.time)
    endpoint: str = Endpoint.LOCAL.value
    role: str | None = None
    connected: bool | None = None
    rssi_dbm: float | None = None
    evm_db: float | None = None
    tx_mcs: int | None = None
    bandwidth_mhz: int | None = None
    freq_mhz: float | None = None
    freq_range: list[int] | None = None
    chan_list: list[int] | None = None
    ssid: str | None = None
    keymgmt: str | None = None
    txpower_dbm: float | None = None
    acktmo_us: int | None = None
    heart_int_ms: int | None = None
    sta_count: int = 0
    sta_list: list[StaInfo] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    degraded: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass
class ByteRecord:
    """透传/AT 通道的一条原始报文记录。"""

    ts: float = field(default_factory=time.time)
    direction: str = Direction.RX.value
    data: bytes = b""
    channel: str = ""
    endpoint: str = ""
    note: str = ""
    kind: str = "raw"

    def hex(self) -> str:
        from .utils import bytes_to_hex

        return bytes_to_hex(self.data)

    def ascii(self) -> str:
        from .utils import ascii_repr

        return ascii_repr(self.data)


@dataclass
class PassthroughStats:
    """485 透传链路统计。"""

    tx_bytes: int = 0
    rx_bytes: int = 0
    tx_frames: int = 0
    rx_frames: int = 0
    tx_rate_bps: float = 0.0
    rx_rate_bps: float = 0.0
    rx_rate_fps: float = 0.0
    last_tx_ts: float = 0.0
    last_rx_ts: float = 0.0
    pending: int = 0
    timeouts: int = 0
    errors: int = 0
    connected: bool = False
    port: str = ""


@dataclass
class CommandTemplate:
    """485 控制指令模板。"""

    id: str = ""
    name: str = ""
    group: str = "默认"
    payload: str = ""
    mode: str = "hex"
    suffix: str = "none"
    expect_reply: bool = True
    reply_timeout_ms: int = 800
    match_prefix: str = ""
    min_reply_bytes: int = 0
    crc16_append: bool = False
    interval_ms: int = 0
    enabled: bool = True
    order: int = 0
    note: str = ""

    def clone(self) -> CommandTemplate:
        return CommandTemplate(**asdict(self))


@dataclass
class CommandResult:
    """一次模板/原始指令的执行结果。"""

    ts: float = field(default_factory=time.time)
    template_id: str = ""
    name: str = ""
    sent_bytes: bytes = b""
    reply: bytes = b""
    ok: bool = False
    rtt_ms: float = 0.0
    error: str = ""
    matched: bool = False


@dataclass
class AlarmEvent:
    ts: float = field(default_factory=time.time)
    level: str = AlarmLevel.WARN.value
    source: str = ""
    code: str = ""
    message: str = ""
