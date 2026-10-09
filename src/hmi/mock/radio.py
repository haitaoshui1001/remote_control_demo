"""仿真电台：把"EWD611 的一块 485 口 + 一个网口"模拟成可对话的对象。

用途：
- 没有硬件时跑通 AT 配置、透传收发、信道遥测三条链路；
- 单元测试里作为可控替身（可注入固定 RSSI、强制一次 ERROR 响应）。

行为对齐手册：查询响应为 "+NAME:value\r\nOK\r\n"，设置成功 "OK\r\n"，
失败 "ERROR\r\n"；AT 与透传数据共用同一条 485 线，靠"是否 AT 开头"区分。
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from ..core.logger import get_logger
from ..core.models import TransportState
from ..transport.base import ByteTransport

RO = "ro"
RW = "rw"
WO = "wo"


class MockRadioModule:
    """一台 EWD611 的参数表 + AT 解释器 + 无线信道状态。"""

    #: AT 名 -> (默认值, 读写性)
    SPEC: dict[str, tuple[Any, str]] = {
        "MODE": ("ap", RW),
        "SSID": ("hgic_ah_test", RW),
        "KEYMGMT": ("WPA-PSK", RW),
        "PSK": ("0" * 64, RW),
        "R_SSID": ("", RW),
        "R_PSK": ("", RW),
        "BSS_BW": (8, RW),
        "FREQ_RANGE": ([8680, 9280], RW),
        "CHAN_LIST": ([8680, 8700, 8720], RW),
        "TXPOWER": (20, RW),
        "TX_MCS": (10, RW),
        "ACKTMO": (0, RW),
        "HEART_INT": (1000, RW),
        "SYSDBG": (0, RW),
        "WNBCFG": ("", RO),
        "RSSI": (-72.0, RO),
        "EVM": (-28.0, RO),
        "CONN_STATE": ("connected", RO),
        "STA_INFO": ("", RO),
        "VERSION": ("V1.6.2-mock", RO),
        "BSSLIST": ("", RO),
        "PAIR": (0, WO),
        "UNPAIR": ("", WO),
        "LOADDEF": (0, WO),
        "SCAN_AP": (0, WO),
        "JOINGROUP": ("", WO),
    }

    MODES = ("ap", "sta", "group", "apsta")
    KEYMGMTS = ("NONE", "WPA-PSK")
    #: 手册里响应不带 OK 的查询（AtClient 对这类用"值行即成功"规则）
    NO_OK_RESPONSE = ("CONN_STATE", "WNBCFG")
    #: 可以带 <id> / <mac> 做定向读取的只读参数（6.3.9 / 6.3.12 / 6.3.14）
    READ_WITH_ARG = ("RSSI", "EVM", "STA_INFO", "BSSLIST")
    INT_RANGE = {
        "BSS_BW": (1, 160),
        "TXPOWER": (0, 30),
        "TX_MCS": (0, 255),
        "ACKTMO": (0, 2000),
        "HEART_INT": (100, 10000),
        "SCAN_AP": (1, 10),
    }
    HEX_KEYS = ("PSK", "R_PSK")
    LIST_KEYS = ("FREQ_RANGE", "CHAN_LIST")
    TEXT_KEYS = ("SSID", "R_SSID")

    def __init__(self, *, name: str = "local", seed: int | None = None) -> None:
        self.name = name
        self.log = get_logger(f"mock.{name}")
        self._rnd = random.Random(seed)
        self._lock = threading.RLock()
        self.params: dict[str, Any] = self._defaults()
        self.wake_mode = True
        self.echo = True
        self.force_error: str | None = None
        self.rx_log: deque[tuple[float, bytes]] = deque(maxlen=500)
        #: 配对与调试打印状态（AT+PAIR / AT+SYSDBG 的行为仿真）
        self.pairing = False
        self.sysdbg: dict[str, int] = {"LMAC": 0, "WNB": 0}
        self.group: tuple[str, int] | None = None
        self.scanned = False
        #: 由 MockByteTransport 注入：将自发文本推到线上
        self.on_unsolicited: Callable[[str], None] | None = None
        self.sta_table: list[dict[str, Any]] = [
            {
                "mac": "00:11:22:33:44:55",
                "rssi": -78.0,
                "evm": -22.0,
                "tx_mcs": 7,
                "rx_mcs": 7,
                "tx": 15234,
                "rx": 18990,
                "lost": 12,
            },
        ]

    def _defaults(self) -> dict[str, Any]:
        return {k: (list(v) if isinstance(v, list) else v) for k, (v, _r) in self.SPEC.items()}

    # ------------------------------------------------------------------ 推到线上
    def push(self, text: str) -> None:
        """模拟电台自发文本（PAIR SUCCESS、LMAC 统计打印）。"""
        cb = self.on_unsolicited
        if cb is not None:
            cb(text)

    # ------------------------------------------------------------------ 存取
    def get(self, key: str) -> Any:
        with self._lock:
            return self.params.get(key.upper())

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self.params[key.upper()] = value

    # ------------------------------------------------------------- AT 解释器
    def handle(self, line: str) -> str:
        """输入一行（不含换行），返回要写回 485/UDP 的原始文本。"""
        text = line.strip()
        if not text or not text.upper().startswith("AT"):
            return ""
        body = text[2:]
        if body == "":
            return "OK\r\n"
        if not body.startswith("+"):
            return "ERROR\r\n"
        rest = body[1:]
        if rest.endswith("?"):
            name, query, value = rest[:-1].strip().upper(), True, ""
        else:
            head, sep, value = rest.partition("=")
            name = head.strip().upper()
            # 手册对 CONN_STATE / WNBCFG / RSSI 同时给了"不带 ?"的写法，这里一并接受
            query = sep != "="
        if name not in self.SPEC:
            return "ERROR\r\n"
        with self._lock:
            if not self.wake_mode:
                return "ERROR\r\n"
            if self.force_error:
                err, self.force_error = self.force_error, None
                return f"{err}\r\n"
        if query:
            return self._answer_query(name)
        if name in self.READ_WITH_ARG and self.SPEC[name][1] == RO:
            return self._answer_read_arg(name, value)
        action = self._ACTIONS.get(name)
        if action is not None:
            return action(self, value)
        ok, parsed = self._parse(name, value)
        if not ok or self.SPEC[name][1] == RO:
            return "ERROR\r\n"
        self.set(name, parsed)
        return "OK\r\n"

    def _answer_query(self, name: str) -> str:
        if self.SPEC[name][1] == WO:
            return "ERROR\r\n"
        if name == "CONN_STATE":
            state = str(self.get("CONN_STATE") or "").lower()
            connected = "connect" in state and "dis" not in state
            return "+CONNECTED\r\n" if connected else "+DISCONNECT\r\n"
        tail = "" if name in self.NO_OK_RESPONSE else "OK\r\n"
        return f"+{name}:{self._render(name)}\r\n{tail}"

    def _answer_read_arg(self, name: str, value: str) -> str:
        """AT+RSSI=<id|mac> / AT+STA_INFO=<id> / AT+BSSLIST=<n> 这类带参数的定向读取。"""
        arg = value.strip()
        if name in ("BSSLIST",):
            if not self.scanned:
                return "ERROR\r\n"
            return f"+BSSLIST:{self._render(name)}\r\nOK\r\n"
        row = self._find_sta(arg)
        if row is None:
            return f"sta:{arg} is not exist\r\n"
        if name == "RSSI":
            return f"+RSSI:{row['rssi']:.1f}\r\nOK\r\n"
        if name == "EVM":
            return f"+EVM:{row['evm']:.1f}\r\nOK\r\n"
        idx = self.sta_table.index(row) + 1
        return f"+STA_INFO:{self._render_sta(idx)}\r\nOK\r\n"

    def _find_sta(self, arg: str) -> dict[str, Any] | None:
        if not arg:
            return self.sta_table[0] if self.sta_table else None
        if arg.isdigit():
            idx = int(arg) - 1
            if 0 <= idx < len(self.sta_table):
                return self.sta_table[idx]
            return None
        low = arg.lower()
        for sta in self.sta_table:
            if str(sta["mac"]).lower() == low:
                return sta
        return None

    # ---------------------------------------------------------------- 动作指令
    def _do_pair(self, value: str) -> str:
        if value not in ("0", "1"):
            return "ERROR\r\n"
        self.pairing = value == "1"
        return "OK\r\n"

    def _do_unpair(self, value: str) -> str:
        mac = value.strip().lower()
        for sta in list(self.sta_table):
            if str(sta["mac"]).lower() == mac:
                self.sta_table.remove(sta)
                return f"unpair sta:{mac}\r\n"
        return f"sta:{mac} is not exist\r\n"

    def _do_loaddef(self, value: str) -> str:
        if value != "1":
            return "ERROR\r\n"
        with self._lock:
            self.params = self._defaults()
            self.sysdbg = {"LMAC": 0, "WNB": 0}
            self.group = None
        return "OK\r\n"

    def _do_scan_ap(self, value: str) -> str:
        ok, num = self._parse("SCAN_AP", value)
        if not ok:
            return "ERROR\r\n"
        self.scanned = True
        self.set("BSSLIST", "hgic_ah_test,90800,-72.0,1;hgic_relay,91600,-83.5,2")
        self.log.debug("扫描 %d 秒完成", num)
        return "OK\r\n"

    def _do_join(self, value: str) -> str:
        head, sep, aid = value.partition(",")
        if not sep or not aid.strip().isdigit():
            return "ERROR\r\n"
        if str(self.get("MODE")).lower() != "group":
            return "ERROR\r\n"
        self.group = (head.strip().lower(), int(aid))
        return "OK\r\n"

    def _do_sysdbg(self, value: str) -> str:
        head, sep, num = value.partition(",")
        key = head.strip().upper()
        if not sep or key not in self.sysdbg or num.strip() not in ("0", "1"):
            return "ERROR\r\n"
        self.sysdbg[key] = int(num)
        return "OK\r\n"

    _ACTIONS: dict[str, Callable[[Any, str], str]] = {
        "PAIR": _do_pair,
        "UNPAIR": _do_unpair,
        "LOADDEF": _do_loaddef,
        "SCAN_AP": _do_scan_ap,
        "JOINGROUP": _do_join,
        "SYSDBG": _do_sysdbg,
    }

    def _render(self, key: str) -> str:
        val = self.get(key)
        if key == "STA_INFO":
            return self._render_sta()
        if key == "WNBCFG":
            chans = self.get("CHAN_LIST") or []
            return (
                f"mode={self.get('MODE')},bw={self.get('BSS_BW')},"
                f"chan={','.join(str(c) for c in chans)}"
            )
        if isinstance(val, (list, tuple)):
            return ",".join(str(v) for v in val)
        if key in ("RSSI", "EVM"):
            return f"{float(val):.1f}"
        return "" if val is None else str(val)

    def _render_sta(self, index: int | None = None) -> str:
        rows = []
        for i, s in enumerate(self.sta_table, start=1):
            if index is not None and i != index:
                continue
            rows.append(
                f"{i},{s['mac']},{s['rssi']:.1f},{s['evm']:.1f},"
                f"{s['tx_mcs']},{s['rx_mcs']},{s['tx']},{s['rx']},{s['lost']}"
            )
        return ";".join(rows)

    def _parse(self, key: str, value: str) -> tuple[bool, Any]:
        value = value.strip()
        if key == "MODE":
            low = value.lower()
            return (low in self.MODES), low
        if key == "KEYMGMT":
            up = value.upper()
            return (up in self.KEYMGMTS), up
        if key in self.HEX_KEYS:
            hexstr = value.replace(" ", "")
            valid = all(c in "0123456789abcdefABCDEF" for c in hexstr)
            return (len(hexstr) in (0, 64) and valid), hexstr.lower()
        if key in self.LIST_KEYS:
            try:
                items = [int(p) for p in value.split(",") if p.strip()]
            except ValueError:
                return False, None
            return (len(items) > 0), items
        if key in self.TEXT_KEYS:
            return (1 <= len(value) <= 32), value
        if key in self.INT_RANGE:
            try:
                num = int(value)
            except ValueError:
                return False, None
            lo, hi = self.INT_RANGE[key]
            return (lo <= num <= hi), num
        return True, value

    # ------------------------------------------------------------ 信道物理仿真
    def tick(self) -> None:
        """RSSI/EVM 随机游走，让链路监控曲线看起来是活的。"""
        with self._lock:
            rssi = float(self.get("RSSI") or -70.0)
            rssi = max(-98.0, min(-52.0, rssi + self._rnd.uniform(-2.2, 2.2)))
            self.params["RSSI"] = round(rssi, 1)
            self.params["EVM"] = round(
                -34.0 + (rssi + 98.0) * 0.35 + self._rnd.uniform(-1.5, 1.5), 1
            )
            self.params["CONN_STATE"] = "connected" if rssi > -95.0 else "disconnected"
            for s in self.sta_table:
                s["rssi"] = round(
                    max(-98.0, min(-52.0, s["rssi"] + self._rnd.uniform(-1.5, 1.5))), 1
                )
                s["tx"] += self._rnd.randint(3, 40)
                s["rx"] += self._rnd.randint(3, 40)
                if self._rnd.random() < 0.12:
                    s["lost"] += 1
        self._push_events()

    def _push_events(self) -> None:
        """配对成功消息和 LMAC 统计打印：电台自发文本，不带 AT 回显。"""
        first = self.sta_table[0] if self.sta_table else None
        if self.pairing:
            if first is not None and self._rnd.random() < 0.35:
                self.pairing = False
                self.push(f"+PAIR_SUCCESS {first['mac']}\r\n")
        elif self.sysdbg.get("LMAC") and first is not None:
            self.push(f"+LMAC:tx={first['tx']},rx={first['rx']},lost={first['lost']}\r\n")

    def stats_line(self) -> str:
        with self._lock:
            return f"mode={self.get('MODE')} rssi={self.get('RSSI')} sta={len(self.sta_table)}"


class MockByteTransport(ByteTransport):
    """挂在 MockRadioModule 上的字节通道：AT 走解释器，其余走"从机设备"仿真。"""

    def __init__(
        self,
        module: MockRadioModule,
        *,
        name: str = "mock485",
        latency_ms: int = 12,
        telemetry_interval_s: float = 2.0,
    ) -> None:
        super().__init__(name=name)
        self.module = module
        self.latency_ms = max(0, int(latency_ms))
        self.telemetry_interval_s = max(0.2, float(telemetry_interval_s))
        self._line = b""
        self._out: deque[tuple[float, bytes]] = deque()
        self._qlock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.paused = False
        module.on_unsolicited = self._push_line

    def _push_line(self, text: str) -> None:
        self._schedule(text.encode("ascii", "replace"))

    def open(self) -> None:
        self._set_state(TransportState.OPENING, "仿真链路建立中")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name=f"hmi-{self.name}-mock", daemon=True
        )
        self._thread.start()
        self._set_state(TransportState.OPEN, f"仿真（{self.module.name}）")

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._thread = None
        with self._qlock:
            self._out.clear()
            self._line = b""
        self._set_state(TransportState.CLOSED, "仿真链路已关闭")

    def write(self, data: bytes) -> int:
        if not self.is_open():
            raise OSError("仿真链路未打开")
        chunk = bytes(data)
        self._note_write(chunk)
        if chunk.lstrip(b"\r\n\t ").upper().startswith(b"AT"):
            self._line += chunk
            self._drain_lines()
        else:
            self._reply_device(chunk)
        return len(chunk)

    # -------------------------------------------------------------------- 内部
    def _drain_lines(self) -> None:
        while True:
            idx = min(
                (i for i in (self._line.find(b"\r"), self._line.find(b"\n")) if i >= 0), default=-1
            )
            if idx < 0:
                return
            line, self._line = self._line[:idx], self._line[idx + 1 :]
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            resp = self.module.handle(text)
            if not resp:
                continue
            payload = resp.encode("ascii", "replace")
            if self.module.echo:
                payload = text.encode("ascii", "replace") + b"\r\n" + payload
            self._schedule(payload)

    def _reply_device(self, chunk: bytes) -> None:
        """远端待控制设备的应答：Modbus-RTU 风格，地址/功能码回显 + 一段数据。"""
        self.module.rx_log.append((time.time(), chunk))
        addr = chunk[0] if chunk else 0x01
        func = chunk[1] if len(chunk) > 1 else 0x03
        seq = len(self.module.rx_log) & 0xFF
        payload = bytes([addr, func, 0x04, seq, 0x00, 0x01, 0x02])
        self._schedule(payload)

    def _schedule(self, payload: bytes) -> None:
        due = time.monotonic() + self.latency_ms / 1000.0
        with self._qlock:
            self._out.append((due, payload))

    def _pop_due(self, now: float) -> bytes | None:
        with self._qlock:
            if not self._out:
                return None
            due, payload = self._out[0]
            if due > now:
                return None
            self._out.popleft()
            return payload

    def _loop(self) -> None:
        last_tick = 0.0
        next_tel = time.monotonic() + self.telemetry_interval_s
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_tick >= 1.0:
                last_tick = now
                self.module.tick()
            if not self.paused and now >= next_tel:
                self._schedule(f"@DEV {self.module.stats_line()}\n".encode("ascii", "replace"))
                next_tel = now + self.telemetry_interval_s
            while True:
                payload = self._pop_due(time.monotonic())
                if payload is None:
                    break
                self._emit_bytes(payload)
            self._stop.wait(0.02)


def build_pair(
    name_local: str = "local", name_remote: str = "remote"
) -> tuple[MockRadioModule, MockRadioModule]:
    """造一对互联的仿真模块（本地 AP + 远端 STA）。"""
    ap = MockRadioModule(name=name_local, seed=11)
    sta = MockRadioModule(name=name_remote, seed=23)
    ap.set("MODE", "ap")
    sta.set("MODE", "sta")
    sta.set("R_SSID", ap.get("SSID"))
    return ap, sta
