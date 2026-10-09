"""线程安全事件总线：业务层与 UI 层通过主题解耦通信。

core / transport / protocol / services 一律不依赖 Qt，只向总线发布事件；
UI 层用 Qt 信号把事件跨线程转投主线程，从而实现"界面不认识硬件"。
"""

from __future__ import annotations

import fnmatch
import itertools
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

Handler = Callable[[str, object], None]
Predicate = Callable[[object], bool]

_log = logging.getLogger("hmi.bus")


class Events:
    """主题常量表，避免各处拼写字符串。"""

    VIDEO_FRAME = "video/frame"
    VIDEO_STATS = "video/stats"
    PT_BYTES = "passthrough/bytes"
    PT_STATS = "passthrough/stats"
    PT_RESULT = "passthrough/result"
    AT_EXCHANGE = "radio/at"
    RADIO_PARAMS = "radio/params"
    LINK_SNAPSHOT = "radio/link"
    TRANSPORT_STATE = "transport/state"
    CMD_RESULT = "command/result"
    ALARM = "system/alarm"
    LOG = "system/log"
    TELEMETRY_SAMPLE = "robot/telemetry"
    MOTION_STATE = "robot/motion"
    CAMERA_CFG = "camera/config"


@dataclass
class _Sub:
    token: int
    topic: str
    handler: Handler
    once: bool
    pattern: bool
    alive: bool = True


@dataclass
class _Waiter:
    event: threading.Event = field(default_factory=threading.Event)
    value: object = None
    predicate: Predicate | None = None

    def check(self, payload: object) -> bool:
        if self.predicate is not None:
            try:
                if not self.predicate(payload):
                    return False
            except Exception:
                return False
        self.value = payload
        self.event.set()
        return True


class EventBus:
    """极简发布/订阅，支持精确主题与通配主题（如 ``video/*``）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._exact: dict[str, list[_Sub]] = {}
        self._patterns: list[_Sub] = []
        self._all: list[_Sub] = []
        self._subs: dict[int, _Sub] = {}
        self._tokens = itertools.count(1)
        self._waiters: dict[int, list[_Waiter]] = {}
        self.published = 0

    def subscribe(self, topic: str, handler: Handler, *, once: bool = False) -> int:
        token = next(self._tokens)
        sub = _Sub(
            token=token,
            topic=topic,
            handler=handler,
            once=once,
            pattern=("*" in topic or "?" in topic),
        )
        with self._lock:
            self._subs[token] = sub
            if topic == "*":
                self._all.append(sub)
            elif sub.pattern:
                self._patterns.append(sub)
            else:
                self._exact.setdefault(topic, []).append(sub)
        return token

    def unsubscribe(self, token: int) -> bool:
        with self._lock:
            sub = self._subs.pop(token, None)
            if sub is None:
                return False
            sub.alive = False
            bucket = (
                self._all
                if sub.topic == "*"
                else (self._patterns if sub.pattern else self._exact.get(sub.topic, []))
            )
            if sub in bucket:
                bucket.remove(sub)
            return True

    def publish(self, topic: str, payload: object = None) -> int:
        with self._lock:
            targets: list[_Sub] = list(self._exact.get(topic, ()))
            targets += [s for s in self._patterns if fnmatch.fnmatch(topic, s.topic)]
            targets += list(self._all)
            waiters = list(self._waiters.get(topic, ()))
            self.published += 1
            expired = [w for w in waiters if w.check(payload)]
            for w in expired:
                lst = self._waiters.get(topic, [])
                if w in lst:
                    lst.remove(w)
            once_subs = [s for s in targets if s.once]
            for s in once_subs:
                self.unsubscribe(s.token)
        delivered = 0
        for sub in targets:
            try:
                sub.handler(topic, payload)
                delivered += 1
            except Exception as exc:
                _log.exception("订阅回调异常 topic=%s token=%s: %s", topic, sub.token, exc)
        return delivered

    def wait_for(
        self,
        topic: str,
        predicate: Predicate | None = None,
        timeout: float | None = 1.0,
    ) -> tuple[bool, object]:
        """同步等待某个主题的一次事件，供测试与协议层简单配对使用。"""
        waiter = _Waiter(predicate=predicate)
        with self._lock:
            self._waiters.setdefault(topic, []).append(waiter)
        got = waiter.event.wait(timeout) if timeout else waiter.event.wait()
        with self._lock:
            lst = self._waiters.get(topic, [])
            if waiter in lst:
                lst.remove(waiter)
        return bool(got), waiter.value

    def listeners(self, topic: str | None = None) -> int:
        with self._lock:
            if topic is None:
                return len(self._subs)
            return len(self._exact.get(topic, ()))

    def clear(self) -> None:
        with self._lock:
            self._exact.clear()
            self._patterns.clear()
            self._all.clear()
            self._subs.clear()
            self._waiters.clear()


class Throttle:
    """按主题键做时间窗节流，供高频事件转 UI 刷新时使用。"""

    def __init__(self, interval_s: float = 0.1) -> None:
        self.interval_s = max(0.0, float(interval_s))
        self._last: dict[str, float] = {}

    def allow(self, key: str = "__all__", ts: float | None = None) -> bool:
        ref = ts if ts is not None else time.monotonic()
        prev = self._last.get(key, 0.0)
        if ref - prev >= self.interval_s:
            self._last[key] = ref
            return True
        return False


class LatestSlot:
    """只保留最新值的槽位，UI 用定时器轮询，天然合并高频更新。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: object = None
        self.version = 0

    def set(self, value: object) -> None:
        with self._lock:
            self._value = value
            self.version += 1

    def get(self) -> object:
        with self._lock:
            return self._value

    def take(self) -> tuple[int, object]:
        with self._lock:
            return self.version, self._value


_bus: EventBus | None = None
_bus_lock = threading.Lock()


def get_bus(create: bool = True) -> EventBus | None:
    """返回进程级默认总线（多数场景够用），测试可自建实例。"""
    global _bus
    with _bus_lock:
        if _bus is None and create:
            _bus = EventBus()
        return _bus


def set_bus(bus: EventBus | None) -> None:
    global _bus
    with _bus_lock:
        _bus = bus
