"""指令模板库测试：模板层只该管"描述"，字节的事交给透传引擎。

守住四条纪律：
1. **容错优先**——配置文件被手改坏、缺字段、多字段，程序要"少几条"而不是"起不来"；
2. **落盘只写非默认值**——save/load 往返必须等价，否则用户改的开关会悄悄丢；
3. **校验前置于上线**——参数没填全时一个字节都不能发到线上；
4. **下发只返结论**——引擎缺失、校验失败、串口写炸了，都返回失败结果而不是抛异常。
"""

from __future__ import annotations

import time

import pytest

from hmi.core.config import PassthroughCfg
from hmi.core.events import EventBus
from hmi.core.models import ByteRecord, CommandResult, CommandTemplate
from hmi.core.utils import verify_crc16
from hmi.mock.radio import MockByteTransport, MockRadioModule
from hmi.protocol.channel import ChannelDemux
from hmi.protocol.passthrough import PassthroughEngine, build_frame
from hmi.protocol.templates import (
    TemplateLibrary,
    default_templates,
    missing_placeholders,
    render_payload,
)

_REPLY = bytes([0x01, 0x03, 0x04, 0x01, 0x00, 0x01, 0x02])


class FakeEngine:
    """透传引擎替身：只记账传过来的关键字参数，不碰任何字节链路。"""

    def __init__(self, *, error: Exception | None = None, reply: CommandResult | None = None):
        self.error = error
        self.reply = reply or CommandResult(ok=True, matched=True, reply=_REPLY)
        self.sent_calls: list[dict] = []
        self.request_calls: list[dict] = []

    def send(self, payload, **kw) -> ByteRecord:
        self.sent_calls.append({"payload": payload, **kw})
        if self.error is not None:
            raise self.error
        frame = build_frame(
            payload,
            mode=kw.get("mode") or "hex",
            suffix=kw.get("suffix") or "none",
            crc16_append=bool(kw.get("crc16_append")),
        )
        return ByteRecord(ts=time.time(), data=frame, kind=kw.get("kind", "raw"))

    def request(self, payload, **kw) -> CommandResult:
        self.request_calls.append({"payload": payload, **kw})
        if self.error is not None:
            raise self.error
        return self.reply


def _lib(tmp_path, **kw) -> TemplateLibrary:
    return TemplateLibrary(path=tmp_path / "config" / "templates.yaml", **kw)


# ------------------------------------------------------------------- 纯函数
def test_missing_placeholders_dedupes_keeping_order():
    assert missing_placeholders("01 06 {reg} {ch} {reg}") == ["reg", "ch"]
    assert missing_placeholders("") == []
    assert missing_placeholders("01 03 00 00") == []


def test_render_replaces_known_and_keeps_unknown():
    out = render_payload("01 06 {reg} 00 {ch}", {"ch": "01"})
    assert out == "01 06 {reg} 00 01"
    assert render_payload("01 06", {"reg": "00 10"}) == "01 06"
    assert render_payload("", None) == ""


def test_render_does_not_break_on_stray_braces():
    # 现场指令里出现孤立花括号很常见，用 str.format 会把指令本身炸掉
    assert render_payload("01 06 } { 00 10", {}) == "01 06 } { 00 10"
    assert render_payload("A {1} B", {}) == "A {1} B"
    assert render_payload("A {a{b} B", {"a": "X"}) == "A {a{b} B"


def test_default_templates_cover_both_modes_and_validate_clean():
    items = default_templates()
    assert [t.id for t in items] == ["modbus_read_holding", "generic_on", "ascii_status"]
    assert len({t.id for t in items}) == len(items)
    assert {t.mode for t in items} == {"hex", "ascii"}
    lib = TemplateLibrary(engine=None, path="unused.yaml", autoload=False)
    assert lib.validate(items[0]) == []
    assert lib.validate(items[2]) == []
    # 带占位符的示例在没有参数时必须被明确指出，而不是发半截指令上线
    assert any("缺少参数" in p for p in lib.validate(items[1]))


# ------------------------------------------------------------------- 读盘
def test_first_load_writes_factory_file(tmp_path):
    lib = _lib(tmp_path)
    target = tmp_path / "config" / "templates.yaml"
    assert target.is_file()
    assert len(lib) == 3
    assert lib.loaded_from == str(target)
    assert lib.load_errors == []
    text = target.read_text(encoding="utf-8")
    assert "modbus_read_holding" in text
    assert lib.ordered()[0].id == "modbus_read_holding"


