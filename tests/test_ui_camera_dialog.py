"""摄像头配置对话框测试（offscreen）。

沿用"publish → bridge._tick() → 看控件"的确定性节奏，网络层整段换成
假服务。重点盯四件事：
1. 没读到值域前控件必须锁死——不让用户盲填。
2. load 成功后按设备实报填控件：编码/分辨率/帧率/码率范围/码率模式。
3. apply 组装的 EncoderSettings 必须保留 current 的 token/name/quality/
   gov_length——写错 token 会毁掉另一档码流。
4. 忙时护栏：服务返回 False 要给出"请稍候"文案，不能卡死按钮。
5. 编码联动：分辨率档位按编码给，换编码要换列表且保住仍然合法的已选档位。
"""

from __future__ import annotations

import pytest

from hmi.core.events import EventBus, Events
from hmi.protocol.onvif import EncoderOptions, EncoderSettings
from hmi.services.camera_config import CameraParams
from hmi.ui.bridge import UiBridge
from hmi.ui.widgets.camera_dialog import CameraConfigDialog


class FakeCameraService:
    def __init__(self) -> None:
        self.load_calls: list[int] = []
        self.apply_calls: list[EncoderSettings] = []
        self.load_return = True
        self.apply_return = True

    def load_async(self, stream_index: int) -> bool:
        self.load_calls.append(int(stream_index))
        return self.load_return

    def apply_async(self, st: EncoderSettings) -> bool:
        self.apply_calls.append(st)
        return self.apply_return


def _options() -> EncoderOptions:
    return EncoderOptions(
        encodings=["H264", "H265"],
        resolutions=[(640, 360), (1280, 720), (1920, 1080)],
        fps_range=(1, 25),
        bitrate_range=(64, 4096),
        source="device",
    )


def _current() -> EncoderSettings:
    return EncoderSettings(
        encoder_token="ENC1", name="SubStream", encoding="H264",
        width=1280, height=720, frame_rate=15, bitrate_kbps=1024,
        rate_mode="V", quality=4.0, gov_length=20,
    )


@pytest.fixture()
def env(qapp):
    bus = EventBus()
    bridge = UiBridge(bus, interval_ms=200)
    bridge.start()
    fake = FakeCameraService()
    dialog = CameraConfigDialog(fake, bridge, stream_index=1)
    yield bus, bridge, fake, dialog
    bridge.stop()


def _deliver(bus, bridge, res: CameraParams) -> None:
    bus.publish(Events.CAMERA_CFG, res)
    bridge._tick()


def _load_ok(bus, bridge, fake, dialog) -> None:
    dialog._on_load()
    _deliver(
        bus, bridge,
        CameraParams(ok=True, action="load", stream_index=1, auth_mode="wsse",
                     profile_name="Chan01_Sub", current=_current(),
                     options=_options()),
    )


# ---------------------------------------------------------------- 初始锁死
def test_controls_locked_before_load(env):
    _, _, _, dialog = env
    assert not dialog.btn_apply.isEnabled()
    assert not dialog.combo_resolution.isEnabled()
    assert dialog.btn_load.isEnabled()


# ---------------------------------------------------------------- 读取
def test_autoload_targets_selected_stream(env):
    bus, bridge, fake, dialog = env
    dialog.maybe_autoload()
    assert fake.load_calls == [1]
    assert "读取中" in dialog.lbl_status.text()
    dialog.maybe_autoload()
    assert len(fake.load_calls) == 1, "在途期间不得重复触发"


def test_load_result_fills_controls_and_unlocks(env):
    bus, bridge, fake, dialog = env
    _load_ok(bus, bridge, fake, dialog)
    assert dialog.combo_encoding.count() == 2
    assert dialog.combo_resolution.currentText() == "1280x720"
    assert int(dialog.combo_fps.currentData()) == 15
    assert (dialog.spin_bitrate.minimum(), dialog.spin_bitrate.maximum()) == (64, 4096)
    assert dialog.spin_bitrate.value() == 1024
    assert dialog.combo_rate.currentData() == "V"
    assert dialog.btn_apply.isEnabled()
    text = dialog.lbl_status.text()
    assert text.startswith("当前：1280x720") and "设备实报值域" in text


