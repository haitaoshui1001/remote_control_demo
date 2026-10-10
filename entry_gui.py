"""打包版 GUI 单入口（PyInstaller 用），也是开发环境的等价启动器。

为什么单独一个文件而不是复用 run.py：run.py 带 argparse 子命令，打包成
无控制台的 windowed exe 后命令行参数没有意义；交付版只需要"双击就开界面"。

冻结环境下的三件关键事（顺序不能换）：
1. chdir 到 exe 所在目录——config/、logs/、data/ 都按相对路径解析
   （hmi.core.config.project_root 兜底就是 cwd），双击启动时 Windows
   本来会把 cwd 设成 exe 目录，但从别的目录以命令行拉起时不会，这里
   显式 chdir 保证两种起法行为一致。
2. 自检先落盘 logs/startup.log——缺 VC 运行库、缺 ffmpeg DLL 这类问题
   发生在弹窗之前，必须有一个"还没来得及崩就已经写下"的现场。
3. 兜底 except 写 logs/crash.log 并弹窗——windowed exe 没有控制台，
   不接住异常的话用户只能看到进程消失。
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = _app_dir()
os.chdir(APP_DIR)
# 冻结版的 __file__ 在 _internal 里，向上找 src/hmi 会跑到构建目录去，
# 把 logs/、data/ 落到交付目录之外；显式钉住根目录，配置与日志都认 exe 同级。
os.environ["EWD611_HOME"] = str(APP_DIR)

# 开发环境（非冻结）需要手动把 src 挂进 sys.path；冻结后 hmi 已打进包内。
_SRC = APP_DIR / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

LOGS = APP_DIR / "logs"


def _pre_selfcheck() -> list[str]:
    """启动即落盘的自检：解释器、依赖导入、配置文件是否在位。"""
    LOGS.mkdir(parents=True, exist_ok=True)
    lines = [
        f"[{datetime.now():%Y-%m-%d %H:%M:%S}] "
        f"frozen={getattr(sys, 'frozen', False)} "
        f"python={sys.version.split()[0]} exe={sys.executable} cwd={os.getcwd()}"
    ]
    problems: list[str] = []
    for mod in ("yaml", "serial", "numpy", "PIL", "av", "PyQt5.QtWidgets"):
        try:
            __import__(mod)
            lines.append(f"import {mod}: OK")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"import {mod}: FAIL {exc!r}")
            problems.append(f"{mod}: {exc}")
    # ElementTree 导入永远成功，真正加载 pyexpat/libexpat 是在第一次解析时；
    # 打包收错 libexpat.dll 只有解析一次才暴露，而读摄像头参数全靠它。
    # 只记不拦：视频显示与 485 控制不依赖 XML，不能因解析器缺失开不了机。
    try:
        import xml.etree.ElementTree as ET

        ET.fromstring("<a/>")
        lines.append("xml expat parse: OK")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"xml expat parse: FAIL {exc!r}（摄像头参数读取不可用）")
    # 页面是 importlib 动态加载的，打包漏收不会报错、只会静默变占位页，
    # 所以这里按注册表逐页真导入一遍，把"占位页"这种降级提前暴露出来。
    try:
        from hmi.ui.pages import PAGES

        for spec in PAGES:
            name = f"hmi.ui.pages.{spec.module}"
            try:
                module = __import__(name, fromlist=[spec.cls])
                ok = getattr(module, spec.cls, None) is not None
            except Exception as exc:  # noqa: BLE001
                lines.append(f"import {name}: FAIL {exc!r}")
                problems.append(f"{name}: {exc}")
                continue
            if ok:
                lines.append(f"import {name}: OK")
            else:
                lines.append(f"import {name}: FAIL 缺 {spec.cls}")
                problems.append(f"{name} 缺 {spec.cls}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"import hmi.ui.pages: FAIL {exc!r}")
        problems.append(f"hmi.ui.pages: {exc}")
    cfg = APP_DIR / "config" / "app.yaml"
    if cfg.is_file():
        lines.append("config/app.yaml: OK")
    else:
        lines.append("config/app.yaml: MISSING（将用内置默认值，现场请放好配置）")
    (LOGS / "startup.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return problems


def main() -> int:
    problems = _pre_selfcheck()
    if problems:
        _fatal("依赖自检未通过", "；".join(problems), None)
        return 1
    try:
        from hmi.ui.app import run_app

        return int(run_app())
    except Exception:  # noqa: BLE001
        _fatal("程序启动失败", traceback.format_exc(), traceback.format_exc())
        return 1


def _fatal(title: str, detail: str, tb: str | None) -> None:
    if tb:
        try:
            (LOGS / "crash.log").write_text(tb, encoding="utf-8")
        except OSError:
            pass
    try:
        from PyQt5.QtWidgets import QApplication, QMessageBox

        _app = QApplication.instance() or QApplication([])
        QMessageBox.critical(None, title, f"{detail[:600]}\n\n详见 logs\\crash.log")
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    sys.exit(main())
