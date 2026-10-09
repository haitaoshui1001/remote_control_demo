"""日志初始化：控制台 + 滚动文件，可选把日志行转投事件总线。"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path

ROOT_NAME = "hmi"
_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-5s [%(name)s] %(message)s"
_DATEFMT = "%H:%M:%S"
_PLACEHOLDER_RE = re.compile(r"%\(([a-zA-Z_][a-zA-Z0-9_]*)\)")


def get_logger(name: str = "") -> logging.Logger:
    """统一取 ``hmi`` 命名空间下的 logger。"""
    if not name:
        return logging.getLogger(ROOT_NAME)
    if name == ROOT_NAME or name.startswith(ROOT_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_NAME}.{name}")


def setup_logging(
    level: str = "INFO",
    log_dir: str | None = "logs",
    *,
    bus=None,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
    console: bool = True,
) -> logging.Logger:
    """配置根 logger，重复调用只补齐缺失的 handler。"""
    root = logging.getLogger(ROOT_NAME)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.propagate = False

    if console and not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    ):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
        root.addHandler(sh)

    if log_dir:
        path = Path(log_dir)
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            path.mkdir(parents=True, exist_ok=True)
            file_ok = any(
                isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers
            )
            if not file_ok:
                fh = logging.handlers.RotatingFileHandler(
                    path / "hmi.log",
                    maxBytes=max_bytes,
                    backupCount=backup_count,
                    encoding="utf-8",
                    delay=True,
                )
                fh.setFormatter(
                    logging.Formatter(
                        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"
                    )
                )
                root.addHandler(fh)
        except OSError as exc:
            root.warning("日志文件不可用（%s），仅保留控制台输出", exc)

    if bus is not None and not any(isinstance(h, BusLogHandler) for h in root.handlers):
        bh = BusLogHandler(bus)
        bh.setLevel(logging.INFO)
        bh.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
        root.addHandler(bh)

    return root


class BusLogHandler(logging.Handler):
    """把日志记录转发为 ``system/log`` 事件，供 UI 日志页显示。"""

    def __init__(self, bus, topic: str = "system/log") -> None:
        super().__init__()
        self.bus = bus
        self.topic = topic

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
            extra = {
                k: v
                for k, v in vars(record).items()
                if k
                in ("name", "module", "direction", "kind", "endpoint", "channel", "hex", "ascii")
            }
            self.bus.publish(
                self.topic,
                {
                    "ts": record.created,
                    "level": record.levelname,
                    "logger": record.name,
                    "text": text,
                    "extra": extra,
                },
            )
        except Exception:
            self.handleError(record)


def tail(path: str, lines: int = 50) -> str:
    """读取日志文件末尾若干行，用于手册/自检提示。"""
    p = Path(path)
    if not p.exists():
        return ""
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        data = fh.readlines()
    return "".join(data[-max(1, int(lines)) :])
