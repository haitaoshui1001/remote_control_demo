import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    """离屏 QApplication，整轮测试共用一个（Qt 只允许进程内一个实例）。

    ``QT_QPA_PLATFORM`` 必须在建 QApplication 之前定好，所以放在本文件顶部设置，
    无显示环境（CI、远程桌面）也能跑界面用例。
    """
    from hmi.ui.app import build_application

    return build_application()
