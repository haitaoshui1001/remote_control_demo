"""遥测帧解析器（占位协议）单元测试。

盯三件事：编解码成对（mock 组和真机解的是同一套字节）、流式健壮性（半帧、
粘包、噪声、校验错都不能让解析器崩或卡死）、以及"匹配不到就当没看见"
（旁路解析不许误吞别人的数据）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hmi.protocol.telemetry_frames import (
    AsciiLineParser,
    BinaryFrameParser,
    TelemetrySample,
    WheeltecFrameParser,
    build_speed_frame,
    clamp_s16,
    create_parser,
    describe_wheeltec_frame,
    encode_ascii_line,
    encode_binary_frame,
    encode_wheeltec_frame,
)

ARGS = (2, 24.61, 3.21, 78.0, 0.45, 39.909236, 116.397428)


def _frame(*, status: int = 2, voltage: float = 24.61, current: float = 3.21,
           soc: float = 78.0, speed: float = 0.45, latitude: float = 39.909236,
           longitude: float = 116.397428) -> bytes:
    return encode_binary_frame(status, voltage, current, soc, speed, latitude, longitude)


# ------------------------------------------------------------------ 二进制
def test_binary_roundtrip_keeps_every_field():
    sample = BinaryFrameParser().feed(_frame())[0]
    assert isinstance(sample, TelemetrySample)
    assert sample.status == 2
    assert sample.voltage == pytest.approx(24.61)
    assert sample.current == pytest.approx(3.21)
    assert sample.soc == pytest.approx(78.0)
    assert sample.speed == pytest.approx(0.45)
    assert sample.latitude == pytest.approx(39.909236, abs=1e-6)
    assert sample.longitude == pytest.approx(116.397428, abs=1e-6)
    assert sample.gps_valid is True


def test_binary_frame_is_twenty_bytes_with_expected_header():
    raw = _frame()
    assert len(raw) == 20
    assert raw[:2] == b"\xAA\x55"
    assert raw[2] == 16


def test_binary_negative_current_survives_signed_layout():
    sample = BinaryFrameParser().feed(_frame(current=-3.21))[0]
    assert sample.current == pytest.approx(-3.21)


def test_binary_bad_checksum_drops_frame_without_stalling():
    raw = bytearray(_frame())
    raw[-1] ^= 0xFF
    parser = BinaryFrameParser()
    assert parser.feed(bytes(raw)) == []
    assert parser.feed(_frame())[0].voltage == pytest.approx(24.61), "错帧后应能继续解下一帧"


def test_binary_split_across_reads():
    raw = _frame()
    parser = BinaryFrameParser()
    assert parser.feed(raw[:6]) == []
    assert len(parser.feed(raw[6:])) == 1


def test_binary_two_frames_in_one_read():
    parser = BinaryFrameParser()
    out = parser.feed(_frame(status=1) + _frame(status=3))
    assert [s.status for s in out] == [1, 3]


def test_binary_leading_noise_is_counted_and_skipped():
    parser = BinaryFrameParser()
    out = parser.feed(b"\x01\x02\x03" + _frame())
    assert len(out) == 1
    assert parser.discarded == 3


def test_binary_custom_header_pairs_with_encoder():
    raw = encode_binary_frame(*ARGS, header_hex="5A A5")
    parser = BinaryFrameParser(header_hex="5A A5")
    assert parser.feed(raw[:2]) == []
    assert len(parser.feed(raw[2:])) == 1


def test_binary_garbage_only_never_yields_samples():
    parser = BinaryFrameParser()
    assert parser.feed(bytes(range(256))) == []


# ------------------------------------------------------------------ ASCII
def test_ascii_line_parses_and_strips_checksum_suffix():
    line = b"TLM,2,24.61,3.21,78,0.45,116.397428,39.909236*7F\r\n"
    sample = AsciiLineParser().feed(line)[0]
    assert sample.status == 2
    assert sample.voltage == pytest.approx(24.61)
    assert sample.soc == pytest.approx(78.0)
    assert sample.latitude == pytest.approx(116.397428, abs=1e-6)
    assert sample.longitude == pytest.approx(39.909236, abs=1e-6)


def test_ascii_partial_line_waits_for_newline():
    parser = AsciiLineParser()
    assert parser.feed(b"TLM,2,24.61,3.21,78,0.45,116.3974") == []
    assert len(parser.feed(b"8,39.909236\r\n")) == 1


def test_ascii_non_prefix_lines_are_discarded():
    parser = AsciiLineParser()
    out = parser.feed(b"AT+OK\r\n" + encode_ascii_line(*ARGS))
    assert len(out) == 1
    assert parser.discarded >= len(b"AT+OK")


def test_ascii_short_line_is_rejected():
    parser = AsciiLineParser()
    assert parser.feed(b"TLM,2,24.61\r\n") == []


def test_ascii_all_blank_numbers_is_rejected():
    parser = AsciiLineParser()
    assert parser.feed(b"TLM,2,-,-,-,-,116.3,39.9\r\n") == []


def test_ascii_encode_parse_roundtrip():
    sample = AsciiLineParser().feed(encode_ascii_line(*ARGS))[0]
    assert sample.voltage == pytest.approx(24.61, abs=0.01)
    assert sample.speed == pytest.approx(0.45, abs=0.01)
    assert sample.latitude == pytest.approx(ARGS[5], abs=1e-6)
    assert sample.longitude == pytest.approx(ARGS[6], abs=1e-6)
    assert sample.gps_valid is True


# ------------------------------------------------------------------ 工厂
@pytest.mark.parametrize(
    ("protocol", "name"),
    [
        ("wheeltec", "wheeltec"),
        ("binary", "binary"),
        ("ascii", "ascii"),
        ("ASCII", "ascii"),
        ("未知", "wheeltec"),
    ],
)
def test_create_parser_follows_protocol_config(protocol, name):
    parser = create_parser(SimpleNamespace(protocol=protocol, header_hex="AA 55"))
    assert parser.name == name


def test_create_parser_passes_header_hex_to_binary():
    parser = create_parser(SimpleNamespace(protocol="binary", header_hex="66 99"))
    assert parser.header == bytes([0x66, 0x99])


# ------------------------------------------------------------------ Wheeltec 底盘
_WHEELTEC_UP = bytes.fromhex("7B00009B0000FFDF0060000C40A8FFFD0006001E5B87827D")


def test_build_speed_frame_golden_downlink():
    assert build_speed_frame(0.1, 0.0, 0.0) == bytes.fromhex("7B00000064000000001F7D")
    assert build_speed_frame(0.1, 0.0, -0.033) == bytes.fromhex("7B000000640000FFDF3F7D")


def test_build_speed_frame_invert_and_clamp():
    assert build_speed_frame(0.0, 0.0, 0.05, invert_angular=True) == bytes.fromhex(
        "7B000000000000FFCE4A7D"
    )
    frame = build_speed_frame(500.0, 0.0, -500.0)
    assert frame[3:5] == b"\x7f\xff" and frame[7:9] == b"\x80\x00", "超界夹到 int16 两端"
    assert clamp_s16(40000) == 32767 and clamp_s16(-40000) == -32768


def test_wheeltec_parser_decodes_golden_upload():
    sample = WheeltecFrameParser().feed(_WHEELTEC_UP)[0]
    assert sample.flag_stop == 0
    assert sample.vx == pytest.approx(0.155)
    assert sample.vy == pytest.approx(0.0)
    assert sample.vz == pytest.approx(-0.033)
    assert sample.ax == pytest.approx(96 / 1672.0)
    assert sample.az == pytest.approx(16552 / 1672.0)
    assert sample.gx == pytest.approx(-3 / 3753.0)
    assert sample.gz == pytest.approx(30 / 3753.0)
    assert sample.voltage == pytest.approx(23.431)
    assert sample.speed == pytest.approx(0.155), "速度取平面合成 hypot(vx, vy)"
    assert sample.status is None and sample.latitude is None


def test_wheeltec_encode_matches_golden_upload():
    raw = encode_wheeltec_frame(
        flag_stop=0,
        voltage=23.431,
        vx=0.155,
        vy=0.0,
        wz=-0.033,
        ax=96 / 1672.0,
        ay=12 / 1672.0,
        az=16552 / 1672.0,
        gx=-3 / 3753.0,
        gy=6 / 3753.0,
        gz=30 / 3753.0,
    )
    assert raw == _WHEELTEC_UP, "mock 组帧必须与 PDF 样例逐字节一致"


def test_wheeltec_parser_drops_bad_bcc_and_resyncs():
    parser = WheeltecFrameParser()
    bad = bytearray(_WHEELTEC_UP)
    bad[22] ^= 0xFF
    out = parser.feed(b"\x11\x22" + bytes(bad) + _WHEELTEC_UP)
    assert len(out) == 1 and out[0].voltage == pytest.approx(23.431)
    assert parser.discarded >= 1


def test_wheeltec_parser_buffers_partial_frame():
    parser = WheeltecFrameParser()
    assert parser.feed(_WHEELTEC_UP[:10]) == []
    assert len(parser.feed(_WHEELTEC_UP[10:])) == 1


# ------------------------------------------------------------------ 备注翻译成人话
def test_describe_downlink_speed_frame_reads_like_human():
    assert (
        describe_wheeltec_frame(build_speed_frame(0.055, 0.0, 0.0))
        == "速度指令：前向速度 0.055 m/s，角速度 0.000 rad/s"
    )
    note = describe_wheeltec_frame(build_speed_frame(-0.2, 0.0, 0.5))
    assert "-0.200 m/s" in note and "0.500 rad/s" in note
    assert "横向速度 0.200 m/s" in describe_wheeltec_frame(build_speed_frame(0.1, 0.2, 0.0))


def test_describe_downlink_reflects_quantization():
    assert "0.055" in describe_wheeltec_frame(build_speed_frame(0.0554, 0.0, 0.0))
    assert "32.767" in describe_wheeltec_frame(build_speed_frame(400.0, 0.0, 0.0))


def test_describe_upload_frame_summarizes_feedback():
    assert (
        describe_wheeltec_frame(_WHEELTEC_UP)
        == "底盘反馈：使能，前向速度 0.155 m/s，角速度 -0.033 rad/s，电压 23.4 V"
    )
    assert "失能" in describe_wheeltec_frame(encode_wheeltec_frame(flag_stop=1))


def test_describe_rejects_non_wheeltec_or_corrupt_frames():
    bad = bytearray(_WHEELTEC_UP)
    bad[22] ^= 0xFF
    assert describe_wheeltec_frame(bytes(bad)) == ""
    assert describe_wheeltec_frame(b"") == ""
    assert describe_wheeltec_frame(b"\x7B" + b"\x00" * 10) == ""
    assert describe_wheeltec_frame(b"modbus-ish") == ""
