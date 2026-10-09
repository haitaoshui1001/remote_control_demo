"""传输层抽象：向上只暴露"字节进出 + 状态变化"，隐藏具体硬件差异。"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from ..core.models import FrameMeta, TransportState
from ..core.utils import RateMeter

BytesListener = Callable[[bytes], None]
StateListener = Callable[[TransportState, str], None]
FrameListener = Callable[[object, FrameMeta], None]


def _drop(bucket: list, fn: object) -> bool:
    """从监听器列表里摘掉一个；幂等，返回是否真的删除了。"""
    try:
        bucket.remove(fn)
        return True
    except ValueError:
        return False


class ByteTransport:
    """双向字节通道基类（串口、UDP-AT、仿真链路都实现它）。"""

    #: 支持应用层定向的通道（如 UDP-AT 的帧头 dst MAC）在自己的类里置 True；
    #: AtClient 只在通道确实支持时才传 target_mac 参数，其余通道签名不变。
    supports_target = False

    def __init__(self, name: str = "byte", *, stats_window_s: float = 5.0) -> None:
        self.name = name
        self.log = logging.getLogger(f"hmi.transport.{name}")
        self._state = TransportState.CLOSED
        self._detail = ""
        self._lock = threading.RLock()
        self._bytes_ls: list[BytesListener] = []
        self._state_ls: list[StateListener] = []
        self._in_meter = RateMeter(stats_window_s)
        self._out_meter = RateMeter(stats_window_s)
        self._rxq: deque[bytes] = deque(maxlen=256)
        self._rx_cv = threading.Condition()
        self.bytes_in = 0
        self.bytes_out = 0
        self.packets_in = 0
        self.packets_out = 0
        self.last_in_ts = 0.0
        self.last_out_ts = 0.0
        self.errors = 0

    def add_bytes_listener(self, fn: BytesListener) -> None:
        self._bytes_ls.append(fn)

    def remove_bytes_listener(self, fn: BytesListener) -> bool:
        return _drop(self._bytes_ls, fn)

    def add_state_listener(self, fn: StateListener) -> None:
        self._state_ls.append(fn)

    def remove_state_listener(self, fn: StateListener) -> bool:
        return _drop(self._state_ls, fn)

    @property
    def state(self) -> TransportState:
        return self._state

    @property
    def state_detail(self) -> str:
        return self._detail

    def is_open(self) -> bool:
        return self._state is TransportState.OPEN

    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def write(self, data: bytes) -> int:
        raise NotImplementedError

    def read_once(self, max_bytes: int = 4096, timeout_ms: int = 50) -> bytes:
        """从接收队列取一段字节。

        读线程是唯一的 OS 读取者，这里只消费它投递的副本，因此与事件监听
        并存而不会互相抢字节；主要给 tools/ 脚本和无监听器的自检场景用。
        """
        deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000.0
        buf = bytearray()
        with self._rx_cv:
            while not self._rxq:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    return b""
                self._rx_cv.wait(remain)
                if self._stop_flag():
                    break
            self._harvest(buf, max_bytes)
        settle = min(0.03, max(0.0, deadline - time.monotonic()))
        if settle and len(buf) < max_bytes:
            time.sleep(settle)
            with self._rx_cv:
                self._harvest(buf, max_bytes)
        return bytes(buf)

    def clear_pending(self) -> int:
        """丢弃队列里积压的字节（发指令前清场用），返回丢弃字节数。"""
        with self._rx_cv:
            dropped = sum(len(c) for c in self._rxq)
            self._rxq.clear()
            return dropped

    def _harvest(self, buf: bytearray, max_bytes: int) -> None:
        """必须在持有 _rx_cv 时调用：搬运队列字节，超出的部分退回队列。"""
        while self._rxq and len(buf) < max_bytes:
            chunk = self._rxq.popleft()
            room = max_bytes - len(buf)
            if len(chunk) > room:
                self._rxq.appendleft(chunk[room:])
                chunk = chunk[:room]
            buf.extend(chunk)

    def _stop_flag(self) -> bool:
        stop = getattr(self, "_stop", None)
        return bool(stop and stop.is_set())

    def info(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "name": self.name,
            "state": self._state.value,
            "detail": self._detail,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "packets_in": self.packets_in,
            "packets_out": self.packets_out,
            "in_rate_bps": round(self._in_meter.rate(now), 1),
            "out_rate_bps": round(self._out_meter.rate(now), 1),
            "last_in_ts": self.last_in_ts,
            "last_out_ts": self.last_out_ts,
            "errors": self.errors,
        }

    def __enter__(self) -> ByteTransport:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.close()
        return False

    def _set_state(self, state: TransportState, detail: str = "") -> None:
        changed = state is not self._state or detail != self._detail
        self._state = state
        self._detail = detail
        if changed:
            self.log.debug("状态 -> %s (%s)", state.value, detail)
            for fn in list(self._state_ls):
                try:
                    fn(state, detail)
                except Exception:
                    self.log.exception("状态回调异常: %s", state.value)

    def _emit_bytes(self, data: bytes) -> None:
        if not data:
            return
        ts = time.time()
        self.bytes_in += len(data)
        self.packets_in += 1
        self.last_in_ts = ts
        self._in_meter.add(len(data))
        with self._rx_cv:
            self._rxq.append(data)
            self._rx_cv.notify_all()
        for fn in list(self._bytes_ls):
            try:
                fn(data)
            except Exception:
                self.log.exception("字节回调异常 len=%s", len(data))

    def _note_write(self, data: bytes) -> None:
        self.bytes_out += len(data)
        self.packets_out += 1
        self.last_out_ts = time.time()
        self._out_meter.add(len(data))

    def _note_error(self, detail: str) -> None:
        self.errors += 1
        self.log.warning("%s: %s", self.name, detail)


class FrameChannel:
    """解码线程 → UI 的帧通道：最新帧槽 + 有界放帧队列（抗抖动缓冲）。

    只留最新一帧的旧模型会被无线图传的突发交付吃掉帧率：一批连到好几帧，
    每个 tick 只来得及看一眼，显示 fps 就被压成“批次频率”（实测 20fps 的
    源只剩 4~7fps）。新模型里每帧照常落进最新帧槽（抓拍 peek 不受影响），
    同时排队，消费者按 ``pull_next()`` 一个节拍发一帧，把突发抹平成连续播放；
    队列有深度上限，满了顶掉最旧的一帧并记一次丢帧。

    队列是“抗抖动缓冲”，但缓冲本身也是延迟：``max_dwell_ms`` 给队首帧设
    停留预算，一旦真实滞留超预算（消费跟不上直播），pull_next 直接丢掉
    积压放最新一帧——用一次跳帧追回延迟，跳过的帧记进 ``skipped`` 账。
    预算为 0 时关闭该行为，退回纯 FIFO。
    """

    def __init__(self, max_q: int = 5, max_dwell_ms: float = 0.0) -> None:
        self._lock = threading.Lock()
        self._image: object = None
        self._meta: FrameMeta | None = None
        self._enqueue_mono = 0.0
        self._version = 0
        self._pulled = 0
        self._watching = True
        self._max_q = max(0, int(max_q))
        self._max_dwell_ms = max(0.0, float(max_dwell_ms))
        self._skipped = 0
        self._q: deque = deque()

    def set_watching(self, watching: bool) -> None:
        """UI 页面显隐时声明“有没有人看”；无人看时的覆盖不算丢帧，积压作废。"""
        with self._lock:
            self._watching = bool(watching)
            if not self._watching:
                self._q.clear()

    def publish(self, image: object, meta: FrameMeta) -> bool:
        """发布一帧：最新帧槽必更新（peek/抓拍用），有人看时再入队一份。

        返回 True 表示确实丢了一帧——队列满，最旧的一帧被顶掉。
        无人观看时视同已被消费，不入队也不计丢帧。
        """
        with self._lock:
            unpulled = self._version != self._pulled
            self._image = image
            self._meta = meta
            self._enqueue_mono = time.monotonic()
            self._version += 1
            if not self._watching:
                self._pulled = self._version
                return False
            if self._max_q <= 0:
                return unpulled
            dropped = len(self._q) >= self._max_q
            if dropped:
                self._q.popleft()
            self._q.append((image, meta, self._enqueue_mono))
            return dropped

    def pull(self) -> tuple[object, FrameMeta, float] | None:
        """取“最新一帧”并清空积压队列：只要现况的旧式消费者（自检脚本）用。"""
        with self._lock:
            self._q.clear()
            if self._image is None or self._version == self._pulled:
                return None
            self._pulled = self._version
            dwell = (time.monotonic() - self._enqueue_mono) * 1000.0
            return self._image, self._meta, dwell

    def pull_next(self) -> tuple[object, FrameMeta, float] | None:
        """按到达顺序取最早的一帧（UI 每节拍一次）；没有可发的返回 None。

        队首帧的真实滞留超过停留预算时，说明消费已经跟不上直播：把中间
        积压整段丢弃、直接放最新一帧，一次跳帧追回延迟（逐帧记 skipped 账）。
        """
        with self._lock:
            now = time.monotonic()
            if self._q:
                if (
                    self._max_dwell_ms > 0.0
                    and len(self._q) > 1
                    and (now - self._q[0][2]) * 1000.0 > self._max_dwell_ms
                ):
                    self._skipped += len(self._q) - 1
                    image, meta, mono = self._q.pop()
                    self._q.clear()
                    self._pulled = self._version
                    return image, meta, (now - mono) * 1000.0
                image, meta, mono = self._q.popleft()
                if not self._q:
                    self._pulled = self._version
                return image, meta, (now - mono) * 1000.0
            if self._image is None or self._version == self._pulled:
                return None
            self._pulled = self._version
            dwell = (now - self._enqueue_mono) * 1000.0
            return self._image, self._meta, dwell

    def peek(self) -> tuple[object, FrameMeta, float] | None:
        """看不消费的最新一帧，供抓拍/录像用；返回 (图像, 元信息, 停留毫秒)。"""
        with self._lock:
            if self._image is None:
                return None
            dwell = (time.monotonic() - self._enqueue_mono) * 1000.0
            return self._image, self._meta, dwell

    def latest_meta(self) -> FrameMeta | None:
        with self._lock:
            return self._meta

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    @property
    def skipped(self) -> int:
        """累计“积压跳帧”数：停留超预算被丢弃的过期帧，与溢出丢帧分账。"""
        with self._lock:
            return self._skipped
