"""配置 schema 与加载：所有可调参数集中在一个 YAML 里。

设计要点：
- 每层只读自己那一段（video/serial/at/link/passthrough），互不越界；
- 未知字段只告警不报错，方便手册与旧版本混用；
- 环境变量可覆盖关键项，便于部署脚本与自检注入。
"""

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from .utils import parse_bool, safe_int


def project_root(start: Path | None = None) -> Path:
    """向上查找带 src/ 标记的目录作为项目根。

    打包版（PyInstaller 冻结）里 ``__file__`` 指向 ``_internal``，一路往上走可能撞到
    别处恰好存在的 ``src/hmi``（构建机上就会），日志和流水于是落到交付目录之外。
    所以入口在启动时把 ``EWD611_HOME`` 钉成 exe 所在目录，这里优先认它。
    """
    pinned = os.environ.get("EWD611_HOME")
    if pinned:
        home = Path(pinned)
        if home.is_dir():
            return home
    here = (start or Path(__file__).resolve()).parent
    for cand in [here, *here.parents, Path.cwd()]:
        if (cand / "src" / "hmi").is_dir():
            return cand
    return Path.cwd()


@dataclass
class AppCfg:
    mock: bool = False
    log_level: str = "INFO"
    log_dir: str = "logs"
    data_dir: str = "data"
    title: str = "EWD611 数传图传上位机控制台"
    templates_file: str = "config/templates.yaml"


@dataclass
class VideoCfg:
    """RTSP 取流参数，默认子码流 704x576（用户确认）。"""

    camera_ip: str = "192.168.1.10"
    rtsp_port: int = 554
    onvif_port: int = 80
    user: str = "admin"
    password: str = ""
    channel: int = 1
    stream_index: int = 1
    url: str = ""
    url_template: str = (
        "rtsp://{ip}:{port}/user={user}&password={password}&channel={channel}&stream={stream}.sdp?"
    )
    # 按码流序号排列的 RTSP 路径（0=主码流、1=子码流）。手填 url 时 ONVIF 探不到
    # 推流地址（TP-Link 不实现 GetStreamUri），主/子码流就得靠这张表换路径；
    # 留空就退化为"地址整条不动"，行为和加这个字段之前一致。
    stream_paths: list[str] = field(default_factory=list)
    transport: str = "tcp"
    auto_connect: bool = True
    auto_reconnect: bool = True
    reconnect_interval_s: float = 3.0
    max_reconnects: int = 0
    open_timeout_us: int = 5_000_000
    decode_threads: int = 2
    render_fps_limit: float = 25.0
    drop_late_frames: bool = True
    keep_aspect: bool = True
    show_osd: bool = True
    buffer_ms: int = 300
    pacing_frames: int = 5
    max_dwell_ms: float = 120.0

    def _stream_path(self, stream_index: int) -> str:
        """按码流序号取 RTSP 路径（越界取最后一档）；stream_paths 没配就返回空串。"""
        paths = [str(p).strip().strip("/") for p in self.stream_paths if str(p).strip()]
        if not paths:
            return ""
        return paths[min(max(int(stream_index), 0), len(paths) - 1)]

    def composed_url(self, stream_index: int | None = None) -> str:
        """按 IP / RTSP 端口 / 账号 / 码流路径拼一条 ``rtsp://账号:密码@IP:端口/路径``。

        账号段只在"用户名和密码都填了"时才带：``rtsp://admin:@ip`` 这种空密码
        只会让设备回 401，界面反倒看不出是密码没填。密码里的 ``@`` ``:``
        必须转义，否则 URL 解析把域名切错，设备报 404 时查不到是密码的锅。
        拼不出来（没 IP 或没路径）返回空串，交给调用方决定怎么兜底。
        """
        idx = self.stream_index if stream_index is None else int(stream_index)
        ip = str(self.camera_ip or "").strip()
        path = self._stream_path(idx)
        if not ip or not path:
            return ""
        auth = ""
        if str(self.user or "").strip() and str(self.password or ""):
            auth = "%s:%s@" % (
                urllib.parse.quote(str(self.user).strip(), safe=""),
                urllib.parse.quote(str(self.password), safe=""),
            )
        return f"rtsp://{auth}{ip}:{int(self.rtsp_port or 554)}/{path}"

    def resolved_url(self, stream_index: int | None = None) -> str:
        """取流地址：显式 url 优先；留空则用 url_template 拼装（传输层也走这里）。

        主/子码流换档按四种情形依次尝试：
        1. 地址里有 ``{path}`` 占位符 —— 直接用 stream_paths 对应路径替换；
        2. 地址里有 ``stream=N`` —— 改数字（雄迈那种一条 URL 走天下的写法）；
        3. 地址末段正好等于 stream_paths 的第一档 —— 按末段换路径（TP-Link
           ``/stream1`` ``/stream2`` 这类，手填地址时 ONVIF 探不到 StreamUri）；
        4. 都不满足 —— 整条地址原样返回，和没加 stream_paths 之前行为一致。

        ``url`` 留空但 ``stream_paths`` 填了、模板里又没有 ``{path}`` 时，直接按
        ``composed_url`` 拼装：用户在连接对话框里填的就是 IP / 端口 / 账号 / 路径，
        自检和取流必须用同一条地址，不然"测通了却没画面"这种鬼故事就没法解释。
        """
        idx = self.stream_index if stream_index is None else int(stream_index)
        path = self._stream_path(idx)
        base = self.url.strip()
        if base:
            if "{path}" in base:
                return base.replace("{path}", path) if path else base
            if "stream=" in base:
                return re.sub(r"stream=\d+", f"stream={idx}", base, count=1)
            if path:
                head, sep, tail = base.rstrip("/").rpartition("/")
                if sep and tail == self._stream_path(0):
                    return f"{head}/{path}"
            return base
        if path and "{path}" not in self.url_template:
            composed = self.composed_url(idx)
            if composed:
                return composed
        return self.url_template.format(
            user=self.user,
            password=self.password,
            ip=self.camera_ip,
            port=self.rtsp_port,
            channel=self.channel,
            stream=idx,
            path=path,
        )