def test_save_load_roundtrip_keeps_every_field(tmp_path):
    lib = _lib(tmp_path)
    tpl = CommandTemplate(
        id="full",
        name="全字段",
        group="组A",
        payload="01 06 00 10 00 01",
        mode="hex",
        suffix="crlf",
        expect_reply=False,
        reply_timeout_ms=1234,
        match_prefix="01 06",
        min_reply_bytes=5,
        crc16_append=True,
        interval_ms=250,
        enabled=False,
        order=7,
        note="备注：含中文与冒号",
    )
    lib.upsert(tpl)
    lib.save()

    again = TemplateLibrary(path=lib.path)
    got = again.get("full")
    assert got is not None
    assert got == tpl


def test_broken_yaml_degrades_to_defaults_without_crashing(tmp_path):
    target = tmp_path / "config" / "templates.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("templates: [1, 2\n", encoding="utf-8")

    lib = TemplateLibrary(path=target)
    assert lib.load_errors
    assert len(lib) == 3
    assert [t.id for t in lib.ordered()] == [t.id for t in default_templates()]
    # 坏文件不能被悄悄覆盖成默认值，否则用户的原始配置就没了
    assert target.read_text(encoding="utf-8") == "templates: [1, 2\n"


def test_dict_wrapper_and_unknown_fields_tolerated(tmp_path):
    target = tmp_path / "config" / "templates.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        "templates:\n"
        "  - id: a\n"
        "    payload: \"01 03\"\n"
        "    owner: 现场张三\n"
        "  - id: b\n"
        "    name: 乙\n",
        encoding="utf-8",
    )
    lib = TemplateLibrary(path=target)
    assert len(lib) == 2
    a = lib.get("a")
    assert a is not None and a.name == "a"
    assert a.mode == "hex" and a.expect_reply is True and a.order == 0
    assert not hasattr(a, "owner")


def test_non_dict_row_skipped_and_missing_id_backfilled(tmp_path):
    target = tmp_path / "config" / "templates.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        "- id: c\n"
        "  name: 丙\n"
        "- 这是一行裸字符串\n"
        "- payload: \"01 06\"\n",
        encoding="utf-8",
    )
    lib = TemplateLibrary(path=target)
    ids = [t.id for t in lib.ordered()]
    assert "c" in ids
    # 裸字符串行被跳过，但序号不回填，保证 id 不与人工条目冲突
    assert "tpl_3" in ids and "tpl_2" not in ids
    assert lib.get("tpl_3").name == "tpl_3"


def test_reload_replaces_previous_items(tmp_path):
    lib = _lib(tmp_path)
    lib.remove("generic_on")
    assert lib.get("generic_on") is None
    lib.load()
    assert lib.get("generic_on") is not None
    assert len(lib) == 3


# ------------------------------------------------------------------- CRUD
def test_upsert_assigns_generated_ids_and_avoids_collisions(tmp_path):
    lib = _lib(tmp_path)
    before = len(lib)

    first = lib.upsert(CommandTemplate(name="Motor On", payload="01 03"))
    assert first.id == "motor_on"
    second = lib.upsert(CommandTemplate(name="Motor On 2", payload="01 03"))
    assert second.id == "motor_on_2"
    # 纯中文名无法 slug 化，退回 cmd 并继续去重
    third = lib.upsert(CommandTemplate(name="电机开", payload="01 03"))
    assert third.id in {"cmd", "cmd_2"}
    assert len(lib) == before + 3
    assert "motor_on" in lib


def test_move_reorders_and_renumbers(tmp_path):
    lib = _lib(tmp_path, autoload=False)
    for n, label in enumerate(("甲", "乙", "丙"), start=1):
        lib.upsert(CommandTemplate(id=f"m{n}", name=label, payload="01 03", order=n * 10))
    assert [t.id for t in lib.ordered()] == ["m1", "m2", "m3"]

    assert lib.move("m3", -1) is True
    assert [t.id for t in lib.ordered()] == ["m1", "m3", "m2"]
    assert [t.order for t in lib.ordered()] == [10, 20, 30]

    assert lib.move("m1", -1) is False
    assert lib.move("nope", 1) is False
    assert lib.move("m1", 99) is True
    assert [t.id for t in lib.ordered()] == ["m3", "m2", "m1"]


