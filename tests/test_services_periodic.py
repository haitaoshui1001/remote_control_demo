"""节拍器 Ticker 的单元测试。

Ticker 是服务层所有"每秒刷一次"需求的地基，行为契约有三条最要命：
回调异常必须被吞掉并计数（节拍线程一死界面就静默停摆）、start/stop
幂等、trigger() 在调用线程同步执行。这里全部用短间隔 + 轮询断言，
不依赖 sleep 精确计时。
"""

from __future__ import annotations

import threading
import time

from hmi.services.periodic import Ticker


def _wait_until(pred, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# ------------------------------------------------------------------ 构造
def test_interval_clamped_to_floor():
    t = Ticker("clamped", lambda: None, 0.001)
    assert t.interval_s == 0.05


def test_repr_contains_name_and_interval():
    t = Ticker("repr-demo", lambda: None, 0.5)
    assert "repr-demo" in repr(t) and "0.5" in repr(t)


# ------------------------------------------------------------------ 同步触发
def test_trigger_runs_in_calling_thread():
    seen: list[int] = []
    t = Ticker("trig", lambda: seen.append(threading.get_ident()), 10.0)
    assert t.trigger() is True
    assert seen == [threading.get_ident()]
    assert t.runs == 1 and t.errors == 0


def test_trigger_counts_error_and_swallows():
    def boom():
        raise RuntimeError("节拍炸了")

    t = Ticker("boom", boom, 10.0)
    assert t.trigger() is True
    assert t.runs == 0 and t.errors == 1
    assert "节拍炸了" in t.last_error
    assert t.last_ts > 0


def test_repeated_start_is_idempotent():
    t = Ticker("idempotent", lambda: None, 0.05)
    assert t.start() is True
    assert t.start() is True
    t.stop()
    assert t.running is False


# ------------------------------------------------------------------ 线程循环
def test_loop_runs_repeatedly_and_stop():
    t = Ticker("loop", lambda: None, 0.05)
    t.start()
    assert _wait_until(lambda: t.runs >= 2)
    t.stop()
    assert t.running is False
    after = t.runs
    time.sleep(0.12)
    assert t.runs == after


def test_loop_survives_callback_errors():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] % 2:
            raise ValueError("奇数次失败")

    t = Ticker("flaky", flaky, 0.05)
    t.start()
    assert _wait_until(lambda: calls["n"] >= 4)
    t.stop()
    assert t.errors >= 2
    assert t.running is False
    assert "奇数次失败" in t.last_error


def test_initial_delay_defers_first_run():
    t = Ticker("delayed", lambda: None, 0.05, initial_delay_s=0.15)
    t.start()
    assert t.runs == 0
    assert _wait_until(lambda: t.runs >= 1, timeout=2.0)
    t.stop()


def test_stop_then_start_again_works():
    t = Ticker("restart", lambda: None, 0.05)
    t.start()
    assert _wait_until(lambda: t.runs >= 1)
    t.stop()
    t.start()
    assert _wait_until(lambda: t.running)
    t.stop()
    assert not t.running


# ------------------------------------------------------------------ 状态汇总
def test_stats_shape():
    t = Ticker("stat", lambda: None, 0.25)
    t.trigger()
    st = t.stats()
    assert st["name"] == "stat"
    assert st["interval_s"] == 0.25
    assert st["running"] is False
    assert st["runs"] == 1 and st["errors"] == 0
    assert set(st) == {
        "name", "running", "interval_s", "runs", "errors", "last_ts", "last_error"
    }
