"""服务层共用的节拍器：一条线程一个回调，异常只计数不上抛。

服务层有好几个"每秒把指标发一次"的需求（视频码率、透传速率、录制刷盘）。
与其各写一遍线程样板，不如统一到这里；回调里抛出的异常一律被吞掉并计数，
因为节拍线程一旦死掉，界面就会静默地停止刷新——这比报错更难查。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

LogFn = Callable[[], object]


class Ticker:
    """固定间隔执行 ``fn``；``trigger()`` 在调用线程立刻跑一次。"""

    def __init__(
        self,
        name: str,
        fn: LogFn,
        interval_s: float,
        *,
        log: logging.Logger | None = None,
        initial_delay_s: float = 0.0,
    ) -> None:
        self.name = name
        self.fn = fn
        self.interval_s = max(0.05, float(interval_s))
        self.initial_delay_s = max(0.0, float(initial_delay_s))
        self.log = log or logging.getLogger(f"hmi.services.ticker.{name}")
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.runs = 0
        self.errors = 0
        self.last_ts = 0.0
        self.last_error = ""

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        """启动节拍；重复调用无副作用，返回是否在运行。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name=f"hmi-{self.name}", daemon=True)
            self._thread.start()
        return True

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        with self._lock:
            thread = self._thread
        return bool(thread and thread.is_alive())

    # ---------------------------------------------------------------------- 执行
    def _call(self) -> None:
        try:
            self.fn()
            self.runs += 1
        except Exception as exc:
            self.errors += 1
            self.last_error = str(exc)
            self.log.exception("%s 节拍回调异常：%s", self.name, exc)
        finally:
            self.last_ts = time.time()

    def trigger(self) -> bool:
        """在**调用线程**立刻执行一次（节拍线程可能正睡在间隔上，插队反而引入竞态）。"""
        before = self.runs + self.errors
        self._call()
        return (self.runs + self.errors) > before

    def _loop(self) -> None:
        if self.initial_delay_s and self._stop.wait(self.initial_delay_s):
            return
        nxt = time.monotonic()
        while not self._stop.is_set():
            self._call()
            nxt += self.interval_s
            delay = nxt - time.monotonic()
            if delay <= 0:
                # 回调超时：丢掉积压节拍直接重新起算，避免追帧把间隔压成 0。
                nxt = time.monotonic()
                delay = self.interval_s
            if self._stop.wait(delay):
                return

    # ---------------------------------------------------------------------- 状态
    def stats(self) -> dict[str, object]:
        return {
            "name": self.name,
            "running": self.running,
            "interval_s": self.interval_s,
            "runs": self.runs,
            "errors": self.errors,
            "last_ts": self.last_ts,
            "last_error": self.last_error,
        }

    def __repr__(self) -> str:
        return f"<Ticker {self.name} interval={self.interval_s}s runs={self.runs}>"