def test_groups_membership_and_summary(tmp_path):
    lib = _lib(tmp_path)
    lib.upsert(CommandTemplate(id="x", name="X", group="", payload="01", order=40))
    lib.upsert(
        CommandTemplate(id="y", name="Y", group="电机", payload="01", order=50, enabled=False)
    )

    assert set(lib.groups()) == {"Modbus 示例", "通用", "默认", "电机"}
    assert [t.id for t in lib.by_group("电机")] == ["y"]
    assert lib.remove("x") is True and lib.remove("x") is False

    s = lib.summary()
    assert s["total"] == len(lib) == 4
    assert s["enabled"] == 3
    assert s["path"] == str(lib.path)
    assert s["errors"] == []


# ------------------------------------------------------------------- 校验
def test_validate_missing_params_short_circuits(tmp_path):
    lib = _lib(tmp_path)
    tpl = CommandTemplate(id="p", name="P", payload="01 06 {reg} 00 {ch}", mode="hex")
    assert lib.validate(tpl) == ["缺少参数：{reg}, {ch}"]
    assert lib.validate(tpl, {"reg": "00 10"}) == ["缺少参数：{ch}"]
    assert lib.validate(tpl, {"reg": "00 10", "ch": "01"}) == []


def test_validate_empty_payload_and_bad_encoding(tmp_path):
    lib = _lib(tmp_path)
    assert lib.validate(CommandTemplate(id="e", payload="   ")) == ["指令内容为空"]

    bad = CommandTemplate(id="b", payload="01 ZZ 03", mode="hex")
    problems = lib.validate(bad)
    assert len(problems) == 1
    assert any("ZZ" in p for p in problems)

    # ascii 模式下的"hex 串"是合法文本，不该被判错
    textish = CommandTemplate(id="t", payload="01 ZZ", mode="ascii")
    assert lib.validate(textish) == []

    empty_after = CommandTemplate(id="z", payload="  ", mode="ascii")
    assert lib.validate(empty_after) == ["指令内容为空"]


def test_validate_checks_prefix_timeout_and_min_bytes(tmp_path):
    lib = _lib(tmp_path)
    tpl = CommandTemplate(
        id="v",
        payload="01 03 00 00",
        match_prefix="01 GG",
        reply_timeout_ms=0,
        min_reply_bytes=-1,
    )
    problems = lib.validate(tpl)
    assert "应答前缀不是合法 Hex" in problems
    assert "应答超时须大于 0 ms" in problems
    assert "最小应答长度不能为负" in problems

    ascii_prefix = CommandTemplate(id="v2", payload="STATUS", mode="ascii", match_prefix="OK")
    assert lib.validate(ascii_prefix) == []


def test_preview_applies_crc_before_suffix_and_renders(tmp_path):
    lib = _lib(tmp_path)
    tpl = CommandTemplate(
        id="f",
        payload="01 03 00 {reg}",
        crc16_append=True,
        suffix="crlf",
    )
    frame = lib.preview(tpl, {"reg": "00 01"})
    assert len(frame) == 5 + 2 + 2
    assert verify_crc16(frame[:-2])
    assert frame.endswith(b"\r\n")
    assert lib.preview(CommandTemplate(id="s", payload="RUN", mode="ascii")) == b"RUN"


# ------------------------------------------------------------------- 下发
def test_run_without_engine_or_unknown_template(tmp_path):
    lib = _lib(tmp_path)
    res = lib.run("modbus_read_holding")
    assert res.ok is False and "未装配" in res.error

    missing = lib.run("not_there")
    assert missing.ok is False and missing.error == "模板不存在"
    assert missing.template_id == "not_there"


def test_run_validation_failure_never_reaches_engine(tmp_path):
    eng = FakeEngine()
    lib = _lib(tmp_path, engine=eng)
    tpl = CommandTemplate(id="g", name="G", payload="01 06 {reg} 00 01")
    res = lib.run(tpl)
    assert res.ok is False and "缺少参数" in res.error
    assert res.template_id == "g" and res.name == "G"
    assert eng.sent_calls == [] and eng.request_calls == []


def test_run_fire_and_forget_uses_send(tmp_path):
    eng = FakeEngine()
    lib = _lib(tmp_path, engine=eng)
    tpl = CommandTemplate(
        id="n",
        name="不等的指令",
        payload="01 06 00 10 00 01",
        expect_reply=False,
        crc16_append=True,
        suffix="crlf",
    )
    res = lib.run(tpl)
    assert res.ok is True and res.matched is False
    assert res.sent_bytes == lib.preview(tpl)

    call = eng.sent_calls[0]
    assert call["mode"] == "hex"
    assert call["suffix"] == "crlf"
    assert call["crc16_append"] is True
    assert call["note"] == "不等的指令"
    assert call["kind"] == "template"


