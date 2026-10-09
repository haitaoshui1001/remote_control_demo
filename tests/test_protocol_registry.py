"""参数注册表测试：所有"能不能发/发出去长什么样/响应怎么回填"的判定都在这里固化。"""

from __future__ import annotations

import pytest

from hmi.core.models import RadioParams
from hmi.protocol import param_registry as reg


def test_spec_metadata_and_wire_name():
    mode = reg.spec_for("MODE")
    assert mode is not None and mode.readable and mode.writable
    assert mode.options == ("ap", "sta", "group", "apsta")
    assert reg.spec_for("RSSI").readable is True
    assert reg.spec_for("PAIR").readable is False
    assert reg.spec_for("LOADDEF").writable is True
    assert reg.spec_for("不存在") is None


def test_volatile_flags_cover_telemetry_only():
    """易变标记决定"后续轮询读哪些"，名单变了必须在这里先过一遍。"""
    assert reg.volatile_names() == [
        "BSSLIST",
        "RSSI",
        "EVM",
        "CONN_STATE",
        "WNBCFG",
        "STA_INFO",
    ]
    assert reg.is_volatile("RSSI") is True
    assert reg.is_volatile("rssi") is True  # resolve_key 归一大小写
    # 配置类与固件版本都是静态量，不该被反复重读
    for static in ("MODE", "TXPOWER", "SSID", "VERSION"):
        assert reg.is_volatile(static) is False
    assert reg.is_volatile("不存在") is False


def test_resolve_key_accepts_lowercase_field_and_title():
    assert reg.resolve_key("mode") == "MODE"
    assert reg.resolve_key("工作模式") == "MODE"
    assert reg.resolve_key("chan_list") == "CHAN_LIST"
    # 带空格的写法不接受：UI 必须用注册表键或字段名，不做模糊匹配以免误下发
    assert reg.resolve_key("chan list") is None
    assert reg.resolve_key("__nope__") is None
    assert reg.resolve_key("") is None


def test_validate_case_and_options():
    ok, text, typed = reg.validate("MODE", "AP")
    assert ok and text == "ap" and typed == "ap"
    ok, text, _ = reg.validate("KEYMGMT", "wpa-psk")
    assert ok and text == "WPA-PSK"
    ok, err, _ = reg.validate("MODE", "mesh")
    assert not ok and "ap" in err


def test_validate_numeric_bounds():
    assert reg.validate("BSS_BW", 8)[0] is True
    ok, err, _ = reg.validate("BSS_BW", 3)
    assert not ok and "1/2/4/8" in err.replace(" ", "")
    assert reg.validate("TXPOWER", 20)[0] is True
    assert reg.validate("TXPOWER", 999)[0] is False
    ok, _err, typed = reg.validate("CHAN_LIST", "8680, 8700")
    assert ok and typed == [8680, 8700]
    assert reg.validate("CHAN_LIST", ",".join(str(8600 + i) for i in range(20)))[0] is False


def test_freq_values_are_mhz_times_ten():
    spec = reg.spec_for("FREQ_RANGE")
    assert spec.scale == 10.0 and spec.unit == "(MHz×10)"
    ok, text, _ = reg.encode("CHAN_LIST", [8680, 8700])
    assert ok and text == "8680,8700"


def test_tx_mcs_accepts_auto_value():
    """手册 6.3.17：0~7 或 10=固定速率，其他值（出厂默认 255）=自动调整，255 必须合法。"""
    assert reg.spec_for("TX_MCS").vmax == 255
    assert reg.validate("TX_MCS", 255)[0] is True
    assert reg.validate("TX_MCS", 256)[0] is False


def test_hex_and_text_limits():
    assert reg.validate("PSK", "A" * 64)[0] is True
    assert reg.validate("PSK", "A" * 63)[0] is False
    assert reg.validate("PSK", "")[0] is True
    assert reg.validate("SSID", "x" * 33)[0] is False


