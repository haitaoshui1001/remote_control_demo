"""摄像头连接对话框测试（offscreen）。

沿用"publish → bridge._tick() → 看控件"的确定性节奏，服务层整个换成假对象。
盯五件事：
1. 打开时控件按真实配置灌值，改动只落在副本上——不点【保存】不许碰 cfg.video。
2. "生效地址"预览必须与 ``VideoCfg.resolved_url`` 同一结论，换码流档位要跟着换。
3. 【测试】是异步的：在途期间按钮禁用、重复点击给"稍等"，结果回来才解锁。
4. 【保存】把副本交给 ``apply_connection``，成功后回调页面并重对齐副本。
5. 【恢复当前配置】丢弃未保存的改动。
"""

from __future__ import annotations

import pytest

from hmi.core.config import AppConfig
from hmi.core.events import EventBus, Events
from hmi.services.camera_config import CameraProbe
from hmi.ui.bridge import UiBridge
from hmi.ui.widgets.camera_conn_dialog import CameraConnDialog


class FakeCameraService:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.probe_calls: list[tuple[object, int]] = []
        self.saved: list[object] = []
        self.probe_return = True
        self.save_return = {"ok": True, "detail": "已写回 app.yaml", "reconnected": True}

    def probe_async(self, v=None, stream_index=None) -> bool:
        self.probe_calls.append((v, -1 if stream_index is None else int(stream_index)))
        return self.probe_return

    def apply_connection(self, snapshot) -> dict:
        self.saved.append(snapshot)
        return dict(self.save_return)


def _cfg() -> AppConfig:
    cfg = AppConfig()
    v = cfg.video
    v.camera_ip = "192.168.1.60"
    v.rtsp_port = 554
    v.onvif_port = 80
    v.user = "admin"
    v.password = "pw123456"
    v.url = "rtsp://admin:pw123456@192.168.1.60:554/stream1"
    v.stream_paths = ["stream1", "stream2"]
    v.stream_index = 0
    v.transport = "udp"
    return cfg


@pytest.fixture()
def env(qapp):
    bus = EventBus()
    bridge = UiBridge(bus, interval_ms=200)
    bridge.start()
    cfg = _cfg()
    fake = FakeCameraService(cfg)
    hits: list[int] = []
    dialog = CameraConnDialog(fake, bridge, on_saved=lambda: hits.append(1))
    yield bus, bridge, cfg, fake, dialog, hits
    bridge.stop()


def _deliver_probe(bus, bridge, res: CameraProbe) -> None:
    bus.publish(Events.CAMERA_PROBE, res)
    bridge._tick()


# ---------------------------------------------------------------- 灌值与副本隔离
def test_widgets_mirror_saved_config(env):
    _, _, cfg, _, dialog, _ = env
    assert dialog.edit_ip.text() == "192.168.1.60"
    assert (dialog.spin_rtsp.value(), dialog.spin_onvif.value()) == (554, 80)
    assert dialog.edit_user.text() == "admin" and dialog.edit_pwd.text() == "pw123456"
    assert [w.text() for w in dialog.edit_paths] == ["stream1", "stream2"]
    assert int(dialog.combo_stream.currentData()) == 0
    assert dialog.combo_transport.currentData() == "udp"
    assert dialog.edit_url.text() == cfg.video.url
    assert dialog.lbl_preview.text() == cfg.video.url


def test_editing_does_not_touch_real_config(env):
    """没点【保存】之前，真实配置一个字都不能变。"""
    _, _, cfg, fake, dialog, hits = env
    dialog.edit_ip.setText("10.0.0.9")
    assert cfg.video.camera_ip == "192.168.1.60"
    assert fake.saved == [] and hits == []
    dialog.btn_revert.click()
    assert dialog.edit_ip.text() == "192.168.1.60"


