"""网口 AT 通道的定向语义自检（不建真套接字，用假 sock 记录发出的帧）。

覆盖实机故障：两台模块在线时把 AT 帧静默定向到一台已复位的 MAC，会让另一台
的 op4 应答被当成串扰丢弃，界面表现为"能发现两台但一台也读不到"。
"""

from __future__ import annotations

import time

import pytest

from hmi.core.config import AtCfg, UdpAtCfg
from hmi.transport.udp_at import (
    BROADCAST_MAC,
    OP_AT,
    UdpAtTransport,
    format_mac,
    parse_mac,
    parse_frame,
)


class FakeSock:
    """只记录 sendto 的套接字替身。"""

    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple]] = []

    def sendto(self, frame: bytes, addr: tuple) -> int:
        self.sent.append((bytes(frame), addr))
        return len(frame)

    def close(self) -> None:  # pragma: no cover - 关闭路径不在本文件覆盖范围
        pass


@pytest.fixture()
def udp():
    t = UdpAtTransport(AtCfg(), UdpAtCfg(probe_timeout_ms=50))
    t.sock = FakeSock()
    return t


def _at_frames(sock: FakeSock) -> list[bytes]:
    return [f for f, _addr in sock.sent if parse_frame(f) and parse_frame(f)[0] == OP_AT]


def test_online_target_goes_out_directed(udp):
    mac = parse_mac("96-a1-3e-7d-83-88")
    udp._modules[mac] = (time.monotonic(), "192.168.1.10")

    written = udp.write(b"AT+TXPOWER=8\r\n", target_mac=format_mac(mac))

    assert written == len(b"AT+TXPOWER=8")
    frames = _at_frames(udp.sock)
    assert len(frames) == 1
    op, dst, _src, text = parse_frame(frames[0])
    assert dst == mac and text == b"AT+TXPOWER=8"
    # 期望应答源同步更新，收线程才会收这台的意见
    assert udp._at_target == mac


def test_offline_target_fails_loudly_and_does_not_poison_at_target(udp):
    live = parse_mac("96-a1-3e-97-66-80")
    udp._modules[live] = (time.monotonic(), "192.168.1.11")
    udp._at_target = live
    dead = parse_mac("aa-bb-cc-dd-ee-ff")

    with pytest.raises(OSError) as exc:
        udp.write(b"AT+TXPOWER=8", target_mac=format_mac(dead))

    assert "离线" in str(exc.value)
    assert format_mac(dead) in str(exc.value)
    # AT 帧一帧都没上线（只可能补发过发现包），期望应答源仍在这台活着的模块上
    assert _at_frames(udp.sock) == []
    assert udp._at_target == live


def test_broadcast_target_skips_online_check(udp):
    udp._at_target = parse_mac("96-a1-3e-7d-83-88")

    udp.write(b"AT+CONN_STATE?", target_mac=BROADCAST_MAC)

    op, dst, _src, _text = parse_frame(_at_frames(udp.sock)[0])
    assert dst == BROADCAST_MAC
    # 广播不指定应答源，否则会把所有模块的应答都当串扰丢掉
    assert udp._at_target == b""


def test_no_target_and_no_module_falls_back_to_broadcast(udp, monkeypatch):
    """没指定目标、也发现不到任何模块：仍按老行为退回全 F 并留下告警（不改语义）。"""
    monkeypatch.setattr(udp, "_ttl", lambda: 0.0)
    warns: list[str] = []
    monkeypatch.setattr(udp.log, "warning", lambda msg, *a: warns.append(msg % a if a else msg))

    udp.write(b"AT+TXPOWER?")

    assert parse_frame(_at_frames(udp.sock)[0])[1] == BROADCAST_MAC
    assert udp._at_target == b""
    assert any("全 F" in w for w in warns)


def test_write_without_channel_is_rejected():
    t = UdpAtTransport(AtCfg(), UdpAtCfg())
    with pytest.raises(OSError, match="通道未打开"):
        t.write(b"AT+TXPOWER?", target_mac="96-a1-3e-7d-83-88")


def test_bad_target_string_falls_back_to_auto(udp):
    mac = parse_mac("96-a1-3e-7d-83-88")
    udp._modules[mac] = (time.monotonic(), "192.168.1.10")

    udp.write(b"AT+TXPOWER?", target_mac="not-a-mac")

    assert parse_frame(_at_frames(udp.sock)[0])[1] == mac
