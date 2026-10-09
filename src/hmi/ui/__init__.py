"""图形界面层：依赖只到 services 为止，绝不碰 protocol / transport。

分层到这里是单向的：``ui -> services -> protocol -> transport -> core``。
页面想拿数据只有两条路——调 ``session`` 上的服务，或订阅 ``bridge`` 的刷新信号。
core 是各层共用的契约层（事件主题、数据模型、配置、日志），跨层引用它不算破窗；
但任何 ``from ..protocol import`` / ``from ..transport import`` 都是设计违规。

``run_app`` 用延迟导出：``import hmi.ui`` 不该顺手把 PyQt5 拖进来（自检脚本、
pytest 都不需要 Qt），CLI 里 ``from ..ui.app import run_app`` 会显式走到 app 模块。
"""

from __future__ import annotations

from typing import Any

__all__ = ["run_app"]


def __getattr__(name: str) -> Any:
    if name == "run_app":
        from .app import run_app

        return run_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
