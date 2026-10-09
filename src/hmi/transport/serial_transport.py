"""RS485（USB 转串口）通道：读线程 + 断线重连 + 端口自动识别。"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

from ..core.config import SerialCfg
from ..core.models import TransportState
from .base import ByteTransport

try:
    import serial
    from serial.tools import list_ports

    HAS_PYSERIAL = True
except Exception:
    serial = None
    list_ports = None
    HAS_PYSERIAL = False

_PORT_KEYWORDS = (
    "CH340",
    "CH341",
    "CP210",
    "FTDI",
    "USB-SERIAL",
    "USB Serial",
    "CDC",
    "485",
    "UART",
)


def list_serial_ports() -> list[dict[str, Any]]:
    """列出候选串口，附带是否像 USB 转串口工具的判断。"""
    if not HAS_PYSERIAL:
        return []
    out: list[dict[str, Any]] = []
    for p in list_ports.comports():
        text = " ".join(filter(None, [p.description, p.manufacturer, p.hwid or ""]))
        upper = text.upper()
        out.append(
            {
                "device": p.device,
                "description": p.description,
                "hwid": p.hwid,
                "vid": f"{p.vid:04X}" if p.vid else "",
                "pid": f"{p.pid:04X}" if p.pid else "",
                "likely_usb_uart": any(k.upper() in upper for k in _PORT_KEYWORDS)
                or bool(p.vid and p.pid),
            }
        )
    return sorted(out, key=lambda d: d["device"])


def guess_port(cfg: SerialCfg, ports: list[dict[str, Any]] | None = None) -> str:
    """按 VID 白名单 → 关键字 → 唯一端口顺序猜一个串口。"""
    items = ports if ports is not None else list_serial_ports()
    if not items:
        return ""
    vids = {str(v).upper().lstrip("0") or "0" for v in cfg.preferred_vids if v}
    for it in items:
        if it.get("vid", "").upper().lstrip("0") in vids and it["likely_usb_uart"]:
            return it["device"]
    for it in items:
        if it["likely_usb_uart"]:
            return it["device"]
    return items[0]["device"] if len(items) == 1 else ""


class SerialTransport(ByteTransport):
    """485 透传 + AT 共用的串口通道。"""

    def __init__(self, cfg: SerialCfg, *, name: str = "serial485") -> None:
        super().__init__(name)
        self.cfg = cfg
        self.port = cfg.port or ""
        self._ser: Any = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self.open_attempts = 0

    def open(self) -> None:
        if not HAS_PYSERIAL:
            py = sys.executable.replace("\\", "/")
            msg = (
                "缺少 pyserial，请对当前解释器安装依赖："
                f'"{py}" -m pip install -r requirements.txt'
            )
            self._set_state(TransportState.ERROR, msg)
            raise RuntimeError(msg)
        with self._lock:
            if self._state is TransportState.OPEN:
                return
            if not self.port:
                guessed = guess_port(self.cfg)
                if not guessed:
                    msg = "未检测到串口，请在 config/app.yaml 指定 serial.port"
                    self._note_error(msg)
                    self._set_state(TransportState.ERROR, msg)
                    raise RuntimeError(msg)
                self.port = guessed
            self._set_state(TransportState.OPENING, self.port)
            try:
                self._ser = serial.Serial(
                    port=self.port,
                    baudrate=int(self.cfg.baudrate),
                    bytesize=int(self.cfg.bytesize),
                    parity=self.cfg.parity,
                    stopbits=int(self.cfg.stopbits),
                    rtscts=bool(self.cfg.rtsCTS),
                    timeout=max(0.005, self.cfg.read_timeout_ms / 1000.0),
                    write_timeout=1.5,
                    exclusive=getattr(self.cfg, "exclusive_fallback", True),
                )
            except Exception as exc:
                self._note_error(f"打开串口 {self.port} 失败: {exc}")
                self._set_state(TransportState.ERROR, f"打开串口 {self.port} 失败：{exc}")
                if not self.cfg.port:
                    # 自动推断的端口打不开（拔线换口、被占用）：忘掉它，下次重开重新枚举
                    self.port = ""
                raise
            self.open_attempts += 1
            self._set_state(
                TransportState.OPEN,
                f"{self.port} @{self.cfg.baudrate}"
                f" {self.cfg.bytesize}{self.cfg.parity}{self.cfg.stopbits}",
            )
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name=f"hmi-{self.name}-rx", daemon=True
            )
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        self._set_state(TransportState.CLOSED, "已关闭")

    def write(self, data: bytes) -> int:
        ser = self._ser
        if ser is None or not getattr(ser, "is_open", False):
            raise OSError(f"串口未打开（{self.port or '未指定端口'}）")
        with self._write_lock:
            ser.write(data)
            try:
                ser.flush()
            except Exception:
                pass
        self._note_write(data)
        return len(data)

    def info(self) -> dict[str, Any]:
        base = super().info()
        base.update(
            {
                "port": self.port,
                "baudrate": self.cfg.baudrate,
                "frame": f"{self.cfg.bytesize}{self.cfg.parity}{self.cfg.stopbits}",
                "attempts": self.open_attempts,
                "pyserial": HAS_PYSERIAL,
            }
        )
        return base

    def _loop(self) -> None:
        idle = self.cfg.rx_idle_gap_ms / 1000.0
        backoff = 1.0
        while not self._stop.is_set():
            ser = self._ser
            if ser is None or not getattr(ser, "is_open", False):
                if not self._try_recover(backoff):
                    return
                backoff = min(10.0, backoff * 2)
                continue
            backoff = 1.0
            try:
                n = ser.in_waiting
                if n:
                    chunk = ser.read(min(int(n), 8192))
                    if chunk:
                        self._emit_bytes(self._drain(ser, chunk, idle))
                else:
                    first = ser.read(1)
                    if first:
                        self._emit_bytes(self._drain(ser, first, idle))
            except Exception as exc:
                self._note_error(f"读线程异常: {exc}")
                self._set_state(TransportState.ERROR, str(exc))
                try:
                    if ser is not None:
                        ser.close()
                except Exception:
                    pass
                self._ser = None
        self._set_state(TransportState.CLOSED, "读线程退出")

    def _drain(self, ser: Any, chunk: bytes, idle: float) -> bytes:
        """把紧随其后的字节并成一包，减少碎片回调。"""
        out = bytearray(chunk)
        t0 = time.monotonic()
        while time.monotonic() - t0 < idle:
            try:
                n = ser.in_waiting
            except Exception:
                break
            if not n:
                time.sleep(0.002)
                continue
            out.extend(ser.read(min(int(n), 8192 - len(out))))
            t0 = time.monotonic()
            if len(out) >= 8192:
                break
        return bytes(out)

    def _try_recover(self, backoff: float) -> bool:
        if not self.cfg.auto_reconnect:
            self.log.info("串口断开且未开启自动重连，读线程退出")
            return False
        self.log.info("%.1fs 后尝试重连串口 %s", backoff, self.port or "(自动)")
        if self._stop.wait(backoff):
            return False
        try:
            self.open()
        except Exception as exc:
            self.log.warning("重连失败: %s", exc)
            return True
        return True
