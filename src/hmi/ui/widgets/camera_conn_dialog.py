"""摄像头连接对话框：IP / 端口 / 账号密码 / 主·子码流路径 / 取流传输。

这些东西 ONVIF 探不出来（实机 TP-Link 不实现 GetStreamUri，密码更不可能自动
拿到），只能在界面上给一个入口手填。填完先点【测试】——RTSP 走明文
OPTIONS/DESCRIBE、ONVIF 走 GetDeviceInformation，两条独立判定，失败时服务层
会回一句"是 IP 不通 / 端口不对 / 密码错 / 路径不存在"，不至于让人瞎猜。

改动只作用在 ``VideoCfg`` 的副本上：点【保存】才写回 app.yaml 并让取流按新
地址重连，关掉窗口就等于没改过。
"""

from __future__ import annotations

import copy
from typing import Any

from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core.config import VideoCfg
from ...core.events import Events

__all__ = ["CameraConnDialog"]

#: 界面只认主/子两档（配置校验把 stream_index 限定在 0/1），路径表也就固定两格。
_STREAM_LABELS = ("主码流", "子码流")


class CameraConnDialog(QDialog):
    """编辑 ``cfg.video`` 的连接子集；【测试】异步、【保存】同步落盘。"""

    def __init__(
        self,
        camera_service: Any,
        bridge: Any,
        parent: QWidget | None = None,
        on_saved: Any = None,
    ) -> None:
        super().__init__(parent)
        self.camera = camera_service
        self.bridge = bridge
        #: 保存成功后回调（页面用它同步工具条上的传输下拉等旁路状态）。
        self.on_saved = on_saved
        self.setWindowTitle("摄像头连接（IP / 账号 / 推流地址）")
        self.setModal(False)

        base = camera_service.cfg.video
        # 副本：窗口里怎么改都不动真实配置，【保存】才回写。
        self._base = copy.deepcopy(base)
        self._probing = False

        self.edit_ip = QLineEdit()
        self.edit_ip.setPlaceholderText("192.168.1.60")

        self.spin_rtsp = QSpinBox()
        self.spin_rtsp.setRange(1, 65535)
        self.spin_rtsp.setToolTip("RTSP 取流端口，绝大多数摄像头是 554")
        self.spin_onvif = QSpinBox()
        self.spin_onvif.setRange(1, 65535)
        self.spin_onvif.setToolTip("ONVIF 服务端口，用于读改编码参数（常见 80 / 2000 / 8000）")

        self.edit_user = QLineEdit()
        self.edit_pwd = QLineEdit()
        self.edit_pwd.setEchoMode(QLineEdit.Password)
        self.chk_pwd = QCheckBox("显示")
        self.chk_pwd.toggled.connect(
            lambda on: self.edit_pwd.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)
        )
        pwd_row = QHBoxLayout()
        pwd_row.addWidget(self.edit_pwd, 1)
        pwd_row.addWidget(self.chk_pwd)

        self.edit_paths = (QLineEdit(), QLineEdit())
        for i, w in enumerate(self.edit_paths):
            w.setPlaceholderText("stream1" if i == 0 else "stream2")
            w.setToolTip(
                f"{_STREAM_LABELS[i]}的 RTSP 路径（地址最后一段，不带斜杠）；"
                "留空表示不换路径"
            )

        self.combo_stream = QComboBox()
        for i, label in enumerate(_STREAM_LABELS):
            self.combo_stream.addItem(label, i)
        self.combo_stream.setToolTip("预览/自检用哪一档；保存后取流就切到这档")

        self.combo_transport = QComboBox()
        self.combo_transport.addItem("TCP（稳妥）", "tcp")
        self.combo_transport.addItem("UDP（低延迟）", "udp")
        self.combo_transport.setToolTip("UDP 延迟低，但图传空口丢包时会花屏")

        self.edit_url = QLineEdit()
        self.edit_url.setPlaceholderText("留空 = 按上面的 IP/端口/路径自动拼装")
        self.edit_url.setToolTip(
            "只有厂商私有地址格式（自动拼装拼不出来）才需要手填。\n"
            "手填后仍可换主/子码流：地址里写 {path} 占位符，"
            "或让末段正好等于主码流路径。"
        )

        self.lbl_preview = QLineEdit()
        self.lbl_preview.setReadOnly(True)
        self.lbl_preview.setStyleSheet("font-family:Consolas,monospace;background:#f4f6f9;")

        ports = QHBoxLayout()
        ports.addWidget(self.spin_rtsp)
        ports.addWidget(QLabel("/"))
        ports.addWidget(self.spin_onvif)
        ports.addStretch(1)

        self.form = QFormLayout()
        self.form.addRow("摄像头 IP", self.edit_ip)
        self.form.addRow("RTSP / ONVIF 端口", ports)
        self.form.addRow("账号", self.edit_user)
        self.form.addRow("密码", pwd_row)
        self.form.addRow("主码流路径", self.edit_paths[0])
        self.form.addRow("子码流路径", self.edit_paths[1])
        self.form.addRow("预览码流", self.combo_stream)
        self.form.addRow("取流传输", self.combo_transport)
        self.form.addRow("手填取流地址", self.edit_url)
        self.form.addRow("生效地址", self.lbl_preview)

        self.btn_test = QPushButton("测试")
        self.btn_test.clicked.connect(self._on_test)
        self.btn_save = QPushButton("保存")
        self.btn_save.clicked.connect(self._on_save)
        self.btn_revert = QPushButton("恢复当前配置")
        self.btn_revert.clicked.connect(self._load_widgets)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.close)
        row = QHBoxLayout()
        row.addWidget(self.btn_test)
        row.addWidget(self.btn_revert)
        row.addStretch(1)
        row.addWidget(self.btn_save)
        row.addWidget(self.btn_close)

        self.lbl_status = QLabel(
            "填完点【测试】：先看能不能取到流，再看能不能改参数；【保存】才写进配置文件。"
        )
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet("color:#3d4a5c;")

        box = QVBoxLayout(self)
        box.addLayout(self.form)
        box.addLayout(row)
        box.addWidget(self.lbl_status)

        for w in (
            self.edit_ip,
            self.edit_user,
            self.edit_pwd,
            self.edit_url,
            *self.edit_paths,
        ):
            w.textChanged.connect(self._refresh_preview)
        self.spin_rtsp.valueChanged.connect(self._refresh_preview)
        self.combo_stream.currentIndexChanged.connect(self._refresh_preview)

        bridge.slot_updated.connect(self._on_slot)
        self._load_widgets()

    # ------------------------------------------------------------------ 控件 <-> 副本
    def _load_widgets(self) -> None:
        """把当前真实配置灌进控件（也用于【恢复当前配置】丢弃改动）。

        读的是 ``camera.cfg.video`` 而不是构造时的快照：配置可能被命令行改过，
        也可能刚被另一个对话框写过，"当前配置"必须以真实对象为准。副本仍留一份
        深拷贝作基线，避免直接持有真实对象后被 ``_snapshot`` 改脏。
        """
        self._base = copy.deepcopy(self.camera.cfg.video)
        v = self._base
        self.edit_ip.setText(str(v.camera_ip or ""))
        self.spin_rtsp.setValue(int(v.rtsp_port or 554))
        self.spin_onvif.setValue(int(v.onvif_port or 80))
        self.edit_user.setText(str(v.user or ""))
        self.edit_pwd.setText(str(v.password or ""))
        paths = [str(p) for p in (v.stream_paths or [])][:2]
        for i, w in enumerate(self.edit_paths):
            w.blockSignals(True)
            w.setText(paths[i] if i < len(paths) else "")
            w.blockSignals(False)
        idx = self.combo_stream.findData(min(max(int(v.stream_index), 0), 1))
        self.combo_stream.setCurrentIndex(idx if idx >= 0 else 0)
        t = self.combo_transport.findData(str(v.transport or "tcp").lower())
        self.combo_transport.setCurrentIndex(t if t >= 0 else 0)
        self.edit_url.setText(str(v.url or ""))
        self._refresh_preview()

    def _snapshot(self) -> VideoCfg:
        """控件 → ``VideoCfg`` 副本（不落盘，只是待测试/待保存的候选）。"""
        snap = copy.deepcopy(self._base)
        snap.camera_ip = self.edit_ip.text().strip()
        snap.rtsp_port = int(self.spin_rtsp.value())
        snap.onvif_port = int(self.spin_onvif.value())
        snap.user = self.edit_user.text().strip()
        snap.password = self.edit_pwd.text()
        snap.stream_paths = [p.text().strip().strip("/") for p in self.edit_paths if p.text().strip()]
        snap.stream_index = int(self.combo_stream.currentData())
        snap.transport = str(self.combo_transport.currentData() or "tcp")
        snap.url = self.edit_url.text().strip()
        return snap

    def _refresh_preview(self) -> None:
        snap = self._snapshot()
        idx = int(snap.stream_index)
        manual = bool(snap.url)
        url = snap.resolved_url(idx)
        self.lbl_preview.setText(url)
        self.lbl_preview.setToolTip(
            ("手填地址生效（末段能对上主码流路径时仍可换档）" if manual else "按 IP/端口/路径自动拼装")
            + f"\n当前预览：{_STREAM_LABELS[min(idx, 1)]}"
        )

    # ------------------------------------------------------------------ 桥回调
    def _on_slot(self, topic: str) -> None:
        if topic != Events.CAMERA_PROBE:
            return
        res = self.bridge.latest(topic)
        if res is not None:
            self._on_probe(res)

    def _on_probe(self, res: Any) -> None:
        self._probing = False
        self.btn_test.setEnabled(True)
        self.btn_test.setText("测试")
        rtsp = "√ " if getattr(res, "rtsp_ok", False) else "× "
        onvif = "√ " if getattr(res, "onvif_ok", False) else "× "
        self.lbl_status.setText(
            f"{rtsp}取流：{getattr(res, 'rtsp_detail', '') or '未测'}\n"
            f"{onvif}ONVIF：{getattr(res, 'onvif_detail', '') or '未测'}"
        )
        color = "#1d7a3d" if getattr(res, "ok", False) else "#b23b3b"
        self.lbl_status.setStyleSheet(f"color:{color};")

    # ------------------------------------------------------------------ 动作
    def _on_test(self) -> None:
        if self._probing:
            self.lbl_status.setText("自检还在进行中（要穿图传空口），稍等一下")
            return
        snap = self._snapshot()
        if not snap.camera_ip and not snap.url:
            self.lbl_status.setText("摄像头 IP 和取流地址都是空的：先填 IP")
            return
        self._probing = self.camera.probe_async(snap, int(snap.stream_index))
        if self._probing:
            self.btn_test.setEnabled(False)
            self.btn_test.setText("自检中…")
            self.lbl_status.setStyleSheet("color:#3d4a5c;")
            self.lbl_status.setText(
                f"正在探测 {self.lbl_preview.text()} ……"
            )
        else:
            self.lbl_status.setText("上一次自检还没结束，请稍候再点")

    def _on_save(self) -> None:
        res = self.camera.apply_connection(self._snapshot())
        detail = str(res.get("detail") or "")
        if res.get("ok"):
            self._base = copy.deepcopy(self.camera.cfg.video)
            self.lbl_status.setStyleSheet("color:#1d7a3d;")
            self._refresh_preview()
            if callable(self.on_saved):
                self.on_saved()
        else:
            self.lbl_status.setStyleSheet("color:#b23b3b;")
        self.lbl_status.setText("保存结果：" + detail)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """每次打开都把控件对齐到最新真实配置，避免上次未保存的残值误导。"""
        super().showEvent(event)
        if not self.edit_ip.text().strip() and not self.edit_url.text().strip():
            self._load_widgets()