def test_run_expect_reply_forwards_matching_params(tmp_path):
    eng = FakeEngine()
    lib = _lib(tmp_path, engine=eng)
    tpl = CommandTemplate(
        id="q",
        name="查询",
        payload="01 03 {reg}",
        match_prefix="01 03",
        reply_timeout_ms=120,
        min_reply_bytes=7,
    )
    res = lib.run(tpl, values={"reg": "00 00 00 02"})
    assert res.ok is True

    call = eng.request_calls[0]
    assert call["payload"] == "01 03 00 00 00 02"
    assert call["timeout_ms"] == 120
    assert call["match_prefix"] == "01 03"
    assert call["min_reply_bytes"] == 7
    assert call["template_id"] == "q" and call["name"] == "查询"


@pytest.mark.parametrize(
    "exc",
    [ValueError("坏指令"), RuntimeError("半双工忙"), OSError("COM7 被占用")],
)
def test_run_converts_engine_errors_to_failure(tmp_path, exc):
    eng = FakeEngine(error=exc)
    lib = _lib(tmp_path, engine=eng)
    silent = CommandTemplate(id="a", name="A", payload="01 03", expect_reply=False)
    res = lib.run(silent)
    assert res.ok is False and res.error == str(exc)

    waiting = CommandTemplate(id="b", name="B", payload="01 03")
    res2 = lib.run(waiting)
    assert res2.ok is False and res2.error == str(exc)


def test_run_engine_override_wins(tmp_path):
    default_eng = FakeEngine()
    lib = _lib(tmp_path, engine=default_eng)
    other = FakeEngine()
    lib.run("modbus_read_holding", engine=other)
    assert other.request_calls and default_eng.request_calls == []


def test_describe_result_is_one_line_for_ui(tmp_path):
    lib = _lib(tmp_path)
    ts = time.time()
    ok = CommandResult(ts=ts, template_id="a", name="开", reply=b"\x01\x03ok", ok=True, rtt_ms=12.4)
    line = lib.describe_result(ok)
    assert line.startswith(time.strftime("%H:%M:%S", time.localtime(ts)))
    assert "开: OK" in line and "ok" in line and "12 ms" in line

    empty = CommandResult(ts=time.time(), template_id="a", name="开", ok=True)
    assert "(无应答)" in lib.describe_result(empty)

    fail = CommandResult(ts=time.time(), template_id="a", ok=False, error="应答超时（800 ms）")
    assert "a: 失败 - 应答超时" in lib.describe_result(fail)


def test_describe_result_appends_tx_only_when_frame_exists(tmp_path):
    """拒发/写失败也带原始帧——行尾 "TX …" 让人不开抓包就知道发了什么。"""
    lib = _lib(tmp_path)
    refused = CommandResult(
        ts=time.time(),
        template_id="a",
        name="速度",
        ok=False,
        error="AT 通道忙（半双工线），指令未发送",
        sent_bytes=bytes([0x01, 0x03]),
    )
    line = lib.describe_result(refused)
    assert "忙" in line and "TX 01 03" in line
    blind = CommandResult(ts=time.time(), template_id="a", ok=False, error="模板不存在")
    assert "TX" not in lib.describe_result(blind)


# ------------------------------------------------------------------- 真链路
def test_library_drives_real_engine_over_mock_link(tmp_path):
    """默认模板经真引擎走仿真链路：CRC 上线、应答配对、结论可读。"""
    mod = MockRadioModule(name="local", seed=3)
    transport = MockByteTransport(mod, latency_ms=2, telemetry_interval_s=3600.0)
    transport.open()
    demux = ChannelDemux("tpl-test", idle_gap_ms=15.0, read_timeout_ms=5)
    demux.attach(transport)
    eng = PassthroughEngine(
        demux,
        bus=EventBus(),
        cfg=PassthroughCfg(reply_timeout_ms=2000),
        endpoint="local",
        name="485",
    )
    eng.start()
    lib = _lib(tmp_path, engine=eng)
    try:
        res = lib.run("modbus_read_holding")
        assert res.ok is True, res.error
        assert res.reply[:2] == bytes([0x01, 0x03])
        assert len(res.sent_bytes) == 8
        assert verify_crc16(res.sent_bytes)
        assert mod.rx_log[-1][1] == res.sent_bytes
        assert "OK" in lib.describe_result(res)
    finally:
        eng.stop()
        demux.detach()
        transport.close()
