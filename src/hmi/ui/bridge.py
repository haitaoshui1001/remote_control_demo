"""事件总线 → Qt 主线程的桥：跨线程回调只写槽位，界面定时合并刷新。

线程模型是这里唯一的核心约定。``core.events.EventBus.publish`` 的回调跑在
**发布者线程**上（串口读线程、RTSP 解码线程、遥测节拍线程、落盘线程都可能），
而 Qt 只允许主线程碰控件，所以回调里既不能发信号也不能改界面。分三类处理：

=============  ============================================= ============
类别            主题                                          处理方式
=============  ============================================= ============
合并类          ``VIDEO_STATS`` / ``LINK_SNAPSHOT`` /         ``LatestSlot``：
                ``RADIO_PARAMS`` / ``PT_STATS`` /             只留最新一条
                ``TRANSPORT_STATE`` / ``CMD_RESULT`` /
                ``PT_RESULT`` / ``TELEMETRY_SAMPLE`` / ``MOTION_STATE``
流水类          ``AT_EXCHANGE`` / ``PT_BYTES`` /              有界 deque，
                ``ALARM`` / ``LOG``                           每 tick 限量排空
拉取类          ``VIDEO_FRAME``                               **不进桥**
=============  ============================================= ============

拉取类为什么不进桥：25fps 的帧塞进事件总线会把总线和 Qt 队列一起打满，而界面
根本不需要每一帧。``services.video.frames`` 本来就是 pull 模型，视频页用自己的
定时器直接拉最新帧即可。

流水类为什么要限量：真机上"485 有高速设备在刷数据 + 日志同时刷屏"是可能发生的，
一次 tick 排不完就计数丢弃，绝不让 Qt 事件队列无限膨胀——那表现成整个窗口卡死。
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from ..core.events import EventBus, Events, LatestSlot

__all__ = ["UiBridge", "COALESCED_TOPICS", "STREAM_TOPICS", "PULLED_TOPICS"]

#: 只要最新值的主题（状态、指标、快照）。
COALESCED_TOPICS: tuple[str, ...] = (
    Events.VIDEO_STATS,
    Events.LINK_SNAPSHOT,
    Events.RADIO_PARAMS,
    Events.PT_STATS,
    Events.TRANSPORT_STATE,
    Events.CMD_RESULT,
    Events.PT_RESULT,
    Events.TELEMETRY_SAMPLE,
    Events.MOTION_STATE,
    Events.CAMERA_CFG,
)

#: 一条都不该悄悄丢掉的主题（报文流水、告警、日志）。
STREAM_TOPICS: tuple[str, ...] = (
    Events.AT_EXCHANGE,
    Events.PT_BYTES,
    Events.ALARM,
    Events.LOG,
)

#: 由页面自己定时拉取、刻意不过桥的主题；写在这里是为了"漏接"时有处可查。
PULLED_TOPICS: tuple[str, ...] = (Events.VIDEO_FRAME,)


class UiBridge(QObject):
    """把总线事件转成两个 Qt 信号，页面按主题名分流。

    - ``slot_updated(topic)``：合并类主题有了新版本。页面用 ``latest(topic)`` 取值。
    - ``batch_ready(topic, items)``：流水类主题本次排空的一批（可能 1 条也可能 300 条）。

    两个信号都只在主线程发出，因此可以直接连到控件槽函数。
    """

    slot_updated = pyqtSignal(str)
    batch_ready = pyqtSignal(str, list)

    def __init__(
        self,
        bus: EventBus,
        *,
        interval_ms: int = 200,
        stream_max: int = 2000,
        drain_budget: int = 400,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._bus = bus
        self._interval_ms = max(50, int(interval_ms))
        self._drain_budget = max(16, int(drain_budget))
        self._slots: dict[str, LatestSlot] = {t: LatestSlot() for t in COALESCED_TOPICS}
        self._queues: dict[str, deque[Any]] = {
            t: deque(maxlen=stream_max) for t in STREAM_TOPICS
        }
        self._seen: dict[str, int] = {t: 0 for t in COALESCED_TOPICS}
        self._latest: dict[str, Any] = {t: None for t in (*COALESCED_TOPICS, *STREAM_TOPICS)}
        self._dropped: dict[str, int] = {t: 0 for t in STREAM_TOPICS}
        self._lock = threading.Lock()
        self._tokens: list[int] = []
        self._timer = QTimer(self)
        self._timer.setInterval(self._interval_ms)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------ 起停
    def start(self) -> None:
        """订阅总线并启动排空定时器；必须在 QApplication 之后调用。"""
        if self._tokens:
            return
        # 一个通配订阅接住所有主题，省得每加一个事件都要回来改一遍；
        # 不认识的负载直接忽略（比如 video/frame，走拉取模型）。
        self._tokens.append(self._bus.subscribe("*", self._on_event))
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        for token in self._tokens:
            self._bus.unsubscribe(token)
        self._tokens = []

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    @property
    def interval_ms(self) -> int:
        return self._interval_ms

    # ------------------------------------------------------------------ 总线回调
    def _on_event(self, topic: str, payload: object) -> None:
        """跑在发布者线程上：只许写槽位 / 入队，绝不碰 Qt。"""
        slot = self._slots.get(topic)
        if slot is not None:
            slot.set(payload)
            return
        with self._lock:
            queue = self._queues.get(topic)
            if queue is None:
                return
            if len(queue) >= queue.maxlen:
                # 溢出说明界面长时间排不过来，计数让日志页显示出来，
                # 而不是悄悄丢掉——静默丢数据比丢数据更难查。
                self._dropped[topic] += 1
                queue.popleft()
            queue.append(payload)

    # ------------------------------------------------------------------ 主线程排空
    def _tick(self) -> None:
        for topic, slot in self._slots.items():
            version, value = slot.take()
            if version == self._seen[topic]:
                continue
            self._seen[topic] = version
            self._latest[topic] = value
            self.slot_updated.emit(topic)
        for topic in self._queues:
            items = self._drain(topic, self._drain_budget)
            if not items:
                continue
            self._latest[topic] = items[-1]
            self.batch_ready.emit(topic, items)

    def _drain(self, topic: str, limit: int) -> list[Any]:
        with self._lock:
            queue = self._queues.get(topic)
            if queue is None:
                return []
            n = min(max(0, int(limit)), len(queue))
            return [queue.popleft() for _ in range(n)]

    # ------------------------------------------------------------------ 取值
    def latest(self, topic: str, default: Any = None) -> Any:
        """最近一次的值（合并类为最新一条，流水类为最后排到的一条）。

        主线程调用；首次 tick 之前返回 ``default``。
        """
        value = self._latest.get(topic, default)
        return default if value is None else value

    def drain(self, topic: str, limit: int | None = None) -> list[Any]:
        """主动排空某个流水主题，供"页面想自己掌握节奏"的场景使用。"""
        return self._drain(topic, self._drain_budget if limit is None else int(limit))

    def dropped(self, topic: str) -> int:
        with self._lock:
            return int(self._dropped.get(topic, 0))

    def stats(self) -> dict[str, Any]:
        """桥自身的水位，给日志页/排障用。"""
        with self._lock:
            queued = {t: len(q) for t, q in self._queues.items()}
            dropped = dict(self._dropped)
        return {
            "interval_ms": self._interval_ms,
            "drain_budget": self._drain_budget,
            "running": self.running,
            "queued": queued,
            "dropped": dropped,
            "coalesced": {t: self._seen[t] for t in self._slots},
            "bus_published": getattr(self._bus, "published", 0),
        }