def test_autoload_skipped_after_fill(env):
    bus, bridge, fake, dialog = env
    _load_ok(bus, bridge, fake, dialog)
    dialog.maybe_autoload()
    assert len(fake.load_calls) == 1, "已有数据不再自动读"


def test_load_failure_shows_error_and_reenables(env):
    bus, bridge, fake, dialog = env
    dialog._on_load()
    _deliver(
        bus, bridge,
        CameraParams(ok=False, action="load", stream_index=1,
                     error="连接被拒绝"),
    )
    assert dialog.lbl_status.text() == "失败：连接被拒绝"
    assert dialog.btn_load.isEnabled()
    assert not dialog.btn_apply.isEnabled()


def test_load_busy_guard(env):
    _, _, fake, dialog = env
    fake.load_return = False
    dialog._on_load()
    assert "还没结束" in dialog.lbl_status.text()
    assert dialog.btn_load.isEnabled()
    assert dialog._loading is False


# ---------------------------------------------------------------- 应用
def test_apply_requires_loaded_current(env):
    _, _, fake, dialog = env
    dialog.btn_apply.setEnabled(True)  # 绕过锁死，直接验证护栏本身
    dialog.btn_apply.click()
    assert fake.apply_calls == [], "没读到 current 绝不能写设备"


def test_apply_preserves_token_and_sends_edited_values(env):
    bus, bridge, fake, dialog = env
    _load_ok(bus, bridge, fake, dialog)
    i = dialog.combo_resolution.findText("1920x1080")
    dialog.combo_resolution.setCurrentIndex(i)
    dialog.combo_fps.setCurrentIndex(dialog.combo_fps.findData(25))
    dialog.spin_bitrate.setValue(4000)
    dialog.combo_rate.setCurrentIndex(dialog.combo_rate.findData("C"))
    dialog.btn_apply.click()
    st = fake.apply_calls[-1]
    assert (st.width, st.height, st.frame_rate, st.bitrate_kbps) == (
        1920, 1080, 25, 4000,
    )
    assert st.rate_mode == "C"
    assert st.encoder_token == "ENC1" and st.name == "SubStream"
    assert st.quality == 4.0 and st.gov_length == 20
    assert "写入中" in dialog.lbl_status.text()


def test_apply_carries_back_uneditable_encoder_fields(env):
    """界面上没给改的抽帧间隔/编码档位要原样带回，不能让默认值把设备配置冲掉。"""
    bus, bridge, fake, dialog = env
    dialog._on_load()
    _deliver(
        bus, bridge,
        CameraParams(ok=True, action="load", stream_index=0, auth_mode="wsse",
                     profile_name="mainStream", options=_options(),
                     current=EncoderSettings(
                         encoder_token="000", name="V_ENC_000", encoding="H264",
                         width=1280, height=720, frame_rate=15, bitrate_kbps=1024,
                         encoding_interval=2, codec_profile="High")),
    )
    dialog.btn_apply.click()
    st = fake.apply_calls[-1]
    assert st.encoding_interval == 2, "抽帧间隔得回写，否则设备上的 2 会被抹成 0"
    assert st.codec_profile == "High", "编码档位得回写，不能凭空造一个 Main"


def test_apply_success_reports_fixed_values(env):
    bus, bridge, fake, dialog = env
    _load_ok(bus, bridge, fake, dialog)
    dialog.btn_apply.click()
    applied = fake.apply_calls[-1]
    _deliver(
        bus, bridge,
        CameraParams(ok=True, action="apply", auth_mode="wsse",
                     applied=applied, fixed=("帧率 60→25",)),
    )
    text = dialog.lbl_status.text()
    assert "已应用" in text and "已修正：帧率 60→25" in text
    assert dialog.btn_load.isEnabled()


def test_apply_failure_shows_error(env):
    bus, bridge, fake, dialog = env
    _load_ok(bus, bridge, fake, dialog)
    dialog.btn_apply.click()
    _deliver(
        bus, bridge,
        CameraParams(ok=False, action="apply", error="设备拒绝写入"),
    )
    assert dialog.lbl_status.text() == "失败：设备拒绝写入"


# ---------------------------------------------------------------- 编码联动
def _xm_options() -> EncoderOptions:
    return EncoderOptions(
        encodings=["H264", "H265"],
        resolutions=[(1280, 720), (1920, 1080), (2304, 1296)],
        resolutions_by_encoding={
            "H264": [(1280, 720), (1920, 1080), (2304, 1296)],
            "H265": [(1280, 720), (1920, 1080)],
        },
        fps_range=(1, 25),
        fps_supported=list(range(1, 26)),
        bitrate_range=(32, 10240),
        source="device",
    )


