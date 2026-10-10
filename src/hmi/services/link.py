"""链路服务：把"一条 485/UDP 线"装配成可用对象，并负责运行时换通道。

``ChannelDemux`` 规定一条字节通道只能有一个读者。**共线模式**（透传与 AT 走
同一条线）下，AT 对话、透传收发、遥测轮询全部共用同一个 demux，且 AT 事务
窗口内的透传帧会被半双工守卫拒发；一旦别处再 new 一个 demux 去读同一个串口，
两边会互抢字节、AT 应答随机丢。

**分线模式**（默认：AT 走网口 UDP、透传走 485 转 USB 串口，由
``passthrough.channel`` 决定）下，本服务为透传单独持有一条字节通道：

    LinkService
      ├── transport   AT 侧：UdpAtTransport / SerialTransport / Mock
      ├── demux       AT 侧分流器（唯一读者）
      ├── at          AtClient（一次一问一答）
      ├── monitor     LinkMonitor（周期读遥测参数 + 判告警）
      ├── _pt_transport  透传侧串口（独立打开/关闭，失败不阻断 AT）
      └── pt_demux       透传侧分流器（AT 永不在其上开窗口→守卫天然失效）

透传线的打开**不在** ``open()`` 里做（AT 失败路径绝不碰串口是硬约束），由
``open_passthrough()`` 独立负责，透传服务启动时调用。

本层另外补两件事：``transport/state`` 事件的发布（传输层只回调、不发事件，
AT 线 kind="at"、透传线 kind="pt"），以及 ``check_stale()`` 的节拍（轮询线程
自己不查过期，不查就没人报"遥测已过期"）。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from ..core.config import AppConfig
from ..core.events import EventBus, Events, get_bus
from ..core.logger import get_logger
from ..core.models import AlarmEvent, AlarmLevel, TransportState
from ..mock.radio import MockByteTransport, MockRadioModule
from ..protocol.at_client import AtClient
from ..protocol.channel import ChannelDemux
from ..protocol.telemetry import LinkMonitor
from ..transport.serial_transport import SerialTransport, list_serial_ports
from ..transport.udp_at import UdpAtTransport, format_mac, list_local_ips
from .periodic import Ticker

__all__ = ["LinkService", "CHANNELS"]

#: 可切换的 AT 通道；mock 只在仿真模式下出现
CHANNELS: tuple[str, ...] = ("serial", "udp", "mock")

TransportLike = SerialTransport | UdpAtTransport | MockByteTransport


class LinkService:
    """一条控制线的生命周期与状态出口。"""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        bus: EventBus | None = None,
        log: logging.Logger | None = None,
        transport: TransportLike | None = None,
        channel: str | None = None,
        endpoint: str = "local",
        stale_interval_s: float = 1.0,
    ) -> None:
        self.cfg = cfg
        self.bus = bus if bus is not None else get_bus()
        self.log = log or get_logger(f"hmi.services.link.{endpoint}")
        self.endpoint = str(endpoint)
        self.channel = self._normalize_channel(channel or cfg.at.channel)

        self.demux = ChannelDemux(
            f"demux-{self.endpoint}",
            idle_gap_ms=cfg.passthrough.rx_idle_gap_ms,
            read_timeout_ms=cfg.serial.read_timeout_ms,
        )
        self.at = AtClient(
            cfg.at,
            self.demux,
            channel=self.channel,
            endpoint=self.endpoint,
            bus=self.bus,
            log=get_logger(f"hmi.protocol.at.{self.endpoint}"),
        )
        self.monitor = LinkMonitor(
            self.at,
            bus=self.bus,
            cfg=cfg.link,
            log=get_logger(f"hmi.protocol.link.{self.endpoint}"),
            modules_provider=self.at_modules,
        )
        self._ticker = Ticker(
            f"link-{self.endpoint}-stale",
            self._tick,
            stale_interval_s,
            log=self.log,
        )
        self._lock = threading.Lock()
        self._transport: TransportLike | None = None
        self._mock_module: MockRadioModule | None = None
        self._open_error = ""
        self._last_state: TransportState = TransportState.CLOSED
        self._state_listeners: list[Any] = []

        # 透传侧独立通道（分线模式）；仿真模式下两功能都落在假电台上=共线
        self.passthrough_channel = self._resolve_passthrough_channel()
        self.passthrough_split = bool(
            not cfg.app.mock and self.passthrough_channel != self.channel
        )
        self.pt_demux: ChannelDemux | None = None
        self._pt_transport: TransportLike | None = None
        self._pt_open_error = ""
        self._pt_last_state: TransportState = TransportState.CLOSED
        if self.passthrough_split:
            self.pt_demux = ChannelDemux(
                f"demux-{self.endpoint}-pt",
                idle_gap_ms=cfg.passthrough.rx_idle_gap_ms,
                read_timeout_ms=cfg.serial.read_timeout_ms,
            )

        if transport is not None:
            self.attach(transport, channel=channel or self.channel)
        else:
            built, built_name = self._new_transport(self.channel)
            self.attach(built, channel=built_name)

    # ------------------------------------------------------------------ 装配
    def _normalize_channel(self, channel: str | None) -> str:
        value = str(channel or "udp").strip().lower()
        if value in CHANNELS:
            return value
        self.log.warning("未知 AT 通道 %r，退回 udp", channel)
        return "udp"

    def _resolve_passthrough_channel(self) -> str:
        """把 ``passthrough.channel`` 解析成具体通道名；same/空=跟随 AT 线。"""
        want = str(self.cfg.passthrough.channel or "").strip().lower()
        if want in ("", "same", "at"):
            return self.channel
        if want in CHANNELS:
            return want
        self.log.warning("未知透传通道 %r，按 serial 处理", want)
        return "serial"

    def build_transport(self, channel: str | None = None) -> TransportLike:
        return self._new_transport(channel)[0]

    def _new_transport(self, channel: str | None = None) -> tuple[TransportLike, str]:
        """造一个未打开的传输层，并回它**实际**属于哪个通道。

        仿真模式下 serial/udp 这类真硬件通道一律换成假电台，通道名跟着改成
        ``mock``——AT 默认走网口后，假电台换装不能再只盯着 serial，否则
        仿真 GUI 会去广播探测真网络、启动即软失败。
        """
        name = self._normalize_channel(channel)
        if name == "mock" or (self.cfg.app.mock and name in ("serial", "udp")):
            module = MockRadioModule(name=self.endpoint)
            self._mock_module = module
            return MockByteTransport(module, name=f"mock485-{self.endpoint}"), "mock"
        self._mock_module = None
        if name == "udp":
            return (
                UdpAtTransport(self.cfg.at, self.cfg.udp_at, name=f"udp_at-{self.endpoint}"),
                "udp",
            )
        return SerialTransport(self.cfg.serial, name=f"serial485-{self.endpoint}"), "serial"

    def attach(self, transport: TransportLike, *, channel: str | None = None) -> None:
        """换绑传输层；旧通道自动解绑并关闭。"""
        with self._lock:
            old = self._transport
            self._transport = transport
            if channel:
                self.channel = self._normalize_channel(channel)
        if old is not None and old is not transport:
            try:
                old.remove_state_listener(self._on_transport_state)
            except (AttributeError, ValueError):
                pass
            if self._is_open(old):
                self._safe_close(old)
        transport.add_state_listener(self._on_transport_state)
        self.demux.attach(transport, start=False)
        self.at.mark_channel(self.channel, self.endpoint)
        self._last_state = TransportState(getattr(transport, "state", TransportState.CLOSED))

    @property
    def transport(self) -> TransportLike | None:
        return self._transport

    @property
    def mock_module(self) -> MockRadioModule | None:
        """仿真模式下的假电台（自检脚本用它注入主动上报）。"""
        return self._mock_module

    def add_state_listener(self, fn: Any) -> None:
        """服务层内部转发器（透传/录制服务可挂这里，避免各自订阅总线）。"""
        with self._lock:
            if fn not in self._state_listeners:
                self._state_listeners.append(fn)

    # ------------------------------------------------------------------ 起停
    def open(self) -> bool:
        """打开线 + 起分流线程 + 按配置起轮询；任何一步失败都不抛异常。

        首选通道打不开时，仅当用户显式配置了 ``at.fallback_channel`` 才
        重建传输层重试（默认为空=禁用）——手册第六章：AT 只走网口配置，
        串口是纯透传通道，自动回退串口只会把 AT 透传到远端设备，禁作保底。
        """
        if self._transport is None:
            self._open_error = "未绑定传输层"
            self._alarm("AT_NO_TRANSPORT", self._open_error)
            return False
        attempted = [self.channel]
        first_error = ""
        while True:
            try:
                self._transport.open()
                break
            except Exception as exc:
                self.log.warning("AT 通道打开失败（%s）：%s", self.channel, exc)
                if not first_error:
                    first_error = str(exc)
                nxt = self._fallback_target(attempted)
                if not nxt:
                    self._open_error = first_error
                    self._alarm("AT_OPEN_FAILED", f"{self.channel} 打不开：{exc}")
                    return False
                self.log.info("AT 自动回退到 %s 通道", nxt)
                built, name = self._new_transport(nxt)
                self.attach(built, channel=name)
                attempted.append(name)
        self._open_error = ""
        self.demux.attach(self._transport, start=True)
        self._ticker.start()
        self.monitor.start()
        if len(attempted) > 1:
            self._alarm(
                "AT_CHANNEL_FALLBACK",
                f"AT 从 {'→'.join(attempted)} 回退后打开，首选通道原因：{first_error}",
            )
        self.log.info("AT 通道已打开：%s（%s）", self.channel, self._detail(self._transport))
        return True

    def _fallback_target(self, attempted: list[str]) -> str:
        """给出下一个该尝试的通道名；禁用/重复/仿真模式回空串。"""
        want = str(self.cfg.at.fallback_channel or "").strip().lower()
        if not want or self.cfg.app.mock:
            return ""
        nxt = self._normalize_channel(want)
        if nxt in attempted:
            return ""
        return nxt

    def close(self) -> None:
        self.monitor.stop()
        self._ticker.stop()
        self.demux.stop()
        transport = self._transport
        if transport is not None:
            self._safe_close(transport)
        with self._lock:
            self._open_error = ""
        self.close_passthrough()

    def restart(self) -> bool:
        self.close()
        time.sleep(0.05)
        return self.open()

    @staticmethod
    def _safe_close(transport: TransportLike) -> None:
        try:
            transport.close()
        except Exception as exc:
            logging.getLogger("hmi.services.link").debug("关闭通道异常（忽略）：%s", exc)

    @staticmethod
    def _is_open(transport: TransportLike) -> bool:
        try:
            return bool(transport.is_open())
        except Exception:
            return False

    @property
    def is_open(self) -> bool:
        transport = self._transport
        return bool(transport is not None and self._is_open(transport))

    @property
    def state(self) -> TransportState:
        transport = self._transport
        if transport is None:
            return TransportState.CLOSED
        return TransportState(getattr(transport, "state", TransportState.CLOSED))

    @property
    def polling(self) -> bool:
        return bool(self.monitor.stats().get("running"))

    # ------------------------------------------------------------------ 换通道
    def set_channel(self, channel: str, *, reopen: bool | None = None) -> dict[str, Any]:
        """运行时切串口/UDP（用户确认过的决策之一）。

        切换会重建传输层：demux 换绑时会停掉旧线程，AT 客户端只改事件归属字段，
        遥测轮询清单不用动（同一套 AT 命令，只是换了条管子）。
        """
        want = self._normalize_channel(channel)
        prev = self.channel
        was_open = self.is_open
        do_reopen = was_open if reopen is None else bool(reopen)
        if want == self.channel and self._transport is not None and not do_reopen:
            return {"ok": True, "channel": want, "detail": "通道未变化"}
        self.monitor.stop()
        self._ticker.stop()
        self.demux.detach()
        built, name = self._new_transport(want)
        self.attach(built, channel=name)
        opened = self.open() if do_reopen else False
        detail = f"已切换到 {name}" + (" 并重新打开" if opened else "（未自动打开）")
        self.log.info("AT 通道切换 %s -> %s（%s）", prev, name, detail)
        return {"ok": True, "channel": name, "detail": detail, "open": opened}

    # ---------------------------------------------------------- 透传独立通道
    def _build_pt_transport(self) -> TransportLike | None:
        """造（不打开）透传侧传输层并绑到 pt 分流器；共线模式返回 None。"""
        if not self.passthrough_split or self.pt_demux is None:
            return None
        with self._lock:
            if self._pt_transport is not None:
                return self._pt_transport
        name = self.passthrough_channel
        if name == "udp":
            built: TransportLike = UdpAtTransport(
                self.cfg.at, self.cfg.udp_at, name=f"udp_pt-{self.endpoint}"
            )
        else:
            built = SerialTransport(self.cfg.serial, name=f"serial485-{self.endpoint}-pt")
        built.add_state_listener(self._on_pt_transport_state)
        self.pt_demux.attach(built, start=False)
        with self._lock:
            self._pt_transport = built
        return built

    def open_passthrough(self) -> bool:
        """独立打开透传线（仅分线模式）。

        失败只记错误 + 发告警，**不阻断 AT**——485 转 USB 没插是常态，
        网口配置功能不该因此瘫痪。
        """
        if not self.passthrough_split:
            return self.is_open
        built = self._build_pt_transport()
        if built is None:
            return False
        try:
            built.open()
        except Exception as exc:
            self._pt_open_error = str(exc)
            self.log.warning("透传通道打开失败（%s）：%s", self.passthrough_channel, exc)
            self._alarm("PT_OPEN_FAILED", f"透传通道 {self.passthrough_channel} 打不开：{exc}")
            return False
        self._pt_open_error = ""
        if self.pt_demux is not None:
            self.pt_demux.attach(built, start=True)
        self.log.info(
            "透传通道已打开：%s（%s）", self.passthrough_channel, self._detail(built)
        )
        return True

    def close_passthrough(self) -> None:
        if not self.passthrough_split:
            return
        if self.pt_demux is not None:
            self.pt_demux.stop()
        with self._lock:
            transport = self._pt_transport
        if transport is not None:
            self._safe_close(transport)

    @property
    def pt_is_open(self) -> bool:
        """透传线是否可发；共线模式下就是 AT 线的状态。"""
        if not self.passthrough_split:
            return self.is_open
        transport = self._pt_transport
        return bool(transport is not None and self._is_open(transport))

    @property
    def pt_state(self) -> TransportState:
        if not self.passthrough_split:
            return self.state
        transport = self._pt_transport
        if transport is None:
            return TransportState.CLOSED
        return TransportState(getattr(transport, "state", TransportState.CLOSED))

    def apply_passthrough_serial(self, **changes: Any) -> dict[str, Any]:
        """运行时换 485 串口/波特率（即选即效）。

        分线模式只重建透传线，AT 完全不受影响；共线模式下串口就是 AT 线，
        只能整条重启。配置写回（``cfg.save()``）由调用方负责。
        """
        valid = {"port", "baudrate", "bytesize", "parity", "stopbits"}
        unknown = [k for k in changes if k not in valid]
        if unknown:
            raise AttributeError(f"未知串口配置项: {', '.join(sorted(unknown))}")
        for key, value in changes.items():
            setattr(self.cfg.serial, key, value)
        if not self.passthrough_split:
            if "serial" not in (self.channel, self.passthrough_channel):
                return {"ok": True, "detail": "串口参数已保存（当前无通道使用串口）"}
            reopened = self.restart()
            detail = (
                "串口参数已应用并重启串口线" if reopened else "串口参数已保存，但线重开失败"
            )
            return {"ok": reopened, "detail": detail}
        with self._lock:
            old = self._pt_transport
            self._pt_transport = None
        if old is not None:
            try:
                old.remove_state_listener(self._on_pt_transport_state)
            except (AttributeError, ValueError):
                pass
            self._safe_close(old)
        if self.pt_demux is not None:
            self.pt_demux.detach()
        opened = self.open_passthrough()
        port = str(self.cfg.serial.port or "自动探测")
        detail = (
            f"透传串口已切到 {port} @ {self.cfg.serial.baudrate}"
            f"{self.cfg.serial.bytesize}{self.cfg.serial.parity}{self.cfg.serial.stopbits}"
        )
        if not opened:
            detail += f"（打开失败：{self._pt_open_error}）"
        return {"ok": opened, "detail": detail}

    def _on_pt_transport_state(self, state: object, detail: str = "") -> None:
        value = state if isinstance(state, TransportState) else TransportState(state)
        changed = value is not self._pt_last_state
        self._pt_last_state = value
        if value is TransportState.ERROR and detail:
            self._pt_open_error = detail
        if changed and self.bus is not None:
            transport = self._pt_transport
            self.bus.publish(
                Events.TRANSPORT_STATE,
                {
                    "ts": time.time(),
                    "kind": "pt",
                    "name": str(getattr(transport, "name", self.passthrough_channel)),
                    "state": value.value,
                    "detail": str(detail),
                    "channel": self.passthrough_channel,
                    "endpoint": self.endpoint,
                },
            )
        if value is TransportState.OPEN and self.pt_demux is not None:
            self.pt_demux.start()

    # ------------------------------------------------------------------ 探测
    def scan_ports(self) -> list[dict[str, Any]]:
        """列出候选串口，给配置的串口下拉框用。"""
        return list_serial_ports()

    def local_ips(self) -> list[str]:
        return list_local_ips()

    def discover(self) -> dict[str, Any]:
        """UDP 通道下广播探测模块数量与状态；非 UDP 通道直接说明原因。"""
        transport = self._transport
        if not isinstance(transport, UdpAtTransport):
            return {"ok": False, "count": 0, "detail": "只有 UDP 通道支持探测", "modules": []}
        try:
            n = transport.discover()
        except Exception as exc:
            return {"ok": False, "count": 0, "detail": str(exc), "modules": []}
        modules = transport.modules()
        online = [m for m in modules if m["online"]]
        macs = "、".join(m["mac"] for m in modules)
        detail = (
            f"在线 {len(online)} 个模块（共 {len(modules)} 个）：{macs}"
            if online
            else "无模块响应 op1 发现，检查唤醒/网线/防火墙"
        )
        return {"ok": n > 0, "count": n, "detail": detail, "modules": modules}

    def at_modules(self) -> list[dict[str, Any]]:
        """UDP 通道的模块表（mac/online/seen_ago_s/source_ip）；其它通道回空列表。"""
        transport = self._transport
        if isinstance(transport, UdpAtTransport):
            try:
                return transport.modules()
            except Exception:
                return []
        return []

    def select_device(self, mac: str) -> dict[str, Any]:
        """选定 AT 通道的目标模块：写 AtClient.target_mac 并清遥测累积（下轮全量重读）。

        空 mac 清空选定、回退传输层自动兜底。只有 UDP 通道支持按 MAC 定向，其它
        通道会清空目标并回 ok=False；UDP 下选中模块暂不在模块表不算失败——write
        时会补一次定向握手，只在 detail 里提示。
        """
        transport = self._transport
        raw = self.at.set_target_mac(mac)
        reset = getattr(self.monitor, "reset", None)
        if callable(reset):
            reset()
        text = format_mac(raw) if raw else ""
        if not isinstance(transport, UdpAtTransport):
            if raw:
                self.at.set_target_mac("")
                text = ""
            return {
                "ok": False,
                "mac": "",
                "known": False,
                "detail": f"当前通道 {self.channel} 不支持按 MAC 选择模块",
            }
        if not text:
            return {"ok": True, "mac": "", "known": True, "detail": "已清空定向目标，回到自动兜底"}
        known = any(m["mac"] == text and m["online"] for m in transport.modules())
        detail = (
            f"已定向模块 {text}"
            if known
            else f"模块 {text} 尚未在模块表，发送时补定向握手"
        )
        return {"ok": True, "mac": text, "known": known, "detail": detail}

    # ------------------------------------------------------------------ 遥测
    def poll_once(self, *, publish: bool = True) -> Any:
        return self.monitor.poll_once(publish=publish)

    def start_polling(self) -> bool:
        self._ticker.start()
        return self.monitor.start()

    def stop_polling(self) -> None:
        self.monitor.stop()

    def set_poll_names(self, names: list[str]) -> list[str]:
        self.monitor.set_names(names)
        return self.monitor.names

    def _tick(self) -> None:
        """轮询线程不查过期，这里补一个节拍；顺带把快照年龄广播出去给 UI 灰显。"""
        if self.monitor.polls == 0:
            return
        self.monitor.check_stale()

    # ------------------------------------------------------------------ 事件
    @staticmethod
    def _detail(transport: TransportLike) -> str:
        try:
            info = transport.info()
        except Exception:
            return ""
        return str(info.get("detail") or info.get("port") or info.get("target") or "")

    def _on_transport_state(self, state: object, detail: str = "") -> None:
        value = state if isinstance(state, TransportState) else TransportState(state)
        with self._lock:
            changed = value is not self._last_state
            self._last_state = value
            listeners = list(self._state_listeners)
        if value is TransportState.ERROR and detail:
            self._open_error = detail
        if changed and self.bus is not None:
            self.bus.publish(
                Events.TRANSPORT_STATE,
                {
                    "ts": time.time(),
                    "kind": "at",
                    "name": str(getattr(self._transport, "name", self.channel)),
                    "state": value.value,
                    "detail": str(detail),
                    "channel": self.channel,
                    "endpoint": self.endpoint,
                },
            )
        for fn in listeners:
            try:
                fn(value, str(detail))
            except Exception:
                self.log.exception("通道状态转发异常")
        if value is TransportState.OPEN:
            self.demux.start()
            self._ticker.start()

    def _alarm(self, code: str, message: str, level: str = AlarmLevel.WARN.value) -> None:
        if self.bus is None:
            return
        self.bus.publish(
            Events.ALARM,
            AlarmEvent(
                ts=time.time(),
                level=level,
                source=f"link/{self.endpoint}",
                code=code,
                message=message,
            ),
        )

    # ------------------------------------------------------------------ 汇总
    def stats(self) -> dict[str, Any]:
        transport = self._transport
        out: dict[str, Any] = {
            "endpoint": self.endpoint,
            "channel": self.channel,
            "state": self.state.value,
            "open": self.is_open,
            "detail": self._detail(transport) if transport else "",
            "error": self._open_error,
            "demux": self.demux.stats(),
            "at": self.at.stats(),
            "link": self.monitor.stats(),
            "ticker": self._ticker.stats(),
            "passthrough_channel": self.passthrough_channel,
            "passthrough_split": self.passthrough_split,
        }
        if self.passthrough_split:
            out["pt_open"] = self.pt_is_open
            out["pt_state"] = self.pt_state.value
            out["pt_error"] = self._pt_open_error
            out["pt_demux"] = self.pt_demux.stats() if self.pt_demux else {}
        return out

    def summary(self) -> dict[str, Any]:
        at = self.at.stats()
        link = self.monitor.stats()
        return {
            "channel": self.channel,
            "state": self.state.value,
            "open": self.is_open,
            "at_sent": at.get("sent"),
            "at_error": at.get("error"),
            "at_timeout": at.get("timeout"),
            "polls": link.get("polls"),
            "stale": link.get("stale"),
            "age_s": link.get("age_s"),
            "error": self._open_error,
        }

    def __repr__(self) -> str:
        return f"<LinkService {self.endpoint} channel={self.channel} state={self.state.value}>"
