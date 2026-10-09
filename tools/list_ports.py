"""枚举本机串口，标注疑似 USB 转串口（485 转换板常见芯片）。

用法：
    python tools/list_ports.py            # 列表
    python tools/list_ports.py --pick     # 交互式选择并写回 config/app.yaml
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hmi.core.config import AppConfig  # noqa: E402
from hmi.transport.serial_transport import list_serial_ports  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="列出本机串口")
    ap.add_argument("--pick", action="store_true", help="选择一个端口并写入配置")
    args = ap.parse_args()

    ports = list_serial_ports()
    if not ports:
        print("未发现任何串口。请确认 485 转 USB 驱动已装（CH340/CP210x/FTDI）。")
        return 1

    print(f"{'设备':<12} {'疑似USB转串口':<14} 描述")
    for p in ports:
        flag = "是" if p["likely_usb_uart"] else "否"
        print(f"{p['device']:<12} {flag:<14} {p['description']}  [{p['hwid']}]")

    if args.pick:
        idx = input("输入序号选择串口: ").strip()
        try:
            chosen = ports[int(idx) - 1]["device"]
        except (ValueError, IndexError):
            print("无效序号，已取消")
            return 1
        cfg = AppConfig.load(ROOT / "config" / "app.yaml")
        cfg.serial.port = chosen
        cfg.save(ROOT / "config" / "app.yaml")
        print(f"已写入 config/app.yaml: serial.port = {chosen}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