@dataclass
class SerialCfg:
    """485 转 USB 串口；模块侧 RS485 固定 115200 8N1，不可改。"""

    port: str = ""
    auto_detect: bool = True
    preferred_vids: list[str] = field(default_factory=lambda: ["1A86", "10C4", "2341"])
    baudrate: int = 115200
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    rtsCTS: bool = False
    read_timeout_ms: int = 20
    rx_idle_gap_ms: int = 30
    auto_reconnect: bool = True
    exclusive_fallback: bool = True


@dataclass
class AtCfg:
    """AT 配置通道（网口 UDP 是唯一配置入口，手册第六章 6.2）。

    默认走网口 UDP（用户确认："AT 参数读取默认通过网络进行"）；
    协议为 255.255.255.255:56789 广播上的二进制成帧 AT（抓包实锤，
    复刻厂商 netat.exe）：先 op1 发现握手拿到模块 MAC，AT 帧再定向
    该模块。裸 ASCII 广播会被模块直接丢弃。
    模块的串口/485 是纯透传通道——向其发送 AT 会被原样透传到远端
    设备，本机电台不会解析，所以 fallback_channel 默认禁用（空）。
    填 serial 仅供明确知晓风险的用户显式开启（如本地调试假电台）。
    """

    channel: str = "udp"
    fallback_channel: str = ""
    line_ending: str = "crlf"
    timeout_ms: int = 1500
    #: 只重试 1 次：超时类失败最坏耗时 = timeout_ms ×(1+retries)。原来 2 次重试
    #: 让一条读不上的参数堵住 AT 锁 4.5s，界面上"点了没反应"就是这么来的。
    retries: int = 1
    ok_tokens: list[str] = field(default_factory=lambda: ["OK"])
    err_tokens: list[str] = field(default_factory=lambda: ["ERROR", "ERR", "FAIL"])
    pre_silence_ms: int = 20
    wake_gap_ms: int = 300
    wake_sequence: str = ""
    echo_suppress: bool = True
    pause_passthrough: bool = True
    local_role: str = "ap"
    remote_role: str = "sta"


