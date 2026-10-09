"""AT 参数字典：以《EWD611(485) 用户手册 V1.1》第六章为准的元数据 + 校验/编解码。

这一层是"表单/下发"的唯一真相源：UI 只按 group 渲染控件，service 只按 field 读写，
范围、单位、掉电保存说明都从这里取，避免各处硬编码 6~20、500ms 这类魔数。

与仿真的差异处理：
- 手册 TXPOWER 范围 6~20，仿真放宽到 0~30。注册表按手册收紧，但提供
  ``set_range_override()`` 让自检/实验环境放宽，两边都能跑通。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .at_codec import as_text, parse_bool_state, parse_float, parse_int, parse_int_list

RW = "rw"
RO = "ro"
WO = "wo"

Q_QMARK = "qmark"
Q_BARE = "bare"
Q_NONE = "none"

G_IDENTITY = "身份与加密"
G_RF = "无线与频点"
G_POWER = "功率与速率"
G_LINK = "链路与心跳"
G_MAINT = "调试与维护"
G_TELEMETRY = "信道遥测（只读）"
G_ACTION = "动作指令（只写）"

GROUPS = (G_IDENTITY, G_RF, G_POWER, G_LINK, G_MAINT, G_TELEMETRY, G_ACTION)

_MAC_RE = re.compile(r"^([0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


class ParamError(ValueError):
    """参数不在注册表内，或值不合法（services 层统一捕获转告警）。"""


@dataclass(frozen=True)
class ParamSpec:
    """一条 AT 参数的完整描述。"""

    name: str
    title: str
    group: str
    kind: str = "int"
    at: str = ""
    prefix: str = ""
    fields: tuple[str, ...] = ()
    rw: str = RW
    query: str = Q_QMARK
    options: tuple[str, ...] = ()
    case: str = ""
    vmin: int | None = None
    vmax: int | None = None
    unit: str = ""
    max_len: int = 0
    count_max: int = 0
    hex_len: int = 0
    allow_empty: bool = False
    scale: float = 0.0
    order: int = 100
    persist: str = ""
    section: str = ""
    desc: str = ""
    #: 遥测轮询用：True 表示链路侧随时会变的只读量（RSSI/EVM/连接态等）。
    #: 首轮全量读取后，后续轮询只读 volatile 项 + 上轮失败项，减轻总线压力。
    volatile: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def wire_name(self) -> str:
        return (self.at or self.name).upper()

    @property
    def readable(self) -> bool:
        return self.rw in (RW, RO) and self.query != Q_NONE

    @property
    def writable(self) -> bool:
        return self.rw in (RW, WO)

    def bound_text(self) -> str:
        if self.options:
            return "/".join(self.options)
        if self.kind in ("list", "range") and self.count_max:
            return f"最多 {self.count_max} 个"
        if self.hex_len:
            return f"{self.hex_len} 个 hex 字符"
        if self.max_len:
            return f"≤ {self.max_len} 字符"
        if self.vmin is not None or self.vmax is not None:
            lo = "-∞" if self.vmin is None else self.vmin
            hi = "+∞" if self.vmax is None else self.vmax
            return f"{lo} ~ {hi}{self.unit}"
        return "-"


def _s(name: str, title: str, group: str, **kw: Any) -> ParamSpec:
    return ParamSpec(name=name, title=title, group=group, **kw)


#: 注册表内容（键为大写 AT 名）
_SPECS: dict[str, ParamSpec] = {
    spec.name: spec
    for spec in (
        _s(
            "MODE",
            "工作模式",
            G_IDENTITY,
            kind="enum",
            options=("ap", "sta", "group", "apsta"),
            case="lower",
            fields=("mode",),
            order=10,
            section="6.3.1",
            persist="仅 APSTA 模式掉电保存，其余模式重启后回默认",
            desc="ap=主机 / sta=从机 / group=组播 / apsta=混合",
        ),
        _s(
            "SSID",
            "本机 SSID",
            G_IDENTITY,
            kind="text",
            fields=("ssid",),
            max_len=32,
            order=20,
            section="6.3.2",
            persist="掉电保存",
            desc="AP 模式下的网络名，长度小于 32 字符",
        ),
        _s(
            "KEYMGMT",
            "加密方式",
            G_IDENTITY,
            kind="enum",
            options=("WPA-PSK", "NONE"),
            case="upper",
            fields=("keymgmt",),
            order=30,
            section="6.3.3",
            persist="掉电保存",
            desc="WPA-PSK 开启加密，NONE 关闭加密",
        ),
        _s(
            "PSK",
            "加密密码",
            G_IDENTITY,
            kind="hex",
            fields=("psk",),
            hex_len=64,
            allow_empty=True,
            order=40,
            section="6.3.4",
            persist="掉电保存",
            desc="64 个 hex 字符（WPA 预共享密钥）",
        ),
        _s(
            "R_SSID",
            "上级 SSID",
            G_IDENTITY,
            kind="text",
            fields=("r_ssid",),
            max_len=32,
            order=50,
            section="6.3.24",
            persist="掉电保存",
            desc="STA/中继模式连接的上一级 AP 的 ssid",
        ),
        _s(
            "R_PSK",
            "上级密码",
            G_IDENTITY,
            kind="hex",
            fields=("r_psk",),
            hex_len=64,
            allow_empty=True,
            order=60,
            section="6.3.25",
            persist="掉电保存",
            desc="上一级 AP 的加密密码，必须 64 个 hex 字符",
        ),
        _s(
            "BSS_BW",
            "BSS 带宽",
            G_RF,
            kind="enum",
            options=("1", "2", "4", "8"),
            case="none",
            fields=("bss_bw",),
            vmin=1,
            vmax=8,
            unit=" MHz",
            order=70,
            section="6.3.6",
            persist="掉电保存",
            desc="1/2/4/8 MHz，带宽越大速率越高、抗干扰越弱",
        ),
        _s(
            "FREQ_RANGE",
            "工作频率范围",
            G_RF,
            kind="range",
            fields=("freq_range",),
            count_max=2,
            vmin=1000,
            vmax=40000,
            scale=10.0,
            unit="(MHz×10)",
            order=80,
            section="6.3.7",
            desc="起始/截止中心频点，值=中心频点×10；设置 CHAN_LIST 后以列表优先",
        ),
        _s(
            "CHAN_LIST",
            "工作频点列表",
            G_RF,
            kind="list",
            fields=("chan_list",),
            count_max=16,
            vmin=1000,
            vmax=40000,
            scale=10.0,
            unit="(MHz×10)",
            order=90,
            section="6.3.8",
            desc="最多 16 个频点，值=中心频点×10，如 9080=908MHz",
        ),
        _s(
            "TXPOWER",
            "最大发射功率",
            G_POWER,
            kind="int",
            fields=("txpower",),
            vmin=6,
            vmax=20,
            unit=" dBm",
            order=110,
            section="6.3.15",
            persist="掉电保存",
            desc="查询响应带 dbm 后缀（+TXPOWER:20dbm）",
        ),
        _s(
            "TX_MCS",
            "发送速率 MCS",
            G_POWER,
            kind="int",
            fields=("tx_mcs",),
            vmin=0,
            vmax=255,
            order=120,
            section="6.3.17",
            persist="掉电保存",
            desc="0~7 或 10（1M 模式）=固定速率；其他值（出厂默认 255）=速率自动调整",
        ),
        _s(
            "ACKTMO",
            "ACK 超时",
            G_POWER,
            kind="int",
            fields=("acktmo",),
            vmin=0,
            vmax=4000,
            unit=" us",
            order=130,
            section="6.3.16",
            desc="按距离增加超时，经验式 10×(公里-1)",
        ),
        _s(
            "HEART_INT",
            "心跳间隔",
            G_LINK,
            kind="int",
            fields=("heart_int",),
            vmin=500,
            vmax=10000,
            unit=" ms",
            order=140,
            section="6.3.18",
            persist="掉电保存",
            desc="建议 STA_count×500；V1.6.2 起只需设 AP，对 STA 无效",
        ),
        _s(
            "JOINGROUP",
            "加入组播网络",
            G_LINK,
            kind="pair_mac_int",
            fields=("group_addr", "group_aid"),
            rw=WO,
            query=Q_NONE,
            order=150,
            section="6.3.23",
            desc="格式 AT+JOINGROUP=<group_addr>,<aid>；必须先把 MODE 设为 group",
        ),
        _s(
            "SYSDBG_LMAC",
            "LMAC 调试打印",
            G_MAINT,
            kind="enum",
            at="SYSDBG",
            prefix="LMAC,",
            options=("1", "0"),
            case="none",
            fields=("sysdbg_lmac",),
            rw=WO,
            query=Q_NONE,
            order=160,
            section="6.3.21",
            desc="LMAC 统计默认打开（打印量大，看 STA_INFO 时建议先关）",
        ),
        _s(
            "SYSDBG_WNB",
            "WNB 调试打印",
            G_MAINT,
            kind="enum",
            at="SYSDBG",
            prefix="WNB,",
            options=("1", "0"),
            case="none",
            fields=("sysdbg_wnb",),
            rw=WO,
            query=Q_NONE,
            order=170,
            section="6.3.21",
            desc="WNB 统计默认关闭",
        ),
        _s(
            "PAIR",
            "配对控制",
            G_ACTION,
            kind="enum",
            options=("1", "0"),
            case="none",
            rw=WO,
            query=Q_NONE,
            order=200,
            section="6.3.5",
            desc="1 启动配对、0 停止；配对成功产生 PAIR SUCCESS 消息但不会自动退出",
        ),
        _s(
            "UNPAIR",
            "解除指定 STA",
            G_ACTION,
            kind="mac",
            rw=WO,
            query=Q_NONE,
            order=210,
            section="6.3.19",
            desc="成功回显 unpair sta:<mac>，不存在回显 sta:<mac> is not exist",
        ),
        _s(
            "LOADDEF",
            "恢复出厂设置",
            G_ACTION,
            kind="enum",
            options=("1",),
            case="none",
            rw=WO,
            query=Q_NONE,
            order=220,
            section="6.3.20",
            desc="清空全部参数，危险操作，UI 需二次确认",
        ),
        _s(
            "SCAN_AP",
            "扫描周围 AP",
            G_ACTION,
            kind="int",
            options=(),
            rw=WO,
            query=Q_NONE,
            vmin=1,
            vmax=10,
            order=230,
            section="6.3.13",
            desc="STA 模式下执行，结果用 BSSLIST 读取",
        ),
        _s(
            "BSSLIST",
            "扫描结果列表",
            G_TELEMETRY,
            kind="text",
            at="BSSLIST",
            rw=RO,
            query=Q_QMARK,
            volatile=True,
            order=300,
            section="6.3.14",
            desc="需要先 SCAN_AP，AP 设置了 ssid 才会被扫到",
        ),
        _s(
            "RSSI",
            "信号强度",
            G_TELEMETRY,
            kind="float",
            rw=RO,
            unit=" dBm",
            volatile=True,
            order=310,
            section="6.3.9",
            desc="AT+RSSI? 查本机；AT+RSSI=<index 或 mac> 查指定从机",
        ),
        _s(
            "EVM",
            "调制误差",
            G_TELEMETRY,
            kind="float",
            rw=RO,
            unit=" dB",
            volatile=True,
            order=320,
            section="6.3.11/6.3.12",
            desc="手册未单列该查询，固件与仿真支持，值越负越好",
        ),
        _s(
            "CONN_STATE",
            "连接状态",
            G_TELEMETRY,
            kind="state",
            rw=RO,
            query=Q_BARE,
            volatile=True,
            order=330,
            section="6.3.10",
            desc="响应为 +CONNECTED / +DISCONNECT，手册未附 OK",
        ),
        _s(
            "WNBCFG",
            "设备参数信息",
            G_TELEMETRY,
            kind="kv",
            rw=RO,
            query=Q_BARE,
            volatile=True,
            order=340,
            section="6.3.11",
            desc="一行 key=value 形式的当前无线配置",
        ),
        _s(
            "STA_INFO",
            "STA 明细",
            G_TELEMETRY,
            kind="sta",
            rw=RO,
            query=Q_QMARK,
            volatile=True,
            order=350,
            section="6.3.12",
            desc="AP 用：AT+STA_INFO=<id>，id 从 1 开始，含 RSSI/EVM/MCS/包统计",
        ),
        _s(
            "VERSION",
            "固件版本",
            G_TELEMETRY,
            kind="text",
            rw=RO,
            order=360,
            section="6.3",
            desc="仿真模块提供，便于自检断言",
        ),
        _s(
            "BAUDRATE",
            "串口波特率",
            G_MAINT,
            kind="int",
            rw=RO,
            query=Q_NONE,
            order=400,
            section="6.3.22",
            desc="该型号不支持修改，固定 115200，帧格式 8N1",
        ),
    )
}

#: 手册范围与仿真范围不一致时，用这里做临时放宽（自检/实验环境）
_RANGE_OVERRIDES: dict[str, tuple[Any, Any]] = {}

#: 不可变基线，供 clear_range_overrides() 复原
_ORIGINAL: dict[str, ParamSpec] = dict(_SPECS)

#: field -> spec 反查表（含多字段参数）
_BY_FIELD: dict[str, str] = {}
_BY_ALIAS: dict[str, str] = {}


def _rebuild_indexes() -> None:
    """重建反查表：AT 名 / RadioParams 字段名 / 中文标题都能解析到同一个键。

    UI 层拿到的往往是中文标题，协议层再自己映射一遍就是重复定义；这里一次性
    把标题登记为别名。``setdefault`` 而不是直接赋值——同名标题以先注册的为准，
    不让后加的规格悄悄改写前一条的解析结果。
    """
    _BY_FIELD.clear()
    _BY_ALIAS.clear()
    for key, spec in _SPECS.items():
        _BY_ALIAS[key.lower()] = key
        _BY_ALIAS[key] = key
        if spec.title:
            _BY_ALIAS.setdefault(spec.title, key)
        for f in spec.fields:
            _BY_FIELD[f] = key
            _BY_ALIAS[f.lower()] = key
            _BY_ALIAS[f.upper()] = key


_rebuild_indexes()


def set_range_override(name: str, lo: Any = None, hi: Any = None) -> None:
    """放宽/收紧某参数的整数上下限（自检或实验环境用），未知参数静默忽略。"""
    key = resolve_key(name)
    if key is None:
        return
    base = _ORIGINAL.get(key) or _SPECS[key]
    new_lo = base.vmin if lo is None else int(lo)
    new_hi = base.vmax if hi is None else int(hi)
    _RANGE_OVERRIDES[key] = (new_lo, new_hi)
    _SPECS[key] = replace(base, vmin=new_lo, vmax=new_hi)


def clear_range_overrides() -> None:
    """恢复全部参数到注册表基线。"""
    _RANGE_OVERRIDES.clear()
    _SPECS.update(_ORIGINAL)


def spec_for(name: str) -> ParamSpec | None:
    key = resolve_key(name)
    return _SPECS.get(key) if key else None


def resolve_key(name: object) -> str | None:
    """把 TXPOWER / txpower / TX_Power 等写法归一到注册表键。"""
    raw = as_text(name).strip()
    if not raw:
        return None
    if raw in _SPECS:
        return raw
    low = raw.lower()
    if low in _BY_ALIAS:
        return _BY_ALIAS[low]
    up = raw.upper()
    if up in _SPECS:
        return up
    if up in _BY_ALIAS:
        return _BY_ALIAS[up]
    return None


def all_specs() -> list[ParamSpec]:
    return sorted(_SPECS.values(), key=lambda s: (s.group, s.order, s.name))


def by_group() -> dict[str, list[ParamSpec]]:
    out: dict[str, list[ParamSpec]] = {g: [] for g in GROUPS}
    for spec in all_specs():
        out.setdefault(spec.group, []).append(spec)
    return out


def writable_specs() -> list[ParamSpec]:
    return [s for s in all_specs() if s.writable]


def is_volatile(name: str) -> bool:
    """该参数是否属于"链路侧随时会变"的遥测量（未知参数按 False 处理）。"""
    spec = spec_for(name)
    return bool(spec and spec.volatile)


def volatile_names() -> list[str]:
    """全部标记为 volatile 的参数名，按注册表 order 排序。"""
    return [s.name for s in all_specs() if s.volatile]


def readable_specs() -> list[ParamSpec]:
    return [s for s in all_specs() if s.readable]


# ------------------------------------------------------------------------ 校验
def _norm_text(value: Any, case: str) -> str:
    text = str(value if value is not None else "").strip()
    if case == "lower":
        return text.lower()
    if case == "upper":
        return text.upper()
    return text


def validate(name: str, value: Any) -> tuple[bool, str, Any]:
    """-> (是否合法, 规范化后的 AT 值文本或错误信息, 解析后的 Python 值)。"""
    spec = spec_for(name)
    if spec is None:
        return False, f"未知参数 {name}", None
    ok, info, text, typed = _validate_kind(spec, value)
    if not ok:
        return False, info, None
    return True, text, typed


def _validate_kind(spec: ParamSpec, value: Any) -> tuple[bool, str, str, Any]:
    kind = spec.kind
    if kind == "enum":
        text = _norm_text(value, spec.case or "none")
        if spec.options and text not in spec.options:
            return False, f"{spec.title} 只能是 {'/'.join(spec.options)}", "", None
        if not spec.options:
            return False, f"{spec.title} 未定义可选值", "", None
        return True, text, text, _coerce_option(spec, text)
    if kind == "int":
        num = parse_int(value)
        if num is None:
            return False, f"{spec.title} 需要整数", "", None
        lo, hi = _bounds(spec)
        if lo is not None and num < lo:
            return False, f"{spec.title} 不能小于 {lo}{spec.unit}", "", None
        if hi is not None and num > hi:
            return False, f"{spec.title} 不能大于 {hi}{spec.unit}", "", None
        return True, str(num), str(num), num
    if kind == "float":
        try:
            num = float(str(value).strip())
        except (TypeError, ValueError):
            return False, f"{spec.title} 需要数值", "", None
        return True, f"{num:.1f}", f"{num:.1f}", num
    if kind == "hex":
        text = as_text(value).replace(" ", "").replace("0x", "").strip().lower()
        if not text and spec.allow_empty:
            return True, "", "", ""
        if text and not _HEX_RE.match(text):
            return False, f"{spec.title} 只能包含 hex 字符", "", None
        if spec.hex_len and len(text) != spec.hex_len:
            return (
                False,
                f"{spec.title} 需要 {spec.hex_len} 个 hex 字符，当前 {len(text)}",
                "",
                None,
            )
        return True, text, text, text
    if kind == "text":
        text = as_text(value).strip()
        if spec.max_len and len(text) > spec.max_len:
            return False, f"{spec.title} 最长 {spec.max_len} 字符，当前 {len(text)}", "", None
        bad = [c for c in ("=", ",", "\r", "\n") if c in text]
        if bad:
            return False, f"{spec.title} 不能包含 {bad}", "", None
        return True, text, text, text
    if kind in ("list", "range"):
        items = _as_int_list(value)
        if not items:
            return False, f"{spec.title} 至少需要 1 个频点", "", None
        if kind == "range" and len(items) != 2:
            return False, f"{spec.title} 需要 start,end 两个值", "", None
        if spec.count_max and len(items) > spec.count_max:
            return False, f"{spec.title} 最多 {spec.count_max} 个", "", None
        lo, hi = _bounds(spec)
        for num in items:
            if lo is not None and num < lo:
                return False, f"{spec.title} 频点 {num} 小于下限 {lo}", "", None
            if hi is not None and num > hi:
                return False, f"{spec.title} 频点 {num} 大于上限 {hi}", "", None
        if kind == "range" and items[0] >= items[1]:
            return False, f"{spec.title} 要求 start < end", "", None
        text = ",".join(str(n) for n in items)
        return True, text, text, items
    if kind == "pair_mac_int":
        addr, aid = _split_mac_int(value)
        if not _MAC_RE.match(addr):
            return False, f"{spec.title} 组播地址需为 00:11:22:33:44:55 形式", "", None
        if aid < 0:
            return False, f"{spec.title} AID 不能为负", "", None
        text = f"{addr},{aid}"
        return True, text, text, (addr, aid)
    if kind == "mac":
        text = as_text(value).strip().lower()
        if not _MAC_RE.match(text):
            return False, f"{spec.title} MAC 格式不正确", "", None
        return True, text, text, text
    return False, f"{spec.title} 不支持直接下发（kind={kind}）", "", None


def _coerce_option(spec: ParamSpec, text: str) -> Any:
    if text.lstrip("-").isdigit():
        return int(text)
    return text


def _bounds(spec: ParamSpec) -> tuple[int | None, int | None]:
    return spec.vmin, spec.vmax


def range_overrides() -> dict[str, tuple[Any, Any]]:
    """当前被放宽/收紧过的参数及其上下限，供自检日志核对。"""
    return dict(_RANGE_OVERRIDES)


def _as_int_list(value: Any) -> list[int]:
    if isinstance(value, str):
        return parse_int_list(value)
    if isinstance(value, Iterable):
        out: list[int] = []
        for item in value:
            num = parse_int(item)
            if num is not None:
                out.append(num)
        return out
    num = parse_int(value)
    return [num] if num is not None else []


def _split_mac_int(value: Any) -> tuple[str, int]:
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        return str(value[0]).strip().lower(), int(parse_int(value[1]) or 0)
    text = as_text(value).strip()
    head, _, tail = text.rpartition(",")
    if not head:
        head, _, tail = text.rpartition(" ")
    if not head:
        return text.lower(), 0
    return head.strip().lower(), int(parse_int(tail) or 0)


# --------------------------------------------------------------------- 编码/解码
def encode(name: str, value: Any) -> tuple[bool, str, str]:
    """-> (ok, AT 值串（已带 prefix）, 错误信息)。"""
    spec = spec_for(name)
    if spec is None:
        return False, "", f"未知参数 {name}"
    ok, text, _typed = validate(name, value)
    if not ok:
        return False, "", text
    if not text and not spec.prefix and not spec.allow_empty:
        return False, "", f"{spec.title} 不能为空"
    return True, f"{spec.prefix}{text}", ""


def decode(name: str, text: object) -> Any:
    """把响应文本转成 Python 值（int/list/str/bool/float），未知参数返回原文。

    对"已经是 Python 值"的输入必须幂等：``build_snapshot`` 拿到的值是 ``query_value``
    解码过的结果，再 ``str()`` 一遍会把 ``[8680, 8700, 8720]`` 变成带方括号的字符串，
    按逗号切之后首尾频点被当成脏 token 丢掉，信道页就只剩中间一个频点；``0`` 也会被
    ``text or ""`` 吞成空串。所以结构化 kind 直接交给能吃对象的解析器。
    """
    spec = spec_for(name)
    if spec is None:
        return "" if text is None else str(text)
    kind = spec.kind
    if kind in ("list", "range"):
        return _as_int_list(text)
    if kind == "pair_mac_int":
        return _split_mac_int(text)
    raw = text.strip() if isinstance(text, str) else text
    if raw is None:
        raw = ""
    if kind == "enum":
        num = parse_int(raw)
        return num if num is not None else raw
    if kind == "int":
        return parse_int(raw)
    if kind == "mac":
        return str(raw)
    if kind == "float":
        return parse_float(raw)
    if kind == "state":
        state = parse_bool_state(raw)
        return state if state is not None else raw
    return raw if isinstance(raw, str) else str(raw)


def apply_to_params(params: Any, name: str, value: Any) -> bool:
    """把读到的响应写进 RadioParams 对应字段，成功返回 True。"""
    spec = spec_for(name)
    if spec is None or not spec.fields:
        return False
    typed = decode(spec.name, value)
    if spec.kind == "pair_mac_int" and isinstance(typed, tuple):
        addr, aid = typed
        setattr(params, spec.fields[0], addr)
        if len(spec.fields) > 1:
            setattr(params, spec.fields[1], aid)
        return True
    setattr(params, spec.fields[0], typed)
    return True


def plan(params: Any) -> list[tuple[str, str]]:
    """把 RadioParams 里非 None 的字段转成 (AT 名, 值串) 下发序列。"""
    touched = params.touched() if hasattr(params, "touched") else dict(params or {})
    steps: list[tuple[int, str, str]] = []
    for key, spec in _SPECS.items():
        if not spec.writable or not spec.fields:
            continue
        hit = None
        for f in spec.fields:
            if f in touched and touched[f] is not None:
                hit = touched[f]
                if spec.kind == "pair_mac_int":
                    hit = (touched.get(spec.fields[0]), touched.get(spec.fields[1]))
                break
        if hit is None:
            continue
        ok, text, err = encode(key, hit)
        if not ok:
            raise ParamError(f"{spec.title}: {err}")
        steps.append((spec.order, key, text))
    steps.sort(key=lambda x: x[0])
    return [(name, text) for _order, name, text in steps]


def summary_rows() -> list[dict[str, str]]:
    """给 UI/文档用的扁平表格。"""
    rows: list[dict[str, str]] = []
    for spec in all_specs():
        rows.append(
            {
                "at": spec.wire_name,
                "title": spec.title,
                "group": spec.group,
                "rw": spec.rw,
                "query": spec.query,
                "bound": spec.bound_text(),
                "persist": spec.persist,
                "section": spec.section,
                "desc": spec.desc,
            }
        )
    return rows
