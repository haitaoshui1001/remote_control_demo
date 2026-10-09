"""指令控制台页测试（offscreen）。

确定性三件套沿用视频页的做法：桥事件先 ``bus.publish`` 再手动 ``bridge._tick()``；
stub 服务方法直接 monkeypatch；后台线程用 ``page._threaded = False`` 压成同步调用
——只有专门验证"线程真的在后台跑"的那一条用例例外，它拿事件对象同步两个线程。

重点盯四件事：等应答指令绝不占主线程、失败收口成红字而不是异常、CRUD 不碰
仓库里的 config/templates.yaml（一律 persist=False 或改道 tmp_path）、通道断开
时发送按钮必须置灰。
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import QCoreApplication

from hmi.core.events import Events
from hmi.core.models import CommandResult, CommandTemplate
from hmi.services.session import build_session
from hmi.ui.bridge import UiBridge
from hmi.ui.pages.console_page import _TID_ROLE, ConsolePage, _ParamsDialog, _TemplateDialog


@pytest.fixture()
def session():
    return build_session(None, mock=True)


@pytest.fixture()
def svc(session):
    return session.passthrough


@pytest.fixture()
def page(qapp, session):
    bridge = UiBridge(session.bus, interval_ms=200)
    bridge.start()
    p = ConsolePage(session, bridge)
    p._threaded = False
    yield p
    p.on_close()
    bridge.stop()


def _leaf_count(page: ConsolePage) -> int:
    return sum(
        page.tree.topLevelItem(i).childCount() for i in range(page.tree.topLevelItemCount())
    )


def _tick(bridge: UiBridge) -> None:
    bridge._tick()


# ------------------------------------------------------------------ preview_manual
def test_preview_manual_builds_frame(svc):
    out = svc.preview_manual("41 42", mode="hex", suffix="crlf")
    assert out["ok"] is True
    assert out["hex"] == "41 42 0D 0A"


def test_preview_manual_crc_appends_two_bytes(svc):
    out = svc.preview_manual("01 03 00 00 00 01", mode="hex", suffix="none", crc16_append=True)
    assert out["ok"] is True
    assert out["detail"] == "8 字节"


def test_preview_manual_rejects_bad_hex(svc):
    out = svc.preview_manual("ZZ", mode="hex", suffix="none")
    assert out["ok"] is False
    assert "非法" in out["detail"]


def test_preview_manual_empty_payload(svc):
    out = svc.preview_manual("", mode="ascii", suffix="none")
    assert out["ok"] is False
    assert "空" in out["detail"]
    # ASCII 模式下空格是合法字节，不算空
    assert svc.preview_manual("   ", mode="ascii", suffix="none")["ok"] is True


def test_preview_manual_uses_cfg_defaults(svc):
    svc.configure(mode="ascii", suffix="crlf")
    out = svc.preview_manual("PING")
    assert out["ok"] is True
    assert out["hex"].endswith("0D 0A")


# ------------------------------------------------------------------ 页面骨架
def test_page_builds_with_registry_templates(page, svc):
    assert page.page_key == "console"
    assert _leaf_count(page) == len(svc.templates.ordered())
    assert "共" in page.lbl_templates.text()


def test_preview_label_tracks_input(page):
    page.edit_payload.setText("41 42")
    assert "41 42" in page.lbl_preview.text()
    page.edit_payload.setText("ZZ")
    assert "非法" in page.lbl_preview.text()


def test_combo_changes_sync_configure(page, svc, monkeypatch):
    seen: dict = {}

    def fake_configure(**changes):
        seen.update(changes)

    monkeypatch.setattr(svc, "configure", fake_configure)
    idx = page.combo_mode.findData("ascii")
    page.combo_mode.setCurrentIndex(idx)
    assert seen.get("mode") == "ascii"


# ------------------------------------------------------------------ 手动发送
def test_send_only_success_appends_log_row(page, svc, monkeypatch):
    calls: list[dict] = []

    def fake_send(payload, **kwargs):
        calls.append({"payload": payload, **kwargs})
        return {"ok": True, "detail": "已发送 2 字节", "hex": "AA BB"}

    monkeypatch.setattr(svc, "send", fake_send)
    page.edit_payload.setText("AA BB")
    page.btn_send.click()

    assert len(calls) == 1
    assert calls[0]["note"] == "手动发送"
    assert page.log_view.count() == 1
    assert "已发送" in page.log_view.item(0).text()


def test_send_only_failure_leaves_log_to_bridge(page, svc, monkeypatch):
    monkeypatch.setattr(
        svc, "send", lambda payload, **kw: {"ok": False, "detail": "AT 通道忙", "hex": ""}
    )
    page.edit_payload.setText("AA")
    page.btn_send.click()
    assert page.log_view.count() == 0


def test_send_only_empty_payload_does_nothing(page, svc, monkeypatch):
    called = []
    monkeypatch.setattr(svc, "send", lambda *a, **k: called.append(1))
    page.btn_send.click()
    assert not called


def test_request_sync_flow_restores_buttons(page, svc, monkeypatch):
    seen: dict = {}

    def fake_request(payload, **kwargs):
        seen.update(kwargs)
        return CommandResult(ts=time.time(), name="手动等待", ok=True, reply=b"OK", rtt_ms=12)

    monkeypatch.setattr(svc, "request", fake_request)
    page.edit_payload.setText("22")
    page.btn_request.click()

    assert seen["timeout_ms"] == int(svc.cfg.passthrough.reply_timeout_ms)
    assert page._busy is False
    assert page.btn_request.isEnabled()


def test_request_exception_is_guarded(page, svc, monkeypatch):
    def boom(payload, **kwargs):
        raise RuntimeError("串口掉了")

    monkeypatch.setattr(svc, "request", boom)
    page.edit_payload.setText("22")
    page.btn_request.click()  # 同步模式：异常必须被 _guard 收口
    assert page._busy is False


def test_request_runs_in_worker_thread(qapp, session):
    bridge = UiBridge(session.bus, interval_ms=200)
    bridge.start()
    p = ConsolePage(session, bridge)
    try:
        done = threading.Event()
        main_thread = threading.get_ident()

        def slow_request(payload, **kwargs):
            assert threading.get_ident() != main_thread, "等应答不能在主线程执行"
            res = CommandResult(ts=time.time(), name="手动等待", ok=True, reply=b"1")
            done.set()
            return res

        session.passthrough.request = slow_request  # type: ignore[method-assign]
        p.edit_payload.setText("01")
        p.btn_request.click()
        assert done.wait(timeout=3.0), "后台线程应完成下发"
        deadline = time.time() + 2.0
        while p._busy and time.time() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.01)
        assert p._busy is False, "worker 信号回投后按钮必须恢复"
    finally:
        p.on_close()
        bridge.stop()


# ------------------------------------------------------------------ 桥事件
def test_transport_state_offline_disables_send(page, session):
    def state(evt):
        return {"ts": time.time(), "kind": "at", "name": "AT-local", "detail": "",
                "channel": "serial", "endpoint": "local", **evt}

    session.bus.publish(Events.TRANSPORT_STATE, state({"state": "closed"}))
    _tick(page.bridge)
    assert page.btn_send.isEnabled() is False
    assert page.btn_request.isEnabled() is False
    session.bus.publish(Events.TRANSPORT_STATE, state({"state": "open"}))
    _tick(page.bridge)
    assert page.btn_send.isEnabled() is True


# ------------------------------------------------------------------ 485 串口配置框
def test_serial_group_defaults_follow_cfg(page, svc):
    ch = page._serial_changes()
    scfg = svc.cfg.serial
    assert ch["baudrate"] == scfg.baudrate
    assert ch["bytesize"] == scfg.bytesize
    assert ch["parity"] == scfg.parity
    assert ch["stopbits"] == scfg.stopbits
    assert "当前：" in page.lbl_serial.text()


def test_serial_refresh_fills_ports_with_usb_mark(page, session, monkeypatch):
    monkeypatch.setattr(
        session.link,
        "scan_ports",
        lambda: [
            {
                "device": "COM9",
                "description": "USB-SERIAL CH340",
                "hwid": "USB VID:PID=1A86:7523",
                "vid": "1A86",
                "pid": "7523",
                "likely_usb_uart": True,
            }
        ],
    )
    page.combo_port.setCurrentIndex(-1)  # 排除真实 config/app.yaml 初始值干扰
    page.btn_ports.click()
    assert page.combo_port.currentData() == "COM9"
    assert "USB" in page.combo_port.currentText()
    # 已选口掉线时刷新不清空选择，标注"当前未在线"（clicked 注入的 bool 已被防御）
    monkeypatch.setattr(session.link, "scan_ports", lambda: [])
    page.btn_ports.click()
    assert page.combo_port.currentData() == "COM9"
    assert "未在线" in page.combo_port.currentText()


def test_serial_apply_forwards_params_and_logs(page, svc, session, monkeypatch):
    seen: dict = {}

    def fake_apply(**kw):
        seen.update(kw)
        return {"ok": True, "detail": "透传串口已切到 COM9 @ 57600 8N1（已保存）"}

    monkeypatch.setattr(session.link, "apply_passthrough_serial", fake_apply)
    monkeypatch.setattr(svc.cfg, "save", lambda: None)
    page.combo_baud.setCurrentText("57600")
    before = page.log_view.count()
    page.btn_serial.click()
    assert seen["baudrate"] == 57600
    assert seen["parity"] == svc.cfg.serial.parity
    assert page.log_view.count() == before + 1
    assert "485 串口" in page.log_view.item(before).text()


def test_serial_apply_survives_cfg_save_failure(page, svc, session, monkeypatch):
    monkeypatch.setattr(
        session.link, "apply_passthrough_serial", lambda **kw: {"ok": True, "detail": "已切换"}
    )

    def boom():
        raise OSError("app.yaml 只读")

    monkeypatch.setattr(svc.cfg, "save", boom)
    page.btn_serial.click()  # 写回失败不能弹崩溃，只能降级提示
    last = page.log_view.item(page.log_view.count() - 1).text()
    assert "仅内存生效" in last


def test_watch_pt_ignores_at_events_and_tracks_pt(page, session):
    """分线模式盯 kind=pt：AT 线开合与 485 发送可用性无关。"""
    page._watch_pt = True

    def state(evt, kind="pt"):
        return {
            "ts": time.time(), "kind": kind, "name": "PT-local", "detail": "",
            "channel": "serial", "endpoint": "local", **evt,
        }

    session.bus.publish(Events.TRANSPORT_STATE, state({"state": "closed"}))
    _tick(page.bridge)
    assert page.btn_send.isEnabled() is False
    session.bus.publish(Events.TRANSPORT_STATE, state({"state": "open"}, kind="at"))
    _tick(page.bridge)
    assert page.btn_send.isEnabled() is False, "AT 线上线不该恢复 485 发送按钮"
    session.bus.publish(Events.TRANSPORT_STATE, state({"state": "open"}))
    _tick(page.bridge)
    assert page.btn_send.isEnabled() is True


def test_cmd_result_from_bridge_lands_in_log(page, session):
    res = CommandResult(ts=time.time(), name="读保持寄存器", ok=False, error="等待应答超时")
    session.bus.publish(Events.CMD_RESULT, res)
    _tick(page.bridge)
    assert page.log_view.count() == 1
    assert "超时" in page.log_view.item(0).text()


# ------------------------------------------------------------------ 模板下发
def _stub_run_template(svc, calls, monkeypatch):
    def fake_run(tid, values=None):
        calls.append((tid, values))
        return CommandResult(ts=time.time(), name=tid, ok=True)

    monkeypatch.setattr(svc, "run_template", fake_run)


def test_double_click_plain_template_runs_directly(page, svc, monkeypatch):
    calls: list = []
    _stub_run_template(svc, calls, monkeypatch)
    tpl = CommandTemplate(id="t_direct", name="直发", payload="AA BB", expect_reply=False)
    page._send_template(tpl)
    assert calls == [("t_direct", {})]


def test_placeholder_template_asks_params_first(page, svc, monkeypatch):
    calls: list = []
    _stub_run_template(svc, calls, monkeypatch)
    tpl = CommandTemplate(id="t_ph", name="带参", payload="AA {ch}", expect_reply=False)
    monkeypatch.setattr(page, "_ask_params", lambda t, names: {"ch": "01"})
    page._send_template(tpl)
    assert calls == [("t_ph", {"ch": "01"})]

    calls.clear()
    monkeypatch.setattr(page, "_ask_params", lambda t, names: None)
    page._send_template(tpl)
    assert calls == [], "取消参数框就不能下发"


def test_busy_blocks_template_dispatch(page, svc, monkeypatch):
    calls: list = []
    monkeypatch.setattr(svc, "run_template", lambda *a, **k: calls.append(1))
    page._set_busy(True)
    page._send_template(CommandTemplate(id="t_busy", payload="AA", expect_reply=True))
    assert calls == []
    page._set_busy(False)


def test_disabled_template_is_not_sent(page, svc, monkeypatch):
    calls: list = []
    monkeypatch.setattr(svc, "run_template", lambda *a, **k: calls.append(1))
    page._send_template(CommandTemplate(id="t_off", payload="AA", enabled=False))
    assert calls == []


# ------------------------------------------------------------------ 模板 CRUD
def test_apply_edit_rejects_invalid_template(page, svc):
    before = len(svc.templates.ordered())
    ok = page._apply_edit(CommandTemplate(id="bad", name="坏", payload=""), persist=False)
    assert ok is False
    assert len(svc.templates.ordered()) == before


def test_apply_edit_and_remove_roundtrip(page, svc):
    before = len(svc.templates.ordered())
    tpl = CommandTemplate(id="t_new", name="新指令", payload="01 02")
    assert page._apply_edit(tpl, persist=False) is True
    assert len(svc.templates.ordered()) == before + 1
    assert svc.templates.get("t_new") is not None

    renamed = CommandTemplate(id="t_new", name="改名", payload="03 04")
    assert page._apply_edit(renamed, persist=False)
    assert svc.templates.get("t_new").name == "改名"
    assert len(svc.templates.ordered()) == before + 1

    svc.templates.remove("t_new")
    page._refresh_tree()
    assert len(svc.templates.ordered()) == before


def test_apply_edit_persist_writes_yaml(page, svc, tmp_path):
    monkey = tmp_path / "templates.yaml"
    svc.templates.path = monkey
    assert page._apply_edit(CommandTemplate(id="t_p", name="落盘", payload="05"), persist=True)
    assert monkey.exists()
    assert "t_p" in monkey.read_text(encoding="utf-8")


def test_move_selected_reorders(page, svc, monkeypatch):
    monkeypatch.setattr(svc.templates, "save", lambda *a, **k: None)  # 本用例不落盘
    svc.templates.upsert(CommandTemplate(id="mv_a", name="A", payload="AA"))
    svc.templates.upsert(CommandTemplate(id="mv_b", name="B", payload="BB"))
    page._refresh_tree()
    try:
        target = None
        for i in range(page.tree.topLevelItemCount()):
            grp = page.tree.topLevelItem(i)
            for j in range(grp.childCount()):
                if grp.child(j).data(0, _TID_ROLE) == "mv_a":
                    target = grp.child(j)
        assert target is not None, "树里应能找到刚新增的 mv_a"
        page.tree.setCurrentItem(target)
        ids_before = [t.id for t in svc.templates.ordered()]
        pos_a = ids_before.index("mv_a")
        assert ids_before[pos_a + 1] == "mv_b", "两条测试模板应相邻且在末尾"
        page._on_move_clicked(1)
        ids_after = [t.id for t in svc.templates.ordered()]
        assert ids_after.index("mv_a") == pos_a + 1
        assert ids_after.index("mv_b") == pos_a
    finally:
        svc.templates.remove("mv_a")
        svc.templates.remove("mv_b")
        page._refresh_tree()


# ------------------------------------------------------------------ 对话框
def test_template_dialog_preserves_order(svc):
    base = CommandTemplate(id="dlg_1", name="保持顺序", payload="AA", order=20)
    dlg = _TemplateDialog(svc.templates, base)
    assert dlg.collect().order == 20
    assert dlg.collect().id == "dlg_1"
    dlg.deleteLater()


def test_params_dialog_previews_bytes(svc):
    tpl = CommandTemplate(id="dlg_2", name="带参", payload="01 {ch}", mode="hex", suffix="none")
    dlg = _ParamsDialog(svc.templates, tpl, ["ch"])
    dlg._edits["ch"].setText("05")
    assert "将发送" in dlg.lbl_preview.text()
    assert dlg.values() == {"ch": "05"}
    dlg.deleteLater()


def test_params_dialog_blocks_incomplete(svc):
    tpl = CommandTemplate(id="dlg_3", name="带参", payload="01 {ch}")
    dlg = _ParamsDialog(svc.templates, tpl, ["ch"])
    dlg._on_accept()
    assert dlg.result() != 1, "参数没填完不能接受"
    dlg._edits["ch"].setText("02")
    dlg._on_accept()
    assert dlg.result() == 1
    dlg.deleteLater()


# ------------------------------------------------------------------ 降级
def test_page_without_passthrough_service(qapp, session):
    fake = SimpleNamespace(bus=session.bus, cfg=session.cfg, passthrough=None)
    bridge = UiBridge(fake.bus, interval_ms=200)
    p = ConsolePage(fake, bridge)
    try:
        assert p.btn_send.isEnabled() is False
        assert p.btn_request.isEnabled() is False
        assert "未装配" in p.lbl_preview.text()
        p.edit_payload.setText("AA")  # 控件本就禁用，手动调用也不能炸
        p._update_preview()
        p._send_template(CommandTemplate(id="x", payload="AA"))
    finally:
        p.on_close()
