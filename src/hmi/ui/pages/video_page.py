"""视频页：取流显示、抓拍、录像、码流切换、链路角标与运动方向盘。

帧数据不过桥（见 ``ui.bridge`` 的拉取模型）：本页用自己的 QTimer 定时调
``session.video.frames.pull_next()``——一个节拍放一帧，按到达顺序播。无线
图传是突发交付（一批连到好几帧），若每个节拍只"看最新一眼"，显示 fps 会被
压成批次频率；放帧队列（``transport.base.FrameChannel``）把突发抹平成连续
播放，队列有界，积压过多时顶掉最旧帧，延迟不会失控。

QImage 转换有个经典坑：``QImage(arr.data, ...)`` 不持有 numpy 缓冲，数组一旦
被回收，画面就成了随机噪声甚至直接崩溃。本页的解法是紧接着
``QPixmap.fromImage(qimg)``——QPixmap 是深拷贝，numpy 数组只需活过这一次转换；
显示时按标签尺寸 KeepAspectRatio 缩放，原始画面始终留在 ``self._full_pm`` 里，
缩放窗口不损失清晰度。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.events import Events
from ...services.video import as_rgb_array
from ..widgets import CameraConfigDialog, MotionPad, TelemetryBar
from .base import BasePage

__all__ = ["VideoPage"]

_RECORD_STYLE = "QPushButton { background:#b3261e; color:white; font-weight:bold; }"

#: 键盘方向键 → 运动方向。页面拿到焦点即可用键盘开机器人，
#: 与方向盘按钮（pressed/released 信号）走同一条 press/release 通道。
_MOTION_KEYMAP = {
    Qt.Key_Up: "up",
    Qt.Key_Down: "down",
    Qt.Key_Left: "left",
    Qt.Key_Right: "right",
}


class VideoPage(BasePage):
    """中央画面 + 顶部工具条 + 底部链路/视频角标。"""

    page_key = "video"
    title = "视频"

    def __init__(self, session, bridge, parent: QWidget | None = None) -> None:
        super().__init__(session, bridge, parent)
        self._full_pm = QPixmap()
        self._frames_shown = 0
        self._dwell_ms = 0.0
        self._dwell_avg = 0.0
        self._camera_dialog: CameraConfigDialog | None = None

        self.btn_source = QPushButton("启动视频")
        self.btn_snapshot = QPushButton("抓拍")
        self.btn_record = QPushButton("录像")
        self.btn_stream = QPushButton("切换主码流")
        for btn, slot in (
            (self.btn_source, self._on_source_clicked),
            (self.btn_snapshot, self._on_snapshot_clicked),
            (self.btn_record, self._on_record_clicked),
            (self.btn_stream, self._on_stream_clicked),
        ):
            btn.clicked.connect(slot)

        # 取流传输层：tcp 稳妥，udp 延迟低但丢包可能花屏；切换即时生效并存盘。
        self.combo_transport = QComboBox()
        self.combo_transport.addItem("TCP", "tcp")
        self.combo_transport.addItem("UDP", "udp")
        cur = str(getattr(session.cfg.video, "transport", "tcp") or "tcp").lower()
        idx = self.combo_transport.findData(cur)
        self.combo_transport.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo_transport.setToolTip(
            "RTSP 取流传输：TCP 稳妥抗丢包；UDP 延迟更低，图传丢包时可能花屏"
        )
        self.combo_transport.activated.connect(self._on_transport_selected)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(self.btn_source)
        bar.addWidget(self.btn_snapshot)
        bar.addWidget(self.btn_record)
        bar.addWidget(self.btn_stream)
        bar.addWidget(QLabel("传输"))
        bar.addWidget(self.combo_transport)
        self.btn_camera = QPushButton("摄像头参数…")
        self.btn_camera.setToolTip("远程读取/修改编码分辨率、帧率、码率（ONVIF，按 IP 直连）")
        self.btn_camera.clicked.connect(self._on_camera_clicked)
        bar.addWidget(self.btn_camera)
        bar.addStretch(1)

        self.video_label = QLabel("等待视频帧……")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setStyleSheet("background:#10151c; color:#5c6672;")
        self.video_label.setMinimumSize(320, 180)

        self.info_label = QLabel("视频：等待指标 | 信道：暂无遥测")
        self.info_label.setStyleSheet("color:#3d4a5c; padding:2px 4px;")
        self.info_label.setWordWrap(True)

        self.telemetry_bar = TelemetryBar(session.cfg.telemetry)
        self.telemetry_bar.exported.connect(self._on_export_telemetry)

        self.motion_pad = MotionPad(session.cfg.motion)
        self.motion_pad.pressed.connect(self._on_motion_press)
        self.motion_pad.released.connect(self._on_motion_release)
        self.motion_pad.limits_changed.connect(self._on_motion_limits)

        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        bottom.addWidget(self.telemetry_bar, 1)
        bottom.addWidget(self.motion_pad, 0, Qt.AlignTop)

        box = QVBoxLayout(self)
        box.setContentsMargins(8, 8, 8, 4)
        box.setSpacing(6)
        box.addLayout(bar)
        box.addWidget(self.video_label, 1)
        box.addWidget(self.info_label)
        box.addLayout(bottom, 0)

        # 方向键控车的前提：页面自己能接键盘。方向盘按钮是 NoFocus，
        # 点按钮不会把焦点从这里抢走。
        self.setFocusPolicy(Qt.StrongFocus)

        limit = float(getattr(session.cfg.video, "render_fps_limit", 25.0) or 25.0)
        self._timer = QTimer(self)
        self._timer.setInterval(max(20, int(round(1000.0 / max(1.0, limit)))))
        self._timer.timeout.connect(self._poll_frames)

        self._sync_buttons()
        self._sync_motion()
        if self.service("video") is None:
            self.video_label.setText("未装配视频服务（--no-video）")

    # ------------------------------------------------------------------ 生命周期
    def on_show(self) -> None:
        telemetry = self.service("telemetry")
        if telemetry is not None:
            self.telemetry_bar.prefill(
                telemetry.history(),
                stale=telemetry.is_stale,
                age_s=telemetry.age_s(),
            )
        if self.service("video") is not None:
            self.service("video").frames.set_watching(True)
            self._timer.start()
            self._poll_frames()
        motion = self.service("motion")
        if motion is not None and self.session.cfg.motion.enabled:
            self.motion_pad.refresh(motion.snapshot())
            self.setFocus()

    def on_hide(self) -> None:
        self._timer.stop()
        self._motion_estop()
        video = self.service("video")
        if video is not None:
            video.frames.set_watching(False)

    def on_close(self) -> None:
        self._timer.stop()
        self._motion_estop()
        video = self.service("video")
        if video is not None:
            video.frames.set_watching(False)

    # ------------------------------------------------------------------ 键盘控车
    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        direction = _MOTION_KEYMAP.get(event.key())
        if direction is not None:
            # 长按的方向键会自动重复——按住一次就该是一次 press，
            # 重复事件直接吞掉，否则松手后服务里还挂着没清的方向。
            if not event.isAutoRepeat():
                self._on_motion_press(direction)
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        direction = _MOTION_KEYMAP.get(event.key())
        if direction is not None:
            # 与 keyPressEvent 对称：系统长按会在两次重复"按下"之间塞进重复"释放"
            # （isAutoRepeat=True），这些中间释放绝不能当成松手，否则按住时目标
            # 会被清零、速度"递增又回零"，而重复的"按下"又被上面吞掉再也回不来。
            # 只有真正松手那一次（isAutoRepeat=False）才释放方向。
            if not event.isAutoRepeat():
                self._on_motion_release(direction)
            event.accept()
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().focusOutEvent(event)
        self._motion_estop()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._rescale()

    # ------------------------------------------------------------------ 帧拉取
    def _poll_frames(self) -> None:
        video = self.service("video")
        if video is None:
            return
        hit = video.frames.pull_next()
        if hit is None:
            return
        image, _meta, dwell = hit
        self._dwell_ms = dwell
        if self._frames_shown:
            self._dwell_avg = self._dwell_avg * 0.8 + dwell * 0.2
        else:
            self._dwell_avg = dwell
        self._show_frame(image)
        self._frames_shown += 1
        self._refresh_info()

    def _show_frame(self, image) -> None:
        arr = np.ascontiguousarray(as_rgb_array(image))
        height, width = int(arr.shape[0]), int(arr.shape[1])
        qimg = QImage(arr.data, width, height, width * 3, QImage.Format_RGB888)
        self._full_pm = QPixmap.fromImage(qimg)
        self.video_label.setText("")
        self._rescale()

    def _rescale(self) -> None:
        if self._full_pm.isNull():
            return
        size = self.video_label.size()
        if size.width() < 2 or size.height() < 2:
            return
        self.video_label.setPixmap(
            self._full_pm.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

    # ------------------------------------------------------------------ 工具条
    def _on_source_clicked(self) -> None:
        video = self.service("video")
        if video is None:
            self.say("未装配视频服务，无法取流")
            return
        if video.running:
            video.stop()
            self.say("视频取流已停止")
        elif video.start():
            self.say("视频取流已启动")
        else:
            self.say(f"视频启动失败：{video.info().get('start_error') or '未知原因'}", 8000)
        self._sync_buttons()

    def _on_snapshot_clicked(self) -> None:
        res = self._capture()
        if res.get("ok"):
            self.say(f"抓拍已保存：{res.get('path')}", 8000)
        else:
            self.say(f"抓拍失败：{res.get('detail') or '未知原因'}", 8000)

    def _capture(self):
        record = self.service("record")
        if record is not None:
            return record.capture()
        video = self.service("video")
        if video is None:
            return {"ok": False, "detail": "未装配视频服务"}
        return video.snapshot()

    def _on_record_clicked(self) -> None:
        record = self.service("record")
        if record is None:
            self.say("未装配录像服务")
            return
        if record.recording:
            res = record.stop_video()
            self.say(
                f"录像已停止：{res.get('path') or ''}（{res.get('frames', 0)} 帧）",
                8000,
            )
        else:
            res = record.start_video()
            if res.get("ok"):
                self.say(f"开始录像：{res.get('path')}", 8000)
            else:
                self.say(f"录像启动失败：{res.get('detail')}", 8000)
        self._sync_buttons()

    def _on_stream_clicked(self) -> None:
        video = self.service("video")
        if video is None:
            self.say("未装配视频服务")
            return
        res = video.toggle_stream()
        self.say(f"码流切换：{res.get('detail')}", 8000)
        self._sync_buttons()
        self._refresh_info()

    def _on_transport_selected(self, index: int) -> None:
        video = self.service("video")
        if video is None:
            self.say("未装配视频服务，传输切换不生效")
            return
        mode = str(self.combo_transport.itemData(index))
        res = video.set_transport(mode)
        self.say(f"传输：{res.get('detail')}", 8000)
        self._sync_buttons()

    def _on_camera_clicked(self) -> None:
        camera = self.service("camera")
        if camera is None:
            self.say("未装配摄像头配置服务")
            return
        if self._camera_dialog is None:
            self._camera_dialog = CameraConfigDialog(
                camera,
                self.bridge,
                stream_index=int(self.session.cfg.video.stream_index),
                parent=self,
            )
        self._camera_dialog.show()
        self._camera_dialog.raise_()
        self._camera_dialog.maybe_autoload()

    def _sync_buttons(self) -> None:
        video = self.service("video")
        record = self.service("record")
        self.btn_source.setEnabled(video is not None)
        self.btn_stream.setEnabled(video is not None)
        self.btn_snapshot.setEnabled(video is not None)
        self.btn_record.setEnabled(record is not None)
        self.combo_transport.setEnabled(video is not None)
        if video is None:
            return
        self.btn_source.setText("停止视频" if video.running else "启动视频")
        idx = int(video.stream_index)
        self.btn_stream.setText("切换主码流" if idx == 1 else "切换子码流")
        recording = bool(record.recording) if record is not None else False
        self.btn_record.setText("停止录像" if recording else "录像")
        self.btn_record.setStyleSheet(_RECORD_STYLE if recording else "")

    # ------------------------------------------------------------------ 运动控制
    def _sync_motion(self) -> None:
        """按装配/启用状态决定方向盘可用性，禁用原因直接写在读数位。"""
        motion = self.service("motion")
        if motion is None:
            self.motion_pad.set_unavailable("未装配运动服务")
        elif not self.session.cfg.motion.enabled:
            self.motion_pad.set_unavailable("运动控制已禁用")
        else:
            self.motion_pad.setEnabled(True)
            self.motion_pad.refresh(motion.snapshot())

    def _on_motion_press(self, direction: str) -> None:
        motion = self.service("motion")
        if motion is not None and motion.press(direction):
            self.motion_pad.refresh(motion.snapshot())

    def _on_motion_release(self, direction: str) -> None:
        motion = self.service("motion")
        if motion is not None:
            motion.release(direction)
            self.motion_pad.refresh(motion.snapshot())

    def _on_motion_limits(self, vx: float, vr: float) -> None:
        """限速写回 cfg：服务每拍动态读 cfg，下一次按键即生效；保存失败只降级。"""
        m = self.session.cfg.motion
        m.max_linear_speed = vx
        m.max_angular_speed = vr
        try:
            self.session.cfg.save()
            self.say(f"限速已设置并保存：vx {vx:.2f} m/s · vr {vr:.2f} rad/s", 6000)
        except OSError as exc:
            self.log.warning("写回限速配置失败（本次仅内存生效）：%s", exc)
            self.say(f"限速已生效（仅本次会话）：写回 app.yaml 失败 - {exc}", 8000)

    def _motion_estop(self) -> None:
        """切页/失焦安全网：只在真在动（或还按着）时急停，避免空发停止帧。"""
        motion = self.service("motion")
        if motion is None or not self.session.cfg.motion.stop_on_focus_loss:
            return
        st = motion.snapshot()
        if st["held"] or st["vx"] or st["vr"] or st["active"]:
            motion.emergency_stop()
            self.motion_pad.refresh(motion.snapshot())

    # ------------------------------------------------------------------ 桥刷新
    def on_slot(self, topic: str) -> None:
        if topic == Events.TRANSPORT_STATE:
            self._sync_buttons()
        if topic == Events.TELEMETRY_SAMPLE:
            self.telemetry_bar.refresh(self.bridge.latest(topic))
        if topic == Events.MOTION_STATE:
            self.motion_pad.refresh(self.bridge.latest(topic))
        self._refresh_info()

    def _on_export_telemetry(self) -> None:
        telemetry = self.service("telemetry")
        if telemetry is None:
            self.say("未装配遥测服务，无法导出")
            return
        rows = telemetry.flush_csv()
        src = str(telemetry.summary().get("csv") or "")
        if not src or not Path(src).exists():
            self.say(f"暂无可导出的遥测文件（内存中 {rows} 行）", 8000)
            return
        target, _ = QFileDialog.getSaveFileName(
            self, "导出遥测记录", str(Path.cwd() / Path(src).name), "CSV 文件 (*.csv)"
        )
        if not target:
            return
        try:
            shutil.copyfile(src, target)
        except OSError as exc:
            self.say(f"导出失败：{exc}", 8000)
            return
        self.say(f"遥测记录已导出：{target}", 8000)

    def _refresh_info(self) -> None:
        parts: list[str] = []
        st = self.bridge.latest(Events.VIDEO_STATS)
        if st is None:
            parts.append("视频：等待指标")
        elif st.connected:
            drop_part = f"丢帧{st.frames_dropped}"
            skipped = getattr(st, "frames_skipped", 0)
            if skipped:
                drop_part += f" 跳帧{skipped}"
            merged = getattr(st, "frames_merged", 0)
            if merged:
                drop_part += f" 合并{merged}"
            parts.append(
                f"视频 {st.resolution or '?'} {st.codec or '?'} "
                f"{st.fps_decode:.1f}fps {drop_part} 重连{st.reconnects}"
            )
        else:
            parts.append("视频未连接")
        link = self.bridge.latest(Events.LINK_SNAPSHOT)
        if link is None:
            parts.append("信道：暂无遥测")
        else:
            link_state = "链路正常" if link.connected else "链路未连接"
            if link.degraded:
                link_state = "链路降级"
            rssi = f"{link.rssi_dbm:.0f}dBm" if link.rssi_dbm is not None else "RSSI?"
            evm = f"{link.evm_db:.1f}dB" if link.evm_db is not None else "EVM?"
            freq = f"{link.freq_mhz:.1f}MHz" if link.freq_mhz is not None else "频点?"
            parts.append(f"信道 {rssi} {evm} {freq} {link_state}")
        if self._frames_shown:
            parts.append(f"显示{self._frames_shown}帧 停留{self._dwell_avg:.0f}ms")
        self.info_label.setText(" | ".join(parts))
