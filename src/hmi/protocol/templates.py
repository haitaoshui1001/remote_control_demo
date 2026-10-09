"""485 控制指令模板库：把常用指令沉淀成可复用、可参数化、可一键下发的条目。

为什么要单独一层
----------------
透传控制台手打 Hex 能解决"试一条"的问题，但现场是"同一条开关指令每天发几百次，
只是从机地址和通道号在变"。模板库负责的是**描述**（这条指令叫什么、属于哪组、
要不要等应答、应答怎么判定），而**上线字节**这件事仍然交给
``passthrough.PassthroughEngine``——两层各管各的，模板层完全不碰串口。

文件形态
--------
``config/templates.yaml``::

    - id: motor_on
      name: 电机开
      group: 电机
      payload: "01 06 00 10 00 01"
      mode: hex
      crc16_append: true
      expect_reply: true
      match_prefix: "01 06"
      min_reply_bytes: 0
      suffix: none
      interval_ms: 0
      enabled: true
      order: 10
      note: 保持寄存器 0x0010 置 1

顶层既可以写成列表，也可以写成 ``templates:`` 键下的列表；字段名与
``CommandTemplate`` 一一对应，多余字段忽略、缺失字段用默认值——配置文件被
手改坏时，程序应该"少几条"而不是"起不来"。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import MISSING, fields
from pathlib import Path
from typing import Any

from ..core.config import PassthroughCfg, project_root
from ..core.logger import get_logger
from ..core.models import CommandResult, CommandTemplate
from ..core.utils import ascii_repr, bytes_to_hex, to_bytes
from .passthrough import PassthroughEngine, build_frame

__all__ = ["TemplateLibrary", "render_payload", "missing_placeholders", "default_templates"]

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DATACLASS_FIELDS = tuple(CommandTemplate.__dataclass_fields__)  # type: ignore[attr-defined]


def missing_placeholders(payload: str) -> list[str]:
    """模板里出现的 ``{xxx}`` 占位符名字（去重保序）。"""
    seen: list[str] = []
    for m in _PLACEHOLDER_RE.finditer(payload or ""):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def render_payload(payload: str, values: dict[str, Any] | None = None) -> str:
    """把 ``{name}`` 换成实际值；没给值的占位符**原样保留**，由 ``validate`` 报错。

    不做 Python ``str.format``：payload 里出现 ``{{`` 或对角括号是常事，
    用 format 会把指令本身炸掉。
    """
    if not values:
        return payload or ""

    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in values:
            return str(values[key])
        return m.group(0)

    return _PLACEHOLDER_RE.sub(repl, payload or "")


def default_templates() -> list[CommandTemplate]:
    """出厂示例：一条 Modbus 读、一条开关、一条 ASCII 文本，覆盖 hex/ascii 两种模式。

    这些是**写法示范**，不是对用户设备的断言；首次运行落盘后用户随便改。
    """
    return [
        CommandTemplate(
            id="modbus_read_holding",
            name="读保持寄存器 0x0000~0x0001",
            group="Modbus 示例",
            payload="01 03 00 00 00 02",
            mode="hex",
            crc16_append=True,
            expect_reply=True,
            match_prefix="01 03",
            note="从机 1，功能码 03；CRC 由程序自动追加",
            order=10,
        ),
        CommandTemplate(
            id="generic_on",
            name="通道 {ch} 开",
            group="通用",
            payload="01 06 00 {reg} 00 01",
            mode="hex",
            crc16_append=True,
            expect_reply=True,
            note="占位符示例：{ch} 与 {reg} 在下发前填入",
            order=20,
        ),
        CommandTemplate(
            id="ascii_status",
            name="查询状态（ASCII）",
            group="通用",
            payload="STATUS\\r\\n",
            mode="ascii",
            suffix="none",
            expect_reply=True,
            note="ASCII 模式支持 \\\\r \\\\n 转义",
            order=30,
        ),
    ]


class TemplateLibrary:
    """模板的增删改查 + 下发。持有引擎引用，但不复制它的任何字节逻辑。"""

    def __init__(
        self,
        engine: PassthroughEngine | None = None,
        *,
        path: str | Path | None = None,
        cfg: PassthroughCfg | None = None,
        log: logging.Logger | None = None,
        autoload: bool = True,
    ) -> None:
        self.engine = engine
        self.cfg = cfg or PassthroughCfg()
        self.log = log or get_logger("hmi.protocol.templates")
        self.path = Path(path) if path else project_root() / "config" / "templates.yaml"
        self._lock = threading.RLock()
        self._items: dict[str, CommandTemplate] = {}
        self.loaded_from: str = ""
        self.load_errors: list[str] = []
        if autoload:
            self.load()

    # ------------------------------------------------------------------ 读写
    def load(self, path: str | Path | None = None) -> list[CommandTemplate]:
        """读 YAML；文件不存在则写入出厂示例，保证 UI 第一屏不是空的。"""
        if path is not None:
            self.path = Path(path)
        self.load_errors = []
        rows = None
        if self.path.is_file():
            rows = self._read_yaml()
            self.loaded_from = str(self.path)
        if rows is None:
            items = default_templates()
            with self._lock:
                self._items = {t.id: t for t in items}
            if not self.path.is_file():
                try:
                    self.save()
                    self.log.info("模板文件不存在，已写入示例模板: %s", self.path)
                except OSError as exc:
                    self.log.warning("示例模板落盘失败: %s", exc)
            return self.ordered()
        items = self._parse_rows(rows)
        with self._lock:
            self._items = {t.id: t for t in items}
        self.log.info("已加载 %d 条指令模板: %s", len(items), self.path)
        return self.ordered()

    def _read_yaml(self) -> Any:
        import yaml

        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except Exception as exc:
            self.load_errors.append(f"模板文件解析失败: {exc}")
            self.log.warning("模板文件解析失败 %s: %s", self.path, exc)
            return None
        if isinstance(data, dict):
            return data.get("templates", data.get("items", []))
        return data

    @staticmethod
    def _parse_rows(rows: Any) -> list[CommandTemplate]:
        items: list[CommandTemplate] = []
        if not isinstance(rows, list):
            return items
        for n, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            fields = {k: v for k, v in row.items() if k in _DATACLASS_FIELDS}
            try:
                tpl = CommandTemplate(**fields)
            except TypeError:
                continue
            if not tpl.id:
                tpl.id = f"tpl_{n + 1}"
            if not tpl.name:
                tpl.name = tpl.id
            items.append(tpl)
        return items

    def save(self, path: str | Path | None = None) -> Path:
        import yaml

        target = Path(path) if path else self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = [self._row(t) for t in self.ordered()]
        with target.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)
        self.path = target
        self.loaded_from = str(target)
        return target

    @staticmethod
    def _row(tpl: CommandTemplate) -> dict[str, Any]:
        """只写"和默认值不一样"的字段；``ts`` 这类带 default_factory 的运行期字段不落盘。"""
        out: dict[str, Any] = {}
        for f in fields(CommandTemplate):
            if f.default is MISSING:
                continue
            value = getattr(tpl, f.name)
            if value == f.default:
                continue
            out[f.name] = value
        return out

    # ------------------------------------------------------------------ CRUD
    def all(self) -> list[CommandTemplate]:
        with self._lock:
            return list(self._items.values())

    def ordered(self) -> list[CommandTemplate]:
        with self._lock:
            return sorted(self._items.values(), key=lambda t: (t.order, t.group, t.name))

    def get(self, template_id: str) -> CommandTemplate | None:
        with self._lock:
            return self._items.get(template_id)

    def groups(self) -> list[str]:
        """按组名去重保序，供 UI 左侧分组树。"""
        with self._lock:
            names = [t.group or "默认" for t in self.ordered()]
        out: list[str] = []
        for n in names:
            if n not in out:
                out.append(n)
        return out

    def by_group(self, group: str) -> list[CommandTemplate]:
        with self._lock:
            return [t for t in self.ordered() if (t.group or "默认") == group]

    def upsert(self, tpl: CommandTemplate) -> CommandTemplate:
        """新增或覆盖；id 为空时自动生成。"""
        if not tpl.id:
            tpl.id = self._new_id(tpl.name or "cmd")
        if not tpl.name:
            tpl.name = tpl.id
        with self._lock:
            self._items[tpl.id] = tpl
        return tpl

    def remove(self, template_id: str) -> bool:
        with self._lock:
            return self._items.pop(template_id, None) is not None

    def move(self, template_id: str, delta: int) -> bool:
        """在有序列表里上移/下移，重排 order 为 10 递增。"""
        with self._lock:
            seq = self.ordered()
            ids = [t.id for t in seq]
            if template_id not in ids:
                return False
            i = ids.index(template_id)
            j = max(0, min(len(ids) - 1, i + delta))
            if i == j:
                return False
            seq.insert(j, seq.pop(i))
            for n, tpl in enumerate(seq):
                tpl.order = (n + 1) * 10
            return True

    def _new_id(self, label: str) -> str:
        base = re.sub(r"[^0-9a-zA-Z_]+", "_", label or "cmd").strip("_").lower() or "cmd"
        with self._lock:
            n = 1
            cand = base
            while cand in self._items:
                n += 1
                cand = f"{base}_{n}"
            return cand

    # ------------------------------------------------------------------ 校验
    def validate(self, tpl: CommandTemplate, values: dict[str, Any] | None = None) -> list[str]:
        """返回问题清单（空列表即没问题）。UI 用它把"发不出去"变成"提前红字提醒"。"""
        problems: list[str] = []
        if not tpl.payload.strip():
            problems.append("指令内容为空")
            return problems
        missing = [p for p in missing_placeholders(tpl.payload) if not values or p not in values]
        if missing:
            problems.append("缺少参数：" + ", ".join("{" + p + "}" for p in missing))
        rendered = render_payload(tpl.payload, values or {})
        if missing:
            return problems
        try:
            data = to_bytes(rendered, tpl.mode)
        except ValueError as exc:
            problems.append(str(exc))
            return problems
        if not data:
            problems.append("按所选编码模式解析后没有任何字节")
        if tpl.mode == "hex":
            try:
                to_bytes(tpl.match_prefix, "hex")
            except ValueError:
                problems.append("应答前缀不是合法 Hex")
        if tpl.reply_timeout_ms <= 0:
            problems.append("应答超时须大于 0 ms")
        if tpl.min_reply_bytes < 0:
            problems.append("最小应答长度不能为负")
        return problems

    def preview(self, tpl: CommandTemplate, values: dict[str, Any] | None = None) -> bytes:
        """dry-run：返回将要上线的字节，不发送。UI 用它做"发送前确认"。"""
        return build_frame(
            render_payload(tpl.payload, values or {}),
            mode=tpl.mode,
            suffix=tpl.suffix,
            crc16_append=tpl.crc16_append,
        )

    # ------------------------------------------------------------------ 下发
    def run(
        self,
        tpl: CommandTemplate | str,
        *,
        values: dict[str, Any] | None = None,
        engine: PassthroughEngine | None = None,
    ) -> CommandResult:
        """按模板下发一次。参数不完整、引擎缺失、线上写失败都返回失败结果而不抛异常。

        ``OSError`` 也要接住：串口拔线、端口被占用、pyserial 写超时都是 ``OSError``
        的子类。引擎层刻意把这类异常上抛（让上层能识别"传输层挂了"并触发重连），
        但这里是一键下发的出口，UI 只该看到一个红色失败结论，而不是一个崩溃弹窗；
        传输层状态另有 ``TRANSPORT_STATE`` 事件负责通知。
        """
        if isinstance(tpl, str):
            found = self.get(tpl)
            if found is None:
                return CommandResult(template_id=str(tpl), ok=False, error="模板不存在")
            tpl = found
        eng = engine or self.engine
        if eng is None:
            return CommandResult(
                template_id=tpl.id, name=tpl.name, ok=False, error="透传引擎未装配"
            )
        problems = self.validate(tpl, values)
        if problems:
            return CommandResult(
                template_id=tpl.id, name=tpl.name, ok=False, error="；".join(problems)
            )
        payload = render_payload(tpl.payload, values or {})
        if not tpl.expect_reply:
            try:
                rec = eng.send(
                    payload,
                    mode=tpl.mode,
                    suffix=tpl.suffix,
                    crc16_append=tpl.crc16_append,
                    note=tpl.name,
                    kind="template",
                )
            except (ValueError, RuntimeError, OSError) as exc:
                return CommandResult(
                    template_id=tpl.id, name=tpl.name, ok=False, error=str(exc)
                )
            return CommandResult(
                ts=rec.ts,
                template_id=tpl.id,
                name=tpl.name,
                sent_bytes=rec.data,
                ok=True,
                matched=False,
            )
        try:
            return eng.request(
                payload,
                mode=tpl.mode,
                suffix=tpl.suffix,
                crc16_append=tpl.crc16_append,
                timeout_ms=tpl.reply_timeout_ms,
                match_prefix=tpl.match_prefix,
                min_reply_bytes=tpl.min_reply_bytes,
                template_id=tpl.id,
                name=tpl.name,
                note=tpl.name,
            )
        except (ValueError, RuntimeError, OSError) as exc:
            return CommandResult(
                template_id=tpl.id,
                name=tpl.name,
                sent_bytes=bytes(getattr(exc, "frame_bytes", b"") or b""),
                ok=False,
                error=str(exc),
            )

    def describe_result(self, res: CommandResult) -> str:
        """给人看的一行结论，UI 日志与自检脚本共用。

        行尾追加 ``TX AA 01 …``：成功显示实际上线的字节，被拒/写失败显示
        本来要发的字节（组帧已完成、只是没上线），排查"到底发了什么"不用开抓包。
        """
        head = time.strftime("%H:%M:%S", time.localtime(res.ts))
        label = res.name or res.template_id or "指令"
        tx = f" TX {bytes_to_hex(res.sent_bytes)}" if res.sent_bytes else ""
        if res.ok:
            reply = ascii_repr(res.reply) if res.reply else "(无应答)"
            return f"{head} {label}: OK {reply} [{res.rtt_ms:.0f} ms]{tx}"
        return f"{head} {label}: 失败 - {res.error}{tx}"

    def summary(self) -> dict[str, Any]:
        with self._lock:
            total = len(self._items)
            enabled = sum(1 for t in self._items.values() if t.enabled)
        return {
            "path": str(self.path),
            "loaded_from": self.loaded_from,
            "total": total,
            "enabled": enabled,
            "groups": len(self.groups()),
            "errors": list(self.load_errors),
        }

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, template_id: object) -> bool:
        with self._lock:
            return template_id in self._items