@dataclass
class UdpAtCfg:
    """网口 AT 成帧协议参数（用户抓包实锤，复刻厂商 netat.exe 行为）。

    模块没有 IP、只认 255.255.255.255 广播，收发双向同端口（默认 56789）。
    UDP 载荷 = 15 字节二进制头（op|00|len|dstMAC|srcMAC）+ ASCII。
    broadcast_scan 开启时按 discovery_interval_ms 周期性发 op1 发现保活
    （netat 实测约 500ms 一次且全程不停），AT 帧自动定向最新发现的模块；
    target_mac 填了则免握手直接定向该模块（多模块场景指定其一）。
    host_id 是本机在协议头里的 6 字节标识（不等于网卡 MAC），留空则
    每次运行随机自选；固定它可让模块侧日志/抓包对照更稳定。
    """

    local_bind_ip: str = ""
    target_port: int = 56789
    local_port: int = 56789
    probe_timeout_ms: int = 900
    broadcast_scan: bool = True
    host_id: str = ""
    target_mac: str = ""
    discovery_interval_ms: int = 500


@dataclass
class LinkCfg:
    """信道遥测轮询与阈值。"""

    auto_poll: bool = True
    poll_interval_s: float = 1.0
    #: 首轮全量读取后，后续轮询只读 volatile 参数（RSSI 等）+ 上轮失败项；
    #: 置 False 则每轮都按 read_order 全量读（老行为）。
    volatile_after_first: bool = True
    history_minutes: float = 30
    read_order: list[str] = field(
        default_factory=lambda: [
            "MODE",
            "CONN_STATE",
            "RSSI",
            "BSS_BW",
            "FREQ_RANGE",
            "CHAN_LIST",
            "TXPOWER",
            "TX_MCS",
            "ACKTMO",
            "HEART_INT",
            "SSID",
            "KEYMGMT",
        ]
    )
    ap_extra: list[str] = field(default_factory=lambda: ["WNBCFG", "STA_INFO"])
    rssi_warn: float = -85.0
    rssi_crit: float = -92.0
    evm_warn: float = -20.0
    loss_warn_percent: float = 5.0
    stale_after_s: float = 12.0


@dataclass
class PassthroughCfg:
    """485 透传控制台默认行为。

    ``channel`` 决定透传字节走哪条线：默认 ``serial``——AT 走网口、控制指令走
    485 转 USB 串口，两条线物理独立、互不阻塞（用户实锤需求）。填 ``same``
    或与 ``at.channel`` 相同值时回到共线模式：共用一条字节通道，AT 事务窗口内
    透传帧会被半双工守卫拒发。
    """

    #: 透传所属字节通道：serial / udp / mock / same（跟随 AT 线）
    channel: str = "serial"
    mode: str = "hex"
    suffix: str = "none"
    echo_tx: bool = True
    reply_timeout_ms: int = 800
    rx_idle_gap_ms: int = 30
    max_records: int = 2000
    auto_scroll: bool = True
    default_group: str = "默认"
    show_hex: bool = True
    show_ascii: bool = True


@dataclass
class TelemetryFrameCfg:
    #: 上行帧协议："wheeltec"（底盘默认）/ "binary" / "ascii"（旧占位格式）
    protocol: str = "wheeltec"
    header_hex: str = "AA 55"


@dataclass
class TelemetryBatteryCfg:
    low_voltage_v: float = 22.0


