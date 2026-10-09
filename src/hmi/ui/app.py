"""应用入口：建 QApplication → 装会话 → 起主窗 → 退出时按序拆干净。

``cli.main.cmd_gui`` 用 ``from ..ui.app import run_app`` 惰性导入本模块，因此这里的
签名必须是 ``run_app(config_path, *, mock=...)``——CLI 那边已经按这个形状在调。

三条不显眼但重要的规矩：

1. **总线桥先订阅，会话后启动**。否则 ``session.start()`` 里的串口/取流日志全落不进
   界面的日志页，看起来像"程序半路才开始记录"。
2. **启动失败不弹模态框**。串口被占用、摄像头没上电在现场是常态，界面要能带着这些
   失败继续用其他功能；失败信息只写状态栏和日志。
3. **必须替换 ``sys.excepthook``**。PyQt5 里被 C++ 回调的 Python 方法抛出未捕获异常会
   直接终止进程；装了自定义 hook 之后才会只记录并继续跑。长时间值守的软件不能因为
   某一次负载形状意外就整窗消失。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from PyQt5 import QtCore
from PyQt5.QtCore import QCoreApplication
from PyQt5.QtGui import QColor, QPalette
from PyQt5.QtWidgets import QApplication

from ..core.config import project_root
from ..core.logger import get_logger, setup_logging
from ..services.session import Session, build_session
from .bridge import UiBridge
from .main_window import MainWindow
from .theme import DARK_COLORS, set_font_px, style_sheet, ui_font

__all__ = ["run_app", "build_application"]


def _log_dir(cfg: Any) -> str:
    path = Path(str(cfg.app.log_dir))
    return str(path if path.is_absolute() else project_root() / path)


def _install_excepthook(log: logging.Logger) -> None:
    """让界面异常只记日志，不带走整个进程。"""

    def hook(kind: type[BaseException], value: BaseException, trace: Any) -> None:
        if kind is SystemExit:
            sys.__excepthook__(kind, value, trace)
            return
        log.critical("界面异常已记录并忽略：%s", value, exc_info=(kind, value, trace))

    sys.excepthook = hook


def _apply_look(app: QApplication, cfg: Any) -> int:
    """按配置定字号、贴样式表，必要时再上深色调色板；返回生效的正文像素字号。

    字号只有一个入口：``ui.font_size``。它先进 theme 模块换算出全套字体和间距，
    再落到 QApplication 的默认字体与样式表——顺序不能反，样式表里的 font-size
    会盖住默认字体，页面里的等宽控件则是在构造时向 theme 取字体，所以必须先定基准。
    """
    px = set_font_px(getattr(cfg.ui, "font_size", None))
    app.setStyle("Fusion")
    app.setFont(ui_font())
    app.setStyleSheet(style_sheet())
    if getattr(cfg.ui, "theme", "light") == "dark":
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(DARK_COLORS["window"]))
        palette.setColor(QPalette.Base, QColor(DARK_COLORS["base"]))
        palette.setColor(QPalette.AlternateBase, QColor(DARK_COLORS["button"]))
        palette.setColor(QPalette.Text, QColor(DARK_COLORS["text"]))
        palette.setColor(QPalette.WindowText, QColor(DARK_COLORS["text"]))
        palette.setColor(QPalette.Button, QColor(DARK_COLORS["button"]))
        palette.setColor(QPalette.ButtonText, QColor(DARK_COLORS["text"]))
        palette.setColor(QPalette.Highlight, QColor(DARK_COLORS["highlight"]))
        app.setPalette(palette)
    return px


#: 平台插件全落空时的人话指引——比 Qt 原生弹窗可读，并直接指明正确入口。
_PLUGIN_MISSING_HINT = (
    "找不到 Qt 平台插件目录（platforms/qwindows.dll），窗口无法启动。"
    "请改用项目根目录的 start_gui.bat 启动（它会固定用项目 conda 环境 "
    "ewd611-hmi 的 python 并注入 .deps/src）；若坚持手动运行，"
    "请确认 python 来自该环境且 PYTHONPATH 含 .deps 与 src，详见操作部署手册。"
)


def _ensure_plugin_paths() -> None:
    """把 PyQt5 自带目录里的插件路径补进 Qt 的搜索列表。

    pip 装的 PyQt5 wheel 靠包目录下那份 ``qt.conf`` 告诉 Qt 插件在哪；一旦换了安装
    布局（``pip install --target``、conda 版把插件放在环境根的 ``Library/plugins``、
    把包整个拷到现场机），``qt.conf`` 就没了。这时 Qt 只会去 ``python.exe`` 旁边找
    ``platforms``，找不到便在 Windows 上弹一句 "no Qt platform plugin could be
    initialized. Reinstalling the application may fix this problem" 后直接 abort——
    重装没用，插件其实就在磁盘上，只是没进搜索路径。这里按三种常见布局各探一次：
    pip wheel 的 ``<包>/Qt5/plugins``、老式 ``<包>/plugins``、conda 的
    ``<环境>/Library/plugins``。命中后除了 ``addLibraryPath``，再补一份
    ``QT_QPA_PLATFORM_PLUGIN_PATH`` 环境变量（Qt 查平台插件的第一优先级，双保险）；
    全部落空时抛人话异常，让控制台给出可读报错和指引，而不是留给原生弹窗。
    必须在建 QApplication 之前调用。
    """
    env_hit = os.environ.get("QT_QPA_PLATFORM_PLUGIN_PATH")
    if env_hit and Path(env_hit).is_dir():
        return
    for p in QCoreApplication.libraryPaths():
        if (Path(p) / "platforms").is_dir():
            return
    pkg = Path(QtCore.__file__).resolve().parent
    candidates = (
        pkg / "Qt5" / "plugins",
        pkg / "plugins",
        Path(sys.prefix) / "Library" / "plugins",
    )
    for candidate in candidates:
        if (candidate / "platforms").is_dir():
            QCoreApplication.addLibraryPath(str(candidate))
            os.environ.setdefault(
                "QT_QPA_PLATFORM_PLUGIN_PATH", str(candidate / "platforms")
            )
            return
    raise RuntimeError(_PLUGIN_MISSING_HINT)


def build_application(argv: list[str] | None = None) -> QApplication:
    """取现有 QApplication，没有就新建（pytest 与重复启动都走这条路）。"""
    app = QApplication.instance()
    if app is None:
        _ensure_plugin_paths()
        app = QApplication(list(argv or []))
    app.setApplicationName("EWD611 数传图传上位机")
    return app


def run_app(
    config_path: str | None = None,
    *,
    mock: bool | None = None,
    endpoint: str = "local",
    **overrides: Any,
) -> int:
    """启动图形界面并阻塞到窗口关闭，返回退出码。

    ``mock=None`` 表示"按配置文件决定"（``python run.py gui --real`` 传下来的是 False）。
    ``overrides`` 是点号路径形式的配置覆盖，与 CLI 的 ``--set`` 同源。
    """
    session: Session = build_session(
        config_path, mock=mock, endpoint=endpoint, **overrides
    )
    log = get_logger("hmi.ui.app")
    cfg = session.cfg
    setup_logging(cfg.app.log_level, log_dir=_log_dir(cfg), bus=session.bus)
    _install_excepthook(log)

    try:
        app = build_application()
    except RuntimeError as exc:
        log.error("界面无法启动：%s", exc)
        return 1
    font_px = _apply_look(app, cfg)

    bridge = UiBridge(session.bus, interval_ms=cfg.ui.poll_ui_ms)
    bridge.start()
    window: MainWindow | None = None
    try:
        result = session.start()
        window = MainWindow(session, bridge)
        notes = [str(x) for x in (result.get("detail") or [])]
        if notes:
            window.statusBar().showMessage("启动提示：" + "；".join(notes), 12000)
        log.info(
            "界面已启动：channel=%s mock=%s link=%s video=%s record=%s font=%spx",
            session.link.channel,
            session.mock,
            bool(result.get("ok")),
            bool(result.get("video")),
            bool(result.get("record")),
            font_px,
        )
        window.show()
        return int(app.exec_())
    finally:
        bridge.stop()
        stats = bridge.stats()
        session.stop()
        log.info(
            "界面已退出：总线共 %s 条，各主题丢弃计数 %s",
            stats["bus_published"],
            stats["dropped"],
        )
