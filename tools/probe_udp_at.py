"""广播探测 EWD611 网口 AT 模块（用户抓包实锤：op1 发现握手，UDP 56789 双向广播）。

用法：
    python tools/probe_udp_at.py
    python tools/probe_udp_at.py --timeout-ms 1500
探测到多个模块时，把目标模块的 MAC 写入 config/app.yaml 的 udp_at.target_mac
即可免发现、直接定向该模块（模块没有 IP、寻址靠协议头里的 MAC）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hmi.core.config import AppConfig  # noqa: E402
from hmi.transport.udp_at import UdpAtTransport  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="广播探测模块网口 AT（UDP 56789）")
    ap.add_argument("--timeout-ms", type=int, default=900, help="等待响应的毫秒数")
    args = ap.parse_args()

    cfg = AppConfig.load(ROOT / "config" / "app.yaml")
    t = UdpAtTransport(cfg.at, cfg.udp_at, name="probe")
    print(f"本机标识 {t.info()['host_id']}，向 255.255.255.255:56789 广播 op1 发现请求…")
    n = t.discover(timeout_ms=args.timeout_ms)
    if n:
        macs = t.discovered_macs()
        print(f"发现 {n} 个模块：{'、'.join(macs)}")
        print(f"最后响应源：{t.last_source}")
        if n > 1:
            print(
                "多模块场景：把目标模块的 MAC 写入 config/app.yaml 的 "
                "udp_at.target_mac 可直接定向该模块。"
            )
        return 0
    print(
        "未获 op2 响应。按序确认：电台处于唤醒模式（手册第六章首行注意）→ "
        "网线接通 → 本机网卡设为 192.168.1.x 静态 IP → 防火墙放行 UDP 56789。"
        "裸 ASCII 会被模块丢弃，必须成帧+握手，本工具已按 netat.exe 协议发送。"
        "串口是纯透传通道，向串口发 AT 只会透传到远端，上位机不回退串口。"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