@dataclass
class TelemetryCurveCfg:
    window_s: float = 60.0
    series: list[str] = field(default_factory=lambda: ["voltage", "vx"])


@dataclass
class TelemetryMockCfg:
    #: "auto" 跟随 app.mock；也可写 "true"/"false" 强制开/关
    enabled: str = "auto"


@dataclass
class TelemetryCfg:
    """机器人遥测状态栏（见 docs/视频页遥测状态栏方案.html）。"""

    enabled: bool = True
    log_csv: bool = True
    stale_after_s: float = 3.0
    display_throttle_hz: float = 2.0
    frame: TelemetryFrameCfg = field(default_factory=TelemetryFrameCfg)
    status_map: dict = field(
        default_factory=lambda: {0: "待机", 1: "自动驾驶", 2: "遥控", 3: "急停"}
    )
    battery: TelemetryBatteryCfg = field(default_factory=TelemetryBatteryCfg)
    curve: TelemetryCurveCfg = field(default_factory=TelemetryCurveCfg)
    mock: TelemetryMockCfg = field(default_factory=TelemetryMockCfg)

    def status_text(self, code: int | None) -> str:
        if code is None:
            return "--"
        text = self.status_map.get(code)
        if text is None:
            text = self.status_map.get(str(code))
        return str(text) if text else f"码{code}"


@dataclass
class MotionCfg:
    """视频页方向运动控制：vx/vr 双通道直通 + 周期重发（见设计报告运动控制小节）。"""

    enabled: bool = True
    #: 线速度上限 m/s、角速度上限 rad/s（按住时的目标值，用户要求放配置文件）
    max_linear_speed: float = 1.0
    max_angular_speed: float = 0.5
    #: 按住期间周期重发帧间隔（ms），抗丢包；节拍器下限 50
    send_period_ms: int = 100
    #: 失焦/切页时立即清零速度并补发停止帧
    stop_on_focus_loss: bool = True
    #: 下发帧协议："wheeltec" 组 11 字节成品帧（默认）；"template" 走 frame_hex 占位模板
    frame_protocol: str = "wheeltec"
    #: Z 角速度符号翻转：实机转向与界面约定（逆时针为正）相反时打开
    invert_angular: bool = False
    #: 运动帧 hex 模板，{vx}/{vr} 填 ×100 的有符号 16 位大端，仅 frame_protocol="template" 生效
    frame_hex: str = "AA 01 {vx} {vr}"


@dataclass
class RecordCfg:
    video_dir: str = "data/recordings"
    byte_dir: str = "data/passthrough"
    log_dir: str = "data/logs"
    snapshot_dir: str = "data/snapshots"
    export_csv: bool = True


@dataclass
class UiCfg:
    theme: str = "light"
    geometry: list[int] = field(default_factory=lambda: [1280, 800, 60, 40])
    start_page: str = "video"
    poll_ui_ms: int = 200
    at_log_max: int = 500
    confirm_exit: bool = True
    #: 正文像素字号，界面所有字号由它换算（默认值须与 ui.theme.DEFAULT_FONT_PX 一致）
    font_size: int = 20


