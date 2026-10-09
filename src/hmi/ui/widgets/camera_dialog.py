"""摄像头远程配置对话框：读/改分辨率、帧率、码率（走 ONVIF）。

合法值域永远来自设备自报的 Options——没读到就锁死控件，不让用户盲填。
分辨率档位是按编码分别给的（实机 H264 比 H265 多一档 2304x1296），所以换
编码要立刻换分辨率列表，否则能把设备根本不供的档位点出去。
所有网络动作丢给 ``CameraConfigService`` 的工作线程，本控件只负责：
点【读取】→ 异步发请求、按钮变"读取中…"；桥回调送来结果 → 填控件、解锁。
【应用】同理：写成功后服务层会自动重启取流，新参数当场生效。
"""

from __future__ import annotations

from typing import Any

from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core.events import Events
from ...protocol.onvif import EncoderSettings

__all__ = ["CameraConfigDialog"]

_RATE_MODES = (("CBR", "C"), ("VBR", "V"))


def _parse_px(text: str) -> tuple[int, int]:
    """把下拉里的 ``"1280x720"`` 拆回宽高；拆不动就返回 0（由协议层拦下）。"""
    w, _, h = str(text).partition("x")
    try:
        return int(w), int(h)
    except ValueError:
        return 0, 0


class CameraConfigDialog(QDialog):
    """一路摄像头 = 一个对话框；主/子码流切换即切换 ONVIF Profile。"""

    def __init__(
        self,
        camera_service: Any,
        bridge: Any,
        stream_index: int = 1,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.camera = camera_service
        self.bridge = bridge
        self.setWindowTitle("摄像头参数（远程配置）")
        self.setModal(False)

        self._current: EncoderSettings | None = None
        self._options: Any = None
        self._loading = False

        self.combo_stream = QComboBox()
        self.combo_stream.addItem("主码流", 0)
        self.combo_stream.addItem("子码流", 1)
        idx = self.combo_stream.findData(int(stream_index))
        self.combo_stream.setCurrentIndex(idx if idx >= 0 else 1)
        self.combo_stream.setToolTip("ONVIF Profile：主码流=0，子码流=1")

        self.btn_load = QPushButton("读取")
        self.btn_load.clicked.connect(self._on_load)

        self.combo_encoding = QComboBox()
        self.combo_resolution = QComboBox()
        self.combo_encoding.currentTextChanged.connect(self._on_encoding_changed)
        self.combo_fps = QComboBox()
        self.combo_rate = QComboBox()
        for label, mode in _RATE_MODES:
            self.combo_rate.addItem(label, mode)
        # 实机走 Media1，写入形状里根本没有恒定/可变之分（码率只落在 BitrateLimit
        # 上），改这项不会生效。只回显、不让改，免得用户以为改了却看不到效果。
        self.combo_rate.setEnabled(False)
        self.combo_rate.setToolTip(
            "仅作回显：设备按 ONVIF Media1 配置，不区分恒定/可变码率；"
            "码率高低由下面的【目标码率】决定"
        )
        self.spin_bitrate = QSpinBox()
        self.spin_bitrate.setSuffix(" kbps")

        self.form = QFormLayout()
        self.form.addRow("码流", self.combo_stream)
        self.form.addRow("编码格式", self.combo_encoding)
        self.form.addRow("分辨率", self.combo_resolution)
        self.form.addRow("帧率", self.combo_fps)
        self.form.addRow("码率控制", self.combo_rate)
        self.form.addRow("目标码率", self.spin_bitrate)

        self.btn_apply = QPushButton("应用")
        self.btn_apply.clicked.connect(self._on_apply)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.close)
        row = QHBoxLayout()
        row.addWidget(self.btn_load)
        row.addStretch(1)
        row.addWidget(self.btn_apply)
        row.addWidget(self.btn_close)

        self.lbl_status = QLabel("点【读取】获取当前参数与设备支持的值域")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet("color:#3d4a5c;")

        box = QVBoxLayout(self)
        box.addLayout(self.form)
        box.addLayout(row)
        box.addWidget(self.lbl_status)
        box.addWidget(QLabel("说明：改码率可缓解图传空口带宽；应用后自动重连取流。"))

        self._set_editable(False)
        bridge.slot_updated.connect(self._on_slot)

    # ------------------------------------------------------------------ 状态
    def _set_editable(self, on: bool) -> None:
        # combo_rate 不在名单里：Media1 写不了码率模式，它常驻禁用只作回显
        for w in (
            self.combo_encoding,
            self.combo_resolution,
            self.combo_fps,
            self.spin_bitrate,
            self.btn_apply,
        ):
            w.setEnabled(on)

    def _busy(self, on: bool, text: str) -> None:
        self._loading = on
        self.btn_load.setEnabled(not on)
        self.btn_apply.setEnabled(not on and self._current is not None)
        if on:
            self.lbl_status.setText(text)

    # ------------------------------------------------------------------ 桥回调
    def _on_slot(self, topic: str) -> None:
        if topic != Events.CAMERA_CFG:
            return
        res = self.bridge.latest(topic)
        if res is not None:
            self._on_result(res)

    def _on_result(self, res: Any) -> None:
        action = getattr(res, "action", "load")
        if not getattr(res, "ok", False):
            self._busy(False, "")
            self.lbl_status.setText(f"失败：{getattr(res, 'error', '未知原因')}")
            self.btn_load.setEnabled(True)
            return
        if action == "load":
            self._fill(res)
        else:
            applied = getattr(res, "applied", None)
            fixed = list(getattr(res, "fixed", ()) or ())
            if applied is not None:
                self._current = applied
            self._busy(False, "")
            self.btn_load.setEnabled(True)
            msg = (
                f"已应用：{applied.width}x{applied.height} "
                f"{applied.frame_rate}fps {applied.bitrate_kbps}kbps"
            )
            if fixed:
                msg += "（已修正：" + "；".join(fixed) + "）"
            self.lbl_status.setText(msg)

    # ------------------------------------------------------------------ 读取
    def _on_load(self) -> None:
        self._current = None
        self._options = None
        self._set_editable(False)
        stream = int(self.combo_stream.currentData())
        self._busy(True, "读取中……（经图传空口，可能要几秒）")
        ok = self.camera.load_async(stream)
        if not ok:
            self.lbl_status.setText("上一次操作还没结束，请稍候再点")
            self.btn_load.setEnabled(True)
            self._loading = False

    def _fill(self, res: Any) -> None:
        self._current = res.current
        self._options = res.options
        opt = res.options
        cur = res.current
        cur_enc = str(cur.encoding or "").strip() or "H264"
        encodings = [str(e) for e in opt.encodings if str(e).strip()]
        if not encodings:
            encodings = [cur_enc]
        elif cur_enc not in encodings:
            # 设备自报的当前值不在它自己给出的 Options 里（实机见过这种不一致）。
            # 不补进去，下拉框就会一项都选不中、显示成空白，用户无从下手。
            encodings.insert(0, cur_enc)
        self.combo_encoding.blockSignals(True)
        self.combo_encoding.clear()
        for enc in encodings:
            self.combo_encoding.addItem(enc)
        self._select(self.combo_encoding, cur_enc)
        self.combo_encoding.blockSignals(False)

        self._fill_resolutions(self.combo_encoding.currentText(), f"{cur.width}x{cur.height}")

        self.combo_fps.blockSignals(True)
        self.combo_fps.clear()
        for fps in opt.fps_values:
            self.combo_fps.addItem(str(fps), fps)
        self._select_data(self.combo_fps, int(cur.frame_rate))
        self.combo_fps.blockSignals(False)

        self.spin_bitrate.blockSignals(True)
        lo, hi = opt.bitrate_range
        self.spin_bitrate.setRange(int(lo), int(hi))
        self.spin_bitrate.setValue(int(cur.bitrate_kbps or lo))
        self.spin_bitrate.blockSignals(False)

        mode = "V" if str(cur.rate_mode).upper().startswith("V") else "C"
        idx = self.combo_rate.findData(mode)
        self.combo_rate.setCurrentIndex(idx if idx >= 0 else 0)

        self._busy(False, "")
        self._set_editable(True)
        self.btn_load.setEnabled(True)
        src = "设备实报值域" if getattr(opt, "source", "device") == "device" else "保守默认值域"
        self.lbl_status.setText(
            f"当前：{cur.width}x{cur.height} {cur.frame_rate}fps "
            f"{cur.bitrate_kbps}kbps {cur.encoding}（{src}）"
        )

    # ------------------------------------------------------------------ 编码联动
    def _fill_resolutions(self, encoding: str, keep: str) -> None:
        """按当前编码重填分辨率，尽量保住原选择，保不住就取面积最接近的一档。"""
        opt = self._options
        if opt is None:
            return
        combo = self.combo_resolution
        rows = opt.resolutions_for(encoding)
        combo.blockSignals(True)
        combo.clear()
        for w, h in rows:
            combo.addItem(f"{w}x{h}")
        i = combo.findText(keep)
        if i < 0 and rows and keep:
            ow, oh = _parse_px(keep)
            best = min(rows, key=lambda r: abs(r[0] * r[1] - ow * oh))
            i = combo.findText(f"{best[0]}x{best[1]}")
        if i >= 0:
            combo.setCurrentIndex(i)
        elif combo.count():
            combo.setCurrentIndex(0)
        combo.blockSignals(False)

    def _on_encoding_changed(self, encoding: str) -> None:
        if self._loading or not encoding:
            return
        self._fill_resolutions(str(encoding), self.combo_resolution.currentText())

    # ------------------------------------------------------------------ 应用
    def _on_apply(self) -> None:
        if self._current is None:
            return
        w, h = _parse_px(str(self.combo_resolution.currentText() or ""))
        self._busy(True, "写入中……（应用后自动重连取流）")
        st = EncoderSettings(
            encoder_token=self._current.encoder_token,
            name=self._current.name,
            encoding=str(self.combo_encoding.currentText() or "H264"),
            width=w,
            height=h,
            frame_rate=int(self.combo_fps.currentData() or 0),
            bitrate_kbps=int(self.spin_bitrate.value()),
            rate_mode=str(self.combo_rate.currentData() or "C"),
            quality=self._current.quality,
            gov_length=self._current.gov_length,
            # 编码档位与抽帧间隔界面上没给改，原样带回去，别让默认值把设备配置冲掉
            encoding_interval=int(self._current.encoding_interval or 0),
            codec_profile=str(self._current.codec_profile or "Main"),
        )
        ok = self.camera.apply_async(st)
        if not ok:
            self.lbl_status.setText("上一次操作还没结束，请稍候再点")
            self.btn_load.setEnabled(True)
            self._loading = False

    # ------------------------------------------------------------------ 小工具
    @staticmethod
    def _select(combo: QComboBox, text: str) -> None:
        i = combo.findText(text)
        if i >= 0:
            combo.setCurrentIndex(i)

    @staticmethod
    def _select_data(combo: QComboBox, value: int) -> None:
        i = combo.findData(value)
        if i >= 0:
            combo.setCurrentIndex(i)
        elif combo.count():
            combo.setCurrentIndex(combo.count() // 2)

    def maybe_autoload(self) -> None:
        """首次打开自动读一次；已有数据或在途就不再触发。"""
        if self._current is None and not self._loading:
            self._on_load()
