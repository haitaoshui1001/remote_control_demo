"""UI 与总线之间那道桥的离线测试（不需要硬件，也不需要显示器）。

这里刻意不开完整会话（不占串口、不起取流线），只验证桥自己的四件事：
高频合并、流水不丢序、溢出留计数、信号只从主线程发出。桥是界面唯一的事件入口，
它错了整层 UI 都不可信，所以这一组用例比页面本身更早写。

调用 ``_on_event`` / ``_tick`` 是有意为之：前者代表"发布者线程刚跑完回调"，后者代表
"主线程的一次节拍"，直接按次序驱动比等 QTimer 触发确定得多。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from PyQt5.QtTest import QTest

from hmi.core.events import EventBus, Events, get_bus, set_bus
from hmi.ui.bridge import (
    COALESCED_TOPICS,
    PULLED_TOPICS,
    STREAM_TOPICS,
    UiBridge,
)

ROOT = Path(__file__).resolve().parents[1]


def make_bridge(bus: EventBus, **kwargs) -> UiBridge:
    return UiBridge(bus, **kwargs)


def test_topic_tables_are_disjoint():
    """三类主题不能重叠：合并类和流水类的排空策略完全相反。"""
    assert Events.VIDEO_FRAME in PULLED_TOPICS
    assert set(COALESCED_TOPICS).isdisjoint(STREAM_TOPICS)
    assert Events.LINK_SNAPSHOT in COALESCED_TOPICS
    assert Events.AT_EXCHANGE in STREAM_TOPICS


def test_coalesced_topics_keep_only_the_latest(qapp):
    """一个节拍内连发 10 条只留最新一条，界面每秒重画几十次没有意义。"""
    bus = EventBus()
    bridge = make_bridge(bus, interval_ms=50)
    bridge.start()
    fired: list[str] = []
    bridge.slot_updated.connect(fired.append)

    for i in range(10):
        bus.publish(Events.LINK_SNAPSHOT, {"seq": i})
    bridge._tick()

    assert fired == [Events.LINK_SNAPSHOT], "同一节拍内的多次更新应合并成一次信号"
    assert bridge.latest(Events.LINK_SNAPSHOT) == {"seq": 9}
    assert bridge.stats()["coalesced"][Events.LINK_SNAPSHOT] == 10
    bridge.stop()


def test_tick_without_new_events_is_silent(qapp):
    """没新内容就不该发信号——否则每个页面每秒白忙 5 次重绘。"""
    bridge = make_bridge(EventBus(), interval_ms=50)
    fired: list[str] = []
    bridge.slot_updated.connect(fired.append)
    bridge.batch_ready.connect(lambda topic, _items: fired.append(topic))

    bridge._tick()
    bridge._tick()

    assert fired == []


def test_stream_topics_are_delivered_in_order(qapp):
    """流水类一条都不能少、顺序不能乱——AT 报文乱序读出来就是错的。"""
    bridge = make_bridge(EventBus(), interval_ms=50, drain_budget=64)
    bridge.start()
    batches: list[list] = []
    bridge.batch_ready.connect(lambda _topic, items: batches.append(items))

    for i in range(20):
        bridge._on_event(Events.AT_EXCHANGE, {"line": i})
    bridge._tick()

    flat = [item for batch in batches for item in batch]
    assert [x["line"] for x in flat] == list(range(20))
    assert bridge.latest(Events.AT_EXCHANGE) == {"line": 19}
    bridge.stop()


def test_overflow_is_counted_not_silently_lost(qapp):
    """排不过来时丢最旧的，但必须留计数；静默丢数据比丢数据更难查。"""
    bridge = make_bridge(EventBus(), interval_ms=50, stream_max=5)

    for i in range(12):
        bridge._on_event(Events.PT_BYTES, {"i": i})

    assert bridge.dropped(Events.PT_BYTES) == 7
    assert bridge.stats()["queued"][Events.PT_BYTES] == 5
    assert [x["i"] for x in bridge.drain(Events.PT_BYTES)] == [7, 8, 9, 10, 11]


def test_drain_budget_limits_work_per_tick(qapp):
    """一次节拍排太多会把主线程占死，限量是界面不卡顿的前提。"""
    bridge = make_bridge(EventBus(), interval_ms=50, stream_max=200, drain_budget=16)
    sizes: list[int] = []
    bridge.batch_ready.connect(lambda _topic, items: sizes.append(len(items)))

    for i in range(40):
        bridge._on_event(Events.LOG, {"i": i})
    for _ in range(4):
        bridge._tick()

    assert sizes == [16, 16, 8], "第四次节拍应当无事可做"
    assert sum(sizes) == 40, "限量只该影响每轮多少，不该影响总数"


def test_pulled_topics_never_cross_the_bridge(qapp):
    """视频帧走拉取模型：25fps 进 Qt 队列等于自杀，必须挡在桥外。"""
    bus = EventBus()
    bridge = make_bridge(bus, interval_ms=50)
    bridge.start()
    fired: list[str] = []
    bridge.slot_updated.connect(fired.append)
    bridge.batch_ready.connect(lambda topic, _items: fired.append(topic))

    bus.publish(Events.VIDEO_FRAME, object())
    bridge._tick()

    assert fired == []
    assert bus.published == 1, "总线确实收到了，只是不过桥"
    assert bridge.latest(Events.VIDEO_FRAME, "无") == "无"
    bridge.stop()


def test_publish_from_worker_thread_emits_on_main_thread(qapp):
    """核心线程契约：串口读线程发布的事件，只能在主线程转成 Qt 信号。"""
    bus = EventBus()
    bridge = make_bridge(bus, interval_ms=20)
    main_ident = threading.get_ident()
    idents: list[int] = []
    bridge.slot_updated.connect(lambda _topic: idents.append(threading.get_ident()))
    bridge.start()

    worker = threading.Thread(target=lambda: bus.publish(Events.VIDEO_STATS, {"fps": 12}))
    worker.start()
    worker.join()

    for _ in range(25):
        QTest.qWait(20)
        if idents:
            break

    assert idents, "后台线程发布的事件最终要在主线程被收到"
    assert set(idents) == {main_ident}, "信号绝不能在工作线程发出"
    assert bridge.latest(Events.VIDEO_STATS) == {"fps": 12}
    bridge.stop()


def test_stop_unsubscribes_and_is_idempotent(qapp):
    """退出时不摘订阅，反复起停会攒一堆僵尸回调，每次事件都白跑一遍。"""
    bus = EventBus()
    bridge = make_bridge(bus, interval_ms=50)
    assert bus.listeners() == 0

    bridge.start()
    bridge.start()
    assert bus.listeners() == 1, "重复 start 不该重复订阅"

    bridge.stop()
    assert bus.listeners() == 0
    assert bridge.running is False
    bridge.stop()


def test_latest_returns_default_before_first_value(qapp):
    bridge = make_bridge(EventBus(), interval_ms=50)
    assert bridge.latest(Events.RADIO_PARAMS) is None
    assert bridge.latest(Events.RADIO_PARAMS, "未读取") == "未读取"


def test_interval_is_clamped(qapp):
    """界面轮询低于 50ms 只会放大抖动，配置写错时桥自己兜底。"""
    assert make_bridge(EventBus(), interval_ms=1).interval_ms == 50


def test_stats_shape_for_status_bar(qapp):
    """主窗口状态栏按这几个键取值，键名改了要一起改。"""
    bridge = make_bridge(EventBus(), interval_ms=200)
    stats = bridge.stats()
    assert stats["interval_ms"] == 200
    assert stats["drain_budget"] == 400
    assert stats["running"] is False
    assert stats["dropped"] == {topic: 0 for topic in STREAM_TOPICS}
    assert set(stats["queued"]) == set(STREAM_TOPICS)


def test_importing_ui_package_does_not_load_qt():
    """自检与 pytest 不该被 PyQt5 拖住：run_app 必须延迟导出。"""
    code = "import sys, hmi.ui; print(any(m.startswith('PyQt5') for m in sys.modules))"
    env = dict(os.environ)
    src = str(ROOT / "src")
    env["PYTHONPATH"] = src if not env.get("PYTHONPATH") else src + os.pathsep + env["PYTHONPATH"]
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("False"), "import hmi.ui 不应把 Qt 拖进来"


def test_lazy_export_still_rejects_unknown_names():
    import hmi.ui

    assert hmi.ui.__all__ == ["run_app"]
    with pytest.raises(AttributeError):
        _ = hmi.ui.NoSuchThing


def test_default_bus_is_replaceable():
    """装配根靠进程级默认总线串起来，测试必须能换掉再换回来。"""
    original = get_bus()
    try:
        set_bus(EventBus())
        assert get_bus() is not original
    finally:
        set_bus(original)
    assert get_bus() is original