@dataclass
class MetaCfg:
    path: str = ""
    loaded: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class AppConfig:
    app: AppCfg = field(default_factory=AppCfg)
    video: VideoCfg = field(default_factory=VideoCfg)
    serial: SerialCfg = field(default_factory=SerialCfg)
    at: AtCfg = field(default_factory=AtCfg)
    udp_at: UdpAtCfg = field(default_factory=UdpAtCfg)
    link: LinkCfg = field(default_factory=LinkCfg)
    passthrough: PassthroughCfg = field(default_factory=PassthroughCfg)
    telemetry: TelemetryCfg = field(default_factory=TelemetryCfg)
    motion: MotionCfg = field(default_factory=MotionCfg)
    record: RecordCfg = field(default_factory=RecordCfg)
    ui: UiCfg = field(default_factory=UiCfg)
    meta: MetaCfg = field(default_factory=MetaCfg)

    def resolved_path(self) -> Path:
        if self.meta.path:
            return Path(self.meta.path)
        return project_root() / "config" / "app.yaml"

    def save(self, path: str | None = None) -> Path:
        import yaml

        target = Path(path) if path else self.resolved_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        payload.pop("meta", None)
        with target.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)
        self.meta.path = str(target)
        return target

    def rtsp_url(self, stream_index: int | None = None) -> str:
        return self.video.resolved_url(stream_index)

    def abs_dir(self, key: str) -> Path:
        raw = str(getattr(self.record, key, getattr(self.app, key, "")) or "")
        p = Path(raw)
        return p if p.is_absolute() else project_root() / p

    def summary(self) -> list[str]:
        return [
            f"配置来源: {self.meta.path or '(内置默认)'}",
            f"运行模式: {'仿真 mock' if self.app.mock else '实机'}",
            f"视频: {self.rtsp_url()} (stream={self.video.stream_index}, {self.video.transport})",
            f"串口: {self.serial.port or '(自动检测)'} @{self.serial.baudrate}"
            f" {self.serial.bytesize}{self.serial.parity}{self.serial.stopbits}",
            f"AT 通道: {self.at.channel}"
            + (
                f" -> 广播:{self.udp_at.target_port}"
                + (
                    f"（目标 {self.udp_at.target_mac}）"
                    if self.udp_at.target_mac
                    else "（发现握手）"
                )
                + (f" 本机 {self.udp_at.host_id}" if self.udp_at.host_id else "")
                if self.at.channel == "udp"
                else ""
            ),
            f"轮询: {'开' if self.link.auto_poll else '关'} 每 {self.link.poll_interval_s}s"
            f" | RSSI 阈值 {self.link.rssi_warn}/{self.link.rssi_crit} dBm",
        ]

    @classmethod
    def load(cls, path: str | None = None, *, env: bool = True) -> AppConfig:
        cfg = cls()
        candidate = Path(path) if path else project_root() / "config" / "app.yaml"
        if candidate.is_file():
            try:
                import yaml

                with candidate.open("r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                _merge_into(cfg, data, cfg.meta.warnings, "config")
                cfg.meta.loaded = True
            except Exception as exc:
                cfg.meta.warnings.append(f"配置文件解析失败: {exc}")
        else:
            cfg.meta.warnings.append(f"未找到配置文件（使用默认值）: {candidate}")
        cfg.meta.path = str(candidate)
        if env:
            _apply_env(cfg, os.environ)
        _validate(cfg)
        return cfg


_ENV_MAP = {
    "HMI_MOCK": ("app", "mock"),
    "HMI_LOG_LEVEL": ("app", "log_level"),
    "HMI_SERIAL_PORT": ("serial", "port"),
    "HMI_RTSP_URL": ("video", "url"),
    "HMI_CAMERA_IP": ("video", "camera_ip"),
    "HMI_AT_CHANNEL": ("at", "channel"),
    "HMI_UDP_TARGET_MAC": ("udp_at", "target_mac"),
    "HMI_UDP_HOST_ID": ("udp_at", "host_id"),
    "HMI_UDP_TARGET_PORT": ("udp_at", "target_port"),
    "HMI_POLL_S": ("link", "poll_interval_s"),
}


def _apply_env(cfg: AppConfig, environ: dict[str, str]) -> None:
    for key, (section, name) in _ENV_MAP.items():
        raw = environ.get(key)
        if raw is None or raw == "":
            continue
        holder = getattr(cfg, section)
        cur = getattr(holder, name, None)
        setattr(holder, name, _coerce(raw, cur))


def _merge_into(inst: Any, data: Any, warnings: list[str], path: str) -> Any:
    if not isinstance(data, dict):
        warnings.append(f"{path}: 期望映射结构，已忽略")
        return inst
    known = set()
    for f in fields(inst):
        known.add(f.name)
        if f.name not in data:
            continue
        value = data[f.name]
        current = getattr(inst, f.name)
        if is_dataclass(current):
            _merge_into(current, value, warnings, f"{path}.{f.name}")
        else:
            setattr(inst, f.name, _coerce(value, current))
    for key in data:
        if key not in known:
            warnings.append(f"{path}.{key}: 未知字段，已忽略")
    return inst


def _coerce(value: Any, current: Any) -> Any:
    if value is None:
        return None
    if isinstance(current, bool):
        return parse_bool(value, default=current)
    if isinstance(current, int):
        return safe_int(value, default=current)
    if isinstance(current, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return current
    if isinstance(current, str):
        return str(value)
    if isinstance(current, list):
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]
    return value


def _validate(cfg: AppConfig) -> None:
    if cfg.serial.baudrate not in (9600, 19200, 38400, 57600, 115200):
        cfg.meta.warnings.append(
            f"串口波特率 {cfg.serial.baudrate} 非常见值，模块 RS485 固定 115200"
        )
    if cfg.at.channel not in ("serial", "udp"):
        cfg.meta.warnings.append(f"AT 通道只能是 serial/udp，收到 {cfg.at.channel}，已回退 udp")
        cfg.at.channel = "udp"
    if cfg.at.fallback_channel not in ("", "serial", "udp", "mock"):
        cfg.meta.warnings.append(
            f"AT 回退通道只能是 serial/udp/空（禁用），收到 {cfg.at.fallback_channel}，"
            "已按默认禁用处理"
        )
        cfg.at.fallback_channel = ""
    if cfg.at.channel == "serial" or cfg.at.fallback_channel == "serial":
        cfg.meta.warnings.append(
            "手册第六章 AT 只走网口配置（6.2），串口是纯透传通道：向串口发 "
            "AT 会被透传到远端（可能打搅被控设备的 485 总线），本机电台不解析"
        )
    raw_pt = str(cfg.passthrough.channel or "").strip().lower()
    if raw_pt not in ("", "same", "serial", "udp", "mock"):
        cfg.meta.warnings.append(
            f"透传通道只能是 serial/udp/mock/same，收到 {cfg.passthrough.channel!r}，"
            "已回退 serial"
        )
    cfg.passthrough.channel = raw_pt or "serial"
    if cfg.video.stream_index not in (0, 1):
        cfg.video.stream_index = 1
    raw_transport = str(cfg.video.transport or "").strip().lower()
    if raw_transport not in ("tcp", "udp"):
        cfg.meta.warnings.append(
            f"视频传输只能是 tcp/udp，收到 {cfg.video.transport!r}，已回退 tcp"
        )
        cfg.video.transport = "tcp"
    else:
        cfg.video.transport = raw_transport
    if not 0 <= cfg.video.pacing_frames <= 15:
        cfg.meta.warnings.append(
            f"放帧队列深度 {cfg.video.pacing_frames} 超出 0~15，已夹回可用范围"
        )
        cfg.video.pacing_frames = min(15, max(0, int(cfg.video.pacing_frames)))
    if not 0.0 <= cfg.video.max_dwell_ms <= 2000.0:
        cfg.meta.warnings.append(
            f"积压停留预算 {cfg.video.max_dwell_ms}ms 超出 0~2000，已夹回"
            "（0 表示关闭积压跳帧）"
        )
        cfg.video.max_dwell_ms = min(2000.0, max(0.0, float(cfg.video.max_dwell_ms)))
    if cfg.link.poll_interval_s < 0.5:
        cfg.link.poll_interval_s = 0.5
    if cfg.at.channel == "udp" and not cfg.udp_at.broadcast_scan:
        cfg.meta.warnings.append(
            "网口 AT 依赖 op1 发现握手，udp_at.broadcast_scan 关闭后必须填写 "
            "udp_at.target_mac 定向模块，否则通道无法打开"
        )
    for key in ("host_id", "target_mac"):
        raw = str(getattr(cfg.udp_at, key) or "").strip()
        if not raw:
            continue
        cleaned = re.sub(r"[:\-\s]", "", raw)
        if len(cleaned) != 12 or not re.fullmatch(r"[0-9A-Fa-f]{12}", cleaned):
            cfg.meta.warnings.append(
                f"udp_at.{key}={raw!r} 不是合法 MAC（6 字节，如 96-a1-3e-7d-83-88），已按留空处理"
            )
            setattr(cfg.udp_at, key, "")
    if cfg.udp_at.discovery_interval_ms < 100:
        cfg.meta.warnings.append(
            f"发现保活间隔 {cfg.udp_at.discovery_interval_ms}ms 过小（netat 实测约 500ms），"
            "已抬到 100ms"
        )
        cfg.udp_at.discovery_interval_ms = 100
    if not 11 <= cfg.ui.font_size <= 28:
        cfg.meta.warnings.append(
            f"界面字号 {cfg.ui.font_size}px 超出 11~28，启动时会自动夹回可用范围"
        )
    tel = cfg.telemetry
    if tel.frame.protocol not in ("wheeltec", "binary", "ascii"):
        cfg.meta.warnings.append(
            f"遥测帧协议只能是 wheeltec/binary/ascii，收到 {tel.frame.protocol}，已回退 wheeltec"
        )
        tel.frame.protocol = "wheeltec"
    if tel.stale_after_s < 1.0:
        cfg.meta.warnings.append(f"遥测 stale_after_s {tel.stale_after_s}s 过小，已夹到 1s")
        tel.stale_after_s = 1.0
    if not 0.5 <= tel.display_throttle_hz <= 20.0:
        clamped = min(20.0, max(0.5, float(tel.display_throttle_hz)))
        cfg.meta.warnings.append(
            f"遥测刷新节流 {tel.display_throttle_hz}Hz 超出 0.5~20，已夹到 {clamped}Hz"
        )
        tel.display_throttle_hz = clamped
    if tel.curve.window_s < 5.0:
        cfg.meta.warnings.append(f"遥测曲线窗口 {tel.curve.window_s}s 过小，已夹到 5s")
        tel.curve.window_s = 5.0
    mot = cfg.motion
    if mot.max_linear_speed <= 0.0 or mot.max_angular_speed <= 0.0:
        cfg.meta.warnings.append("运动最大速度必须大于 0，非正值已回退默认")
        if mot.max_linear_speed <= 0.0:
            mot.max_linear_speed = 1.0
        if mot.max_angular_speed <= 0.0:
            mot.max_angular_speed = 0.5
    if mot.send_period_ms < 50:
        cfg.meta.warnings.append(f"运动重发周期 {mot.send_period_ms}ms 过小，已夹到 50ms")
        mot.send_period_ms = 50


def load_config(path: str | None = None, **overrides: Any) -> AppConfig:
    cfg = AppConfig.load(path)
    if overrides:
        apply_overrides(cfg, overrides)
    return cfg


def apply_overrides(cfg: AppConfig, overrides: dict[str, Any]) -> AppConfig:
    for dotted, value in overrides.items():
        parts = dotted.split(".")
        holder = cfg
        for p in parts[:-1]:
            holder = getattr(holder, p, None)
            if holder is None:
                cfg.meta.warnings.append(f"覆盖路径无效: {dotted}")
                break
        if holder is None:
            continue
        name = parts[-1]
        if not hasattr(holder, name):
            cfg.meta.warnings.append(f"覆盖字段无效: {dotted}")
            continue
        setattr(holder, name, _coerce(value, getattr(holder, name)))
    return cfg
