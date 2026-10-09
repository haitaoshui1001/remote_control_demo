"""机器人遥测帧解析：字段模型 + 三种可插拔解析器 + Wheeltec 下行帧编码。

- ``WheeltecFrameParser``（默认）：Wheeltec 底盘 24 字节上行反馈帧
  ``0x7B | 停止标志 | XYZ速度 | XYZ加速度 | IMU三轴角速度 | 电压 | BCC | 0x7D``，
  多字节均为大端补码（见《串口通信控制与反馈》协议）；
- ``BinaryFrameParser``：旧占位格式 ``AA 55 | 长度 | 状态1B | 电压2B(0.01V)
  | 电流2B(0.01A) | 电量1B | 速度2B(0.001m/s) | 纬度4B | 经度4B | XOR``
  （多字节小端），保留作甲方自定义格式的退路；
- ``AsciiLineParser``：旧占位格式 ``TLM,2,24.61,3.21,78,0.45,...`` 逗号行。

下行速度指令同样在本模块收口：``build_speed_frame`` 组 11 字节成品帧
``0x7B | 预留2B | XYZ目标速度 | BCC | 0x7D``。解析器都按"流"设计：输入任意
切分的字节块，内部攒缓冲，吐解析完成的样本，匹配不到帧特征的字节丢弃
（旁路解析，不影响透传数据页）。
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

__all__ = [
    "TelemetrySample",
    "TelemetryFrameParser",
    "BinaryFrameParser",
    "AsciiLineParser",
    "WheeltecFrameParser",
    "build_speed_frame",
    "describe_wheeltec_frame",
    "create_parser",
    "encode_binary_frame",
    "encode_ascii_line",
    "encode_wheeltec_frame",
]

_BINARY_LAYOUT = "<BHhBHi i".replace(" ", "")
_BINARY_PAYLOAD_SIZE = struct.calcsize(_BINARY_LAYOUT)

_WHEELTEC_HEAD = 0x7B
_WHEELTEC_TAIL = 0x7D
_WHEELTEC_UP_LEN = 24
_WHEELTEC_DOWN_LEN = 11
_WHEELTEC_UP_LAYOUT = ">B9hH"
#: 协议文档换算常量：加速度 raw ÷1672 = m/s²；IMU 角速度 raw ÷3753 = rad/s
_WHEELTEC_ACC_SCALE = 1672.0
_WHEELTEC_GYRO_SCALE = 3753.0


@dataclass
class TelemetrySample:
    """一帧遥测的解析结果；缺数据的字段为 None，由界面统一显示 ``--``。"""

    ts: float = 0.0
    status: int | None = None
    voltage: float | None = None
    current: float | None = None
    soc: float | None = None
    speed: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    gps_valid: bool = False
    distance: float | None = None
    #: Wheeltec 底盘反馈专有字段（其余协议恒为 None）：
    #: flag_stop 0=电机使能，非 0=失能/急停；vx/vy m/s、vz rad/s（左转正）；
    #: ax/ay/az m/s²、gx/gy/gz rad/s（IMU）
    flag_stop: int | None = None
    vx: float | None = None
    vy: float | None = None
    vz: float | None = None
    ax: float | None = None
    ay: float | None = None
    az: float | None = None
    gx: float | None = None
    gy: float | None = None
    gz: float | None = None
    raw: str = ""


class TelemetryFrameParser:
    """解析器接口：feed 任意字节块，返回其中解析完成的样本列表。"""

    name = "none"

    def reset(self) -> None:
        raise NotImplementedError

    def feed(self, data: bytes) -> list[TelemetrySample]:
        raise NotImplementedError


def _parse_header_hex(header_hex: str, default: bytes) -> bytes:
    raw = (header_hex or "").replace(",", " ").split()
    try:
        head = bytes(int(tok, 16) for tok in raw)
    except ValueError:
        return default
    return head or default


def _xor(data: bytes) -> int:
    acc = 0
    for byte in data:
        acc ^= byte
    return acc


class BinaryFrameParser(TelemetryFrameParser):
    """占位二进制帧：``AA 55 | 长度 | 16 字节负载 | XOR``。"""

    name = "binary"

    def __init__(self, header_hex: str = "AA 55", max_buffer: int = 4096) -> None:
        self.header = _parse_header_hex(header_hex, b"\xAA\x55")
        self.max_buffer = max(64, int(max_buffer))
        self._buf = bytearray()
        self.discarded = 0

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> list[TelemetrySample]:
        if data:
            self._buf.extend(data)
        out: list[TelemetrySample] = []
        head = self.header
        while True:
            idx = self._buf.find(head)
            if idx < 0:
                keep = len(head) - 1
                if len(self._buf) > keep:
                    self.discarded += len(self._buf) - keep
                    del self._buf[: len(self._buf) - keep]
                break
            if idx > 0:
                self.discarded += idx
                del self._buf[:idx]
            if len(self._buf) < len(head) + 1:
                break
            length = self._buf[len(head)]
            total = len(head) + 1 + int(length) + 1
            if len(self._buf) < total:
                if total > self.max_buffer:
                    self.discarded += len(head) + 1
                    del self._buf[: len(head) + 1]
                break
            frame = bytes(self._buf[:total])
            body = frame[len(head): total - 1]
            checksum = frame[total - 1]
            del self._buf[:total]
            sample = self._decode(body, checksum, frame)
            if sample is not None:
                out.append(sample)
        return out

    def _decode(self, body: bytes, checksum: int, frame: bytes) -> TelemetrySample | None:
        if _xor(body) != checksum:
            return None
        if len(body) < 1 + _BINARY_PAYLOAD_SIZE:
            return None
        status, raw_u, raw_i, soc, raw_s, lat_raw, lon_raw = struct.unpack(
            _BINARY_LAYOUT, body[1: 1 + _BINARY_PAYLOAD_SIZE]
        )
        lat = lat_raw / 1_000_000.0
        lon = lon_raw / 1_000_000.0
        return TelemetrySample(
            status=int(status),
            voltage=raw_u / 100.0,
            current=raw_i / 100.0,
            soc=float(soc),
            speed=raw_s / 1000.0,
            latitude=lat,
            longitude=lon,
            gps_valid=bool(lat or lon),
            raw=frame.hex(" "),
        )


def _wheeltec_upload_valid(frame: bytes) -> bool:
    return (
        len(frame) == _WHEELTEC_UP_LEN
        and frame[0] == _WHEELTEC_HEAD
        and frame[_WHEELTEC_UP_LEN - 1] == _WHEELTEC_TAIL
        and _xor(frame[: _WHEELTEC_UP_LEN - 2]) == frame[_WHEELTEC_UP_LEN - 2]
    )


def _decode_wheeltec_upload(frame: bytes) -> TelemetrySample:
    flag, *raws = struct.unpack(_WHEELTEC_UP_LAYOUT, frame[1:22])
    vx = raws[0] / 1000.0
    vy = raws[1] / 1000.0
    return TelemetrySample(
        voltage=raws[9] / 1000.0,
        speed=math.hypot(vx, vy),
        flag_stop=int(flag),
        vx=vx,
        vy=vy,
        vz=raws[2] / 1000.0,
        ax=raws[3] / _WHEELTEC_ACC_SCALE,
        ay=raws[4] / _WHEELTEC_ACC_SCALE,
        az=raws[5] / _WHEELTEC_ACC_SCALE,
        gx=raws[6] / _WHEELTEC_GYRO_SCALE,
        gy=raws[7] / _WHEELTEC_GYRO_SCALE,
        gz=raws[8] / _WHEELTEC_GYRO_SCALE,
        raw=frame.hex(" ").upper(),
    )


class WheeltecFrameParser(TelemetryFrameParser):
    """Wheeltec 底盘 24 字节定长反馈帧；BCC/帧尾校验不过则跳一字节重找 0x7B。"""

    name = "wheeltec"

    def __init__(self, max_buffer: int = 4096) -> None:
        self.max_buffer = max(64, int(max_buffer))
        self._buf = bytearray()
        self.discarded = 0

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> list[TelemetrySample]:
        if data:
            self._buf.extend(data)
        out: list[TelemetrySample] = []
        while True:
            idx = self._buf.find(_WHEELTEC_HEAD)
            if idx < 0:
                self.discarded += len(self._buf)
                self._buf.clear()
                break
            if idx > 0:
                self.discarded += idx
                del self._buf[:idx]
            if len(self._buf) < _WHEELTEC_UP_LEN:
                break
            if not _wheeltec_upload_valid(bytes(self._buf[:_WHEELTEC_UP_LEN])):
                self.discarded += 1
                del self._buf[:1]
                continue
            frame = bytes(self._buf[:_WHEELTEC_UP_LEN])
            del self._buf[:_WHEELTEC_UP_LEN]
            out.append(_decode_wheeltec_upload(frame))
        if len(self._buf) > self.max_buffer:
            self.discarded += len(self._buf) - self.max_buffer
            del self._buf[: len(self._buf) - self.max_buffer]
        return out


class AsciiLineParser(TelemetryFrameParser):
    """占位 ASCII 行：``TLM,状态,电压,电流,电量,速度,纬度,经度*校验``。"""

    name = "ascii"

    def __init__(self, prefix: str = "TLM", max_buffer: int = 4096) -> None:
        self.prefix = (prefix or "TLM").encode("ascii", "ignore")
        self.max_buffer = max(64, int(max_buffer))
        self._buf = bytearray()
        self.discarded = 0

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> list[TelemetrySample]:
        if data:
            self._buf.extend(data)
        out: list[TelemetrySample] = []
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                if len(self._buf) > self.max_buffer:
                    cut = len(self._buf) - self.max_buffer
                    self.discarded += cut
                    del self._buf[:cut]
                break
            line = bytes(self._buf[:nl]).strip()
            del self._buf[: nl + 1]
            if not line.startswith(self.prefix):
                self.discarded += len(line) + 1
                continue
            sample = self._decode(line)
            if sample is not None:
                out.append(sample)
        return out

    def _decode(self, line: bytes) -> TelemetrySample | None:
        text = line.decode("ascii", "ignore")
        star = text.find("*")
        if star >= 0:
            text = text[:star]
        parts = [p.strip() for p in text.split(",")]
        if len(parts) < 8 or parts[0] != self.prefix.decode("ascii"):
            return None

        def _opt_int(tok: str) -> int | None:
            try:
                return int(float(tok))
            except ValueError:
                return None

        def _opt_float(tok: str) -> float | None:
            try:
                return float(tok)
            except ValueError:
                return None

        status = _opt_int(parts[1])
        voltage = _opt_float(parts[2])
        current = _opt_float(parts[3])
        soc = _opt_float(parts[4])
        speed = _opt_float(parts[5])
        lat = _opt_float(parts[6])
        lon = _opt_float(parts[7])
        if voltage is None and speed is None:
            return None
        return TelemetrySample(
            status=status,
            voltage=voltage,
            current=current,
            soc=soc,
            speed=speed,
            latitude=lat,
            longitude=lon,
            gps_valid=bool(lat is not None and lon is not None and (lat or lon)),
            raw=line.decode("ascii", "ignore"),
        )


def clamp_s16(raw: int) -> int:
    return max(-0x8000, min(0x7FFF, int(raw)))


def _scaled_s16(value: float, factor: float) -> int:
    return clamp_s16(round(float(value) * factor))


def build_speed_frame(
    vx: float, vy: float, wz: float, *, invert_angular: bool = False
) -> bytes:
    """Wheeltec 下行速度帧（11 字节）：``0x7B | 预留2B | XYZ目标 | BCC | 0x7D``。

    物理量纲：vx/vy m/s、wz rad/s（raw = ×1000，mm/s 与 millirad/s）。
    左转为正与界面一致；实机若转向相反，用 invert_angular 翻转符号。
    """
    if invert_angular:
        wz = -wz
    head = bytes([_WHEELTEC_HEAD, 0x00, 0x00])
    body = struct.pack(
        ">hhh",
        _scaled_s16(vx, 1000.0),
        _scaled_s16(vy, 1000.0),
        _scaled_s16(wz, 1000.0),
    )
    return head + body + bytes([_xor(head + body), _WHEELTEC_TAIL])


def describe_wheeltec_frame(data: bytes) -> str:
    """把一帧 Wheeltec 报文翻译成人话，供流水表"备注"列展示；不是本协议帧返回空串。

    - 11 字节下行：``速度指令：前向速度 0.055 m/s，角速度 0.000 rad/s``
      （横向速度非零时额外补一段）；
    - 24 字节上行：``底盘反馈：使能，前向速度 …，角速度 …，电压 …V``。

    两种帧都先验帧头/帧尾/BCC，校验不过宁可不解释，免得把坏帧的噪声
    当成速度报给操作员。数值一律取帧内 raw÷1000 的实际生效值——与线上
    字节严格一致，量化（夹到 int16、四舍五入）后的结果也如实反映。
    """
    data = bytes(data)
    if len(data) == _WHEELTEC_DOWN_LEN:
        if (
            data[0] != _WHEELTEC_HEAD
            or data[-1] != _WHEELTEC_TAIL
            or _xor(data[:9]) != data[9]
        ):
            return ""
        vx, vy, wz = (v / 1000.0 for v in struct.unpack(">hhh", data[3:9]))
        parts = [f"前向速度 {vx:.3f} m/s"]
        if vy:
            parts.append(f"横向速度 {vy:.3f} m/s")
        parts.append(f"角速度 {wz:.3f} rad/s")
        return "速度指令：" + "，".join(parts)
    if len(data) == _WHEELTEC_UP_LEN and _wheeltec_upload_valid(data):
        s = _decode_wheeltec_upload(data)
        state = "使能" if not s.flag_stop else f"失能(标志 0x{s.flag_stop:02X})"
        parts = [state, f"前向速度 {s.vx:.3f} m/s"]
        if s.vy:
            parts.append(f"横向速度 {s.vy:.3f} m/s")
        parts.append(f"角速度 {s.vz:.3f} rad/s")
        if s.voltage is not None:
            parts.append(f"电压 {s.voltage:.1f} V")
        return "底盘反馈：" + "，".join(parts)
    return ""


def create_parser(frame_cfg: object) -> TelemetryFrameParser:
    """按 ``telemetry.frame`` 配置段实例化解析器；默认 Wheeltec 底盘协议。"""
    protocol = str(getattr(frame_cfg, "protocol", "wheeltec") or "wheeltec").strip().lower()
    if protocol == "ascii":
        return AsciiLineParser()
    if protocol == "binary":
        return BinaryFrameParser(header_hex=str(getattr(frame_cfg, "header_hex", "AA 55")))
    return WheeltecFrameParser()


def encode_binary_frame(
    status: int,
    voltage: float,
    current: float,
    soc: float,
    speed: float,
    latitude: float,
    longitude: float,
    header_hex: str = "AA 55",
) -> bytes:
    """按占位格式组一帧（mock 源与自测共用，保证编解码成对验证）。"""
    head = _parse_header_hex(header_hex, b"\xAA\x55")
    payload = struct.pack(
        _BINARY_LAYOUT,
        int(status) & 0xFF,
        max(0, min(0xFFFF, int(round(voltage * 100)))),
        max(-0x8000, min(0x7FFF, int(round(current * 100)))),
        max(0, min(0xFF, int(round(soc)))),
        max(0, min(0xFFFF, int(round(speed * 1000)))),
        int(round(latitude * 1_000_000)),
        int(round(longitude * 1_000_000)),
    )
    body = bytes([len(payload)]) + payload
    return head + body + bytes([_xor(body)])


def encode_ascii_line(
    status: int,
    voltage: float,
    current: float,
    soc: float,
    speed: float,
    latitude: float,
    longitude: float,
) -> bytes:
    """按占位格式组一行（mock 源与自测共用）。"""
    text = (
        f"TLM,{int(status)},{voltage:.2f},{current:.2f},{soc:.0f},"
        f"{speed:.2f},{latitude:.6f},{longitude:.6f}"
    )
    return (text + "\r\n").encode("ascii")


def encode_wheeltec_frame(
    flag_stop: int = 0,
    voltage: float = 24.0,
    vx: float = 0.0,
    vy: float = 0.0,
    wz: float = 0.0,
    ax: float = 0.0,
    ay: float = 0.0,
    az: float = 0.0,
    gx: float = 0.0,
    gy: float = 0.0,
    gz: float = 0.0,
) -> bytes:
    """按 Wheeltec 协议组一帧上行反馈（24 字节），mock 源与自测共用保证编解码成对。"""
    payload = struct.pack(
        _WHEELTEC_UP_LAYOUT,
        int(flag_stop) & 0xFF,
        _scaled_s16(vx, 1000.0),
        _scaled_s16(vy, 1000.0),
        _scaled_s16(wz, 1000.0),
        _scaled_s16(ax, _WHEELTEC_ACC_SCALE),
        _scaled_s16(ay, _WHEELTEC_ACC_SCALE),
        _scaled_s16(az, _WHEELTEC_ACC_SCALE),
        _scaled_s16(gx, _WHEELTEC_GYRO_SCALE),
        _scaled_s16(gy, _WHEELTEC_GYRO_SCALE),
        _scaled_s16(gz, _WHEELTEC_GYRO_SCALE),
        max(0, min(0xFFFF, round(voltage * 1000.0))),
    )
    frame = bytes([_WHEELTEC_HEAD]) + payload
    return frame + bytes([_xor(frame), _WHEELTEC_TAIL])
