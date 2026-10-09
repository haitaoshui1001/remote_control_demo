"""项目统一入口：不装包也能跑——把 src 放进 sys.path，再转交给 hmi.cli。

为什么不用 `[project.scripts]`：现场部署的实际动作是"拷目录 + 建虚拟环境 + 敲命令"。
让 run.py 待在项目根，改代码不需要重装；写成 entry_points 反而每次都要 `pip install -e .`
才能在改动后生效，对交付场景是净负担。

常用命令（详细清单见《操作部署手册》）：
    python run.py                    # 等价于 selfcheck，无硬件跑全链路
    python run.py selfcheck --json   # 给 CI 收结论
    python run.py config --init      # 生成 config/app.yaml
    python run.py ports --probe      # 列出串口并猜哪个是 485 转 USB
    python run.py gui                # 启动图形界面
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
DEPS = ROOT / ".deps"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if DEPS.is_dir() and str(DEPS) not in sys.path:
    sys.path.append(str(DEPS))

# Windows 控制台常见 cp936，报告里一个 ✓ 就能让 print 抛 UnicodeEncodeError，
# 整段结论跟着丢。这里只把错误模式换成 replace：编码仍由系统决定，不至于在
# PowerShell 5.1 下把中文写成乱码。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

from hmi.cli import main  # noqa: E402  (必须在 sys.path 装配之后)

if __name__ == "__main__":
    sys.exit(main())