def test_resolution_list_follows_encoding(env):
    """H265 不供 2304x1296：切编码要换列表，并把选择退到面积最接近的一档。"""
    bus, bridge, fake, dialog = env
    dialog._on_load()
    _deliver(
        bus, bridge,
        CameraParams(ok=True, action="load", stream_index=0, auth_mode="wsse",
                     profile_name="mainStream", options=_xm_options(),
                     current=EncoderSettings(
                         encoder_token="000", name="V_ENC_000", encoding="H264",
                         width=2304, height=1296, frame_rate=20, bitrate_kbps=2000)),
    )
    assert dialog.combo_resolution.currentText() == "2304x1296"
    dialog.combo_encoding.setCurrentIndex(dialog.combo_encoding.findText("H265"))
    assert [dialog.combo_resolution.itemText(i) for i in range(
        dialog.combo_resolution.count())] == ["1280x720", "1920x1080"]
    assert dialog.combo_resolution.currentText() == "1920x1080"
    dialog.btn_apply.click()
    st = fake.apply_calls[-1]
    assert (st.encoding, st.width, st.height) == ("H265", 1920, 1080)
    assert int(dialog.combo_fps.currentData()) == 20


def test_resolution_selection_kept_when_encoding_has_no_tiers(env):
    """设备没分档（退回并集）时，用户已选的档位不许被联动改没。"""
    bus, bridge, fake, dialog = env
    _load_ok(bus, bridge, fake, dialog)
    dialog.combo_resolution.setCurrentIndex(dialog.combo_resolution.findText("640x360"))
    dialog.combo_encoding.setCurrentIndex(dialog.combo_encoding.findText("H265"))
    assert dialog.combo_resolution.currentText() == "640x360"


def test_encoding_combo_never_shows_blank(env):
    """当前值不在设备自报的 Options 里（实机这种不一致见过）也得选上。

    选不中的话下拉框会显示成一片空白，用户既看不出当前是什么，也没法改。
    """
    bus, bridge, fake, dialog = env
    dialog._on_load()
    _deliver(
        bus, bridge,
        CameraParams(ok=True, action="load", stream_index=0, auth_mode="wsse",
                     profile_name="mainStream",
                     options=EncoderOptions(
                         encodings=["H264"], resolutions=[(1280, 720)],
                         fps_range=(1, 25), bitrate_range=(64, 4096),
                         source="device"),
                     current=EncoderSettings(
                         encoder_token="000", name="V_ENC_000", encoding="MJPEG",
                         width=1280, height=720, frame_rate=15, bitrate_kbps=1024)),
    )
    assert dialog.combo_encoding.currentText() == "MJPEG", "空白框=用户看不见当前值"
    assert dialog.combo_encoding.findText("H264") >= 0

    # Options 一项都没报时也不能空着
    dialog._on_load()
    _deliver(
        bus, bridge,
        CameraParams(ok=True, action="load", stream_index=0, auth_mode="wsse",
                     profile_name="mainStream",
                     options=EncoderOptions(
                         encodings=[], resolutions=[(1280, 720)],
                         fps_range=(1, 25), bitrate_range=(64, 4096),
                         source="device"),
                     current=EncoderSettings(
                         encoder_token="000", name="V_ENC_000", encoding="H265",
                         width=1280, height=720, frame_rate=15, bitrate_kbps=1024)),
    )
    assert dialog.combo_encoding.currentText() == "H265"
    assert dialog.combo_resolution.currentText() == "1280x720"


def test_rate_mode_combo_is_readonly_echo(env):
    """Media1 写不了码率模式，控件必须常驻禁用，免得用户改了以为生效。"""
    bus, bridge, fake, dialog = env
    assert not dialog.combo_rate.isEnabled()
    assert "仅作回显" in dialog.combo_rate.toolTip()
    _load_ok(bus, bridge, fake, dialog)
    assert not dialog.combo_rate.isEnabled(), "读取成功后也不许解锁"
    assert dialog.combo_encoding.isEnabled() and dialog.btn_apply.isEnabled()


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