def test_password_echo_toggle(env):
    from PyQt5.QtWidgets import QLineEdit

    _, _, _, _, dialog, _ = env
    assert dialog.edit_pwd.echoMode() == QLineEdit.Password
    dialog.chk_pwd.setChecked(True)
    assert dialog.edit_pwd.echoMode() == QLineEdit.Normal


# ---------------------------------------------------------------- 生效地址预览
def test_preview_follows_stream_tier(env):
    _, _, cfg, _, dialog, _ = env
    dialog.combo_stream.setCurrentIndex(dialog.combo_stream.findData(1))
    assert dialog.lbl_preview.text() == "rtsp://admin:pw123456@192.168.1.60:554/stream2"
    assert "子码流" in dialog.lbl_preview.toolTip()


def test_preview_composes_when_manual_url_cleared(env):
    """手填地址清空后回自动拼装，且密码里有保留字符要转义。"""
    _, _, _, _, dialog, _ = env
    dialog.edit_url.setText("")
    assert dialog.lbl_preview.text() == "rtsp://admin:pw123456@192.168.1.60:554/stream1"
    dialog.edit_ip.setText("192.168.1.7")
    dialog.spin_rtsp.setValue(8554)
    assert dialog.lbl_preview.text().startswith("rtsp://admin:pw123456@192.168.1.7:8554/")


def test_preview_drops_credentials_when_password_empty(env):
    _, _, _, _, dialog, _ = env
    dialog.edit_url.setText("")
    dialog.edit_pwd.setText("")
    assert dialog.lbl_preview.text() == "rtsp://192.168.1.60:554/stream1"


# ---------------------------------------------------------------- 测试按钮
def test_test_button_sends_snapshot_not_saved_config(env):
    bus, bridge, cfg, fake, dialog, _ = env
    dialog.edit_ip.setText("10.0.0.9")
    dialog.edit_url.setText("")
    dialog.combo_stream.setCurrentIndex(dialog.combo_stream.findData(1))
    dialog.btn_test.click()
    snap, idx = fake.probe_calls[-1]
    assert snap.camera_ip == "10.0.0.9" and idx == 1
    assert cfg.video.camera_ip == "192.168.1.60", "自检按还没保存的参数跑"
    assert snap.resolved_url(idx) == "rtsp://admin:pw123456@10.0.0.9:554/stream2"
    assert "自检中" in dialog.btn_test.text() and not dialog.btn_test.isEnabled()
    assert dialog.lbl_preview.text() in dialog.lbl_status.text()


def test_test_button_busy_guard(env):
    _, _, _, fake, dialog, _ = env
    fake.probe_return = False
    dialog.btn_test.click()
    assert "上一次自检还没结束" in dialog.lbl_status.text()
    assert dialog.btn_test.isEnabled(), "没起跑就不该把按钮按住"


def test_second_click_while_probing_is_ignored(env):
    """在途期间按钮禁用；槽函数被别处直接调用也得拦住（自检穿空口要几秒）。"""
    bus, bridge, _, fake, dialog, _ = env
    dialog.btn_test.click()
    assert not dialog.btn_test.isEnabled(), "按钮自己就是第一道护栏"
    dialog._on_test()
    assert len(fake.probe_calls) == 1, "在途期间不得重复触发"
    assert "自检还在进行中" in dialog.lbl_status.text()
    assert "正在探测" not in dialog.lbl_status.text(), "拦截提示要覆盖进度文案"


def test_test_button_refuses_empty_target(env):
    _, _, _, fake, dialog, _ = env
    dialog.edit_ip.setText("")
    dialog.edit_url.setText("")
    dialog.btn_test.click()
    assert fake.probe_calls == [] and "先填 IP" in dialog.lbl_status.text()