def test_range_override_relaxes_bounds():
    reg.set_range_override("TXPOWER", 0, 30)
    assert reg.validate("TXPOWER", 27)[0] is True
    assert reg.validate("TXPOWER", 31)[0] is False
    assert reg.range_overrides()["TXPOWER"] == (0, 30)
    reg.clear_range_overrides()
    assert reg.spec_for("TXPOWER").vmax == 20
    assert reg.validate("TXPOWER", 27)[0] is False
    assert reg.range_overrides() == {}


def test_encode_prefixes_and_rejects_unknown():
    ok, text, err = reg.encode("SYSDBG_LMAC", "1")
    assert ok and text == "LMAC,1" and err == ""
    assert reg.spec_for("SYSDBG_LMAC").wire_name == "SYSDBG"
    ok, _text, err = reg.encode("NOSUCH", 1)
    assert not ok and "未知参数" in err
    ok, _text, err = reg.encode("TXPOWER", 999)
    assert not ok and "大于" in err


def test_decode_shapes():
    assert reg.decode("RSSI", "-72.4 dBm") == pytest.approx(-72.4)
    assert reg.decode("CHAN_LIST", "8680,8700") == [8680, 8700]
    assert reg.decode("CONN_STATE", "connected") is True
    assert reg.decode("MODE", "ap") == "ap"
    assert reg.decode("TXPOWER", "20dbm") == 20
    assert reg.decode("NOSUCH", "whatever") == "whatever"


def test_decode_is_idempotent_on_parsed_values():
    """遥测轮询会把 ``query_value`` 的解码结果再喂一次 ``apply_to_params``。

    这一条钉住"二次解码不能丢数据"：曾经 ``decode`` 先把 list ``str()`` 成
    ``"[8680, 8700, 8720]"`` 再按逗号切，首尾带方括号的 token 解析失败被丢弃，
    信道页只剩一个频点；``0`` 也会被 ``text or ""`` 吞掉。
    """
    assert reg.decode("CHAN_LIST", [8680, 8700, 8720]) == [8680, 8700, 8720]
    assert reg.decode("FREQ_RANGE", (8680, 9280)) == [8680, 9280]
    assert reg.decode("TXPOWER", 0) == 0
    assert reg.decode("RSSI", -72.4) == pytest.approx(-72.4)
    assert reg.decode("CONN_STATE", True) is True
    assert reg.decode("MODE", 1) == 1
    assert reg.decode("SSID", None) == ""
    params = RadioParams()
    assert reg.apply_to_params(params, "CHAN_LIST", [8680, 8700, 8720]) is True
    assert params.chan_list == [8680, 8700, 8720]


def test_apply_to_params_and_skip_fieldless():
    params = RadioParams()
    assert reg.apply_to_params(params, "SSID", "hgic_ah_test") is True
    assert params.ssid == "hgic_ah_test"
    assert reg.apply_to_params(params, "CHAN_LIST", "8680,8700") is True
    assert params.chan_list == [8680, 8700]
    assert reg.apply_to_params(params, "STA_INFO", "1,00:11:22:33:44:55,-78") is False
    assert reg.apply_to_params(params, "NOSUCH", "x") is False


def test_plan_orders_by_spec_order_and_skips_none():
    params = RadioParams(mode="sta", ssid="node_a", txpower=20)
    steps = reg.plan(params)
    names = [n for n, _ in steps]
    assert names == ["MODE", "SSID", "TXPOWER"]
    assert dict(steps)["SSID"] == "node_a"
    assert reg.plan(RadioParams()) == []


def test_plan_raises_on_invalid_value():
    params = RadioParams(bss_bw=3)
    with pytest.raises(reg.ParamError):
        reg.plan(params)


def test_groups_partition_read_and_write():
    readable = {s.name for s in reg.readable_specs()}
    writable = {s.name for s in reg.writable_specs()}
    assert "RSSI" in readable and "RSSI" not in writable
    assert "MODE" in readable and "MODE" in writable
    assert "PAIR" in writable and "PAIR" not in readable
    assert set(reg.by_group()) == set(reg.GROUPS)
    rows = reg.summary_rows()
    assert len(rows) == len(reg.all_specs())
    assert {"at", "title", "group", "rw", "bound"} <= set(rows[0])
    assert all(row["at"] for row in rows)
    assert {"MODE", "SSID", "TXPOWER"} <= {row["at"] for row in rows}