def test_probe_result_renders_two_lines(env):
    bus, bridge, _, fake, dialog, _ = env
    dialog.btn_test.click()
    _deliver_probe(
        bus, bridge,
        CameraProbe(
            ok=False, rtsp_url="rtsp://10.0.0.9/stream1", rtsp_ok=False,
            rtsp_detail="DESCRIBE 回 404：推流路径不存在，核对主/子码流路径（如 stream1/stream2）",
            onvif_ok=True, onvif_detail="ONVIF 正常：TP-LINK / TL-IPC341P-W2.8",
        ),
    )
    text = dialog.lbl_status.text()
    assert text.startswith("× 取流：DESCRIBE 回 404") and "√ ONVIF：ONVIF 正常" in text
    assert "#b23b3b" in dialog.lbl_status.styleSheet()
    assert dialog.btn_test.isEnabled() and dialog.btn_test.text() == "测试"

    dialog.btn_test.click()
    _deliver_probe(
        bus, bridge,
        CameraProbe(ok=True, rtsp_ok=True, rtsp_detail="DESCRIBE 正常：视频 1 路，1920x1080@25fps",
                    onvif_ok=True, onvif_detail="ONVIF 正常：TP-LINK"),
    )
    assert "√ 取流" in dialog.lbl_status.text() and "#1d7a3d" in dialog.lbl_status.styleSheet()
    assert len(fake.probe_calls) == 2


def test_probe_topic_other_than_camera_probe_ignored(env):
    """摄像头参数对话框的结果不该被这里当自检结论显示。"""
    bus, bridge, _, _, dialog, _ = env
    before = dialog.lbl_status.text()
    bus.publish(Events.CAMERA_CFG, object())
    bridge._tick()
    assert dialog.lbl_status.text() == before


# ---------------------------------------------------------------- 保存
def test_save_passes_snapshot_and_notifies_page(env):
    _, _, cfg, fake, dialog, hits = env
    dialog.edit_pwd.setText("newpass")
    dialog.combo_stream.setCurrentIndex(dialog.combo_stream.findData(1))
    dialog.btn_save.click()
    snap = fake.saved[-1]
    assert snap.password == "newpass" and snap.stream_index == 1
    assert cfg.video.password == "pw123456", "落盘由服务层负责，对话框不许直接改 cfg"
    assert hits == [1], "保存成功要回调页面同步工具条"
    assert "保存结果：已写回" in dialog.lbl_status.text()
    assert dialog._base.password == cfg.video.password, "副本基线要跟着真实配置刷新"


def test_save_failure_keeps_edits_and_skips_callback(env):
    _, _, _, fake, dialog, hits = env
    fake.save_return = {"ok": False, "detail": "摄像头 IP 和取流地址都是空的：至少填一项"}
    dialog.edit_ip.setText("10.1.1.1")
    dialog.btn_save.click()
    assert hits == []
    assert "至少填一项" in dialog.lbl_status.text()
    assert "#b23b3b" in dialog.lbl_status.styleSheet()
    assert dialog.edit_ip.text() == "10.1.1.1", "保存失败别把用户的输入抹掉"


def test_save_realigns_base_so_reopen_keeps_values(env):
    """保存后真实配置就是新的，未保存的草稿基线要跟着对齐。"""
    _, _, cfg, fake, dialog, _ = env
    cfg.video.password = "written-by-service"
    cfg.video.camera_ip = "172.16.31.5"
    fake.save_return = {"ok": True, "detail": "已写回"}
    dialog.btn_save.click()
    assert dialog._base.camera_ip == "172.16.31.5"
    assert dialog.edit_ip.text() == "192.168.1.60", "控件仍显示用户填的值，不强行回填"


def test_show_event_reloads_when_widgets_empty(env):
    """窗口留着、配置被别处改过（如命令行改 app.yaml）时，重新打开要跟上。"""
    _, _, cfg, _, dialog, _ = env
    dialog.edit_ip.setText("")
    dialog.edit_url.setText("")
    cfg.video.camera_ip = "192.168.9.9"
    dialog.show()
    assert dialog.edit_ip.text() == "192.168.9.9"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
