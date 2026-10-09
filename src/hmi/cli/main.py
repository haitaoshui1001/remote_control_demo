"""命令行入口：无硬件自检、配置落盘、模板检查、端口探测与界面启动。

CLI 排在图形界面之前是有原因的。自检脚本要在**没插串口线、没接摄像头**的机器上，
用仿真源把五类服务按真实顺序跑一遍：等界面出来时，每个按钮背后的路径都已经被程序
验证过，剩下的只是"显示得够不够清楚"。

这里不做任何业务判断，只负责三件事：解析参数、按序调用 services、把结论打成人能读的
报告。所以每个检查段都是一个独立函数，出问题时能一眼定位到是哪一段。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from ..core.config import load_config, project_root
from ..core.logger import setup_logging

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NOT_READY = 3


# ---------------------------------------------------------------------- 报告
def _width(text: str) -> int:
    """中文按两格算，否则表格在终端里必然错位。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, dict | list | tuple | set):
        return json.dumps(list(value) if isinstance(value, set) else value,
                          ensure_ascii=False, default=str)
    return str(value)


def _num(value: object, digits: int = 1) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, int | float):
        return f"{value:.{digits}f}" if isinstance(value, float) else str(value)
    return str(value)


class Report:
    """自检记录器：先攒结论，最后统一渲染。

    中途抛异常也必须出报告，所以渲染放在调用方的收尾里——否则跑到第 6 步崩了，
    前 5 步的结论会跟着一起丢，那是最难排查的一种失败。
    """

    def __init__(self) -> None:
        self.items: list[tuple[str, str, str]] = []

    def section(self, title: str) -> None:
        self.items.append(("SECTION", title, ""))

    def info(self, name: str, detail: object = "") -> None:
        self.items.append(("INFO", name, _text(detail)))

    def skip(self, name: str, detail: object = "") -> None:
        self.items.append(("SKIP", name, _text(detail)))

    def fail(self, name: str, detail: object = "") -> None:
        self.items.append(("FAIL", name, _text(detail)))

    def check(self, name: str, passed: object, detail: object = "") -> bool:
        self.items.append(("PASS" if passed else "FAIL", name, _text(detail)))
        return bool(passed)

    @property
    def failures(self) -> list[tuple[str, str, str]]:
        return [it for it in self.items if it[0] == "FAIL"]

    def counts(self) -> dict[str, int]:
        out = {"PASS": 0, "FAIL": 0, "SKIP": 0, "INFO": 0}
        for status, _, _ in self.items:
            out[status] = out.get(status, 0) + 1
        return out

    def render(self, *, as_json: bool = False) -> int:
        if as_json:
            print(json.dumps(
                {
                    "counts": self.counts(),
                    "items": [{"status": s, "name": n, "detail": d}
                              for s, n, d in self.items if s != "SECTION"],
                },
                ensure_ascii=False,
                indent=2,
            ))
            return EXIT_FAILED if self.failures else EXIT_OK
        width = max((_width(n) for _, n, _ in self.items if n), default=8)
        print("\n" + "=" * 72)
        for status, name, detail in self.items:
            if status == "SECTION":
                print(f"\n{name}")
                continue
            print(f"  [{status}] {_pad(name, width)}  {detail}")
        c = self.counts()
        print("\n" + "-" * 72)
        print(f"  合计：通过 {c['PASS']}，失败 {c['FAIL']}，跳过 {c['SKIP']}，说明 {c['INFO']}")
        if self.failures:
            print("  需要处理：")
            for _, name, detail in self.failures:
                print(f"    · {name} —— {detail}")
        print("=" * 72)
        return EXIT_FAILED if self.failures else EXIT_OK


def _parse_kv(pairs: list[str] | None) -> dict[str, Any]:
    """把 ``--set serial.port=COM7`` 这类参数变成点号路径覆盖字典。"""
    out: dict[str, Any] = {}
    for item in pairs or []:
        if "=" not in item:
            continue
        key, _, raw = item.partition("=")
        out[key.strip()] = raw.strip()
    return out


# ------------------------------------------------------------------ selfcheck
def _check_startup(rep: Report, session: Any, started: dict[str, Any]) -> None:
    rep.section("1 装配与启动")
    link = session.link
    rep.check("控制通道", bool(started.get("link")),
              f"{link.channel} · {link.state.value}")
    rep.check("透传分流", bool(started.get("passthrough")), "AT 与非 AT 各有唯一读者")
    rep.check("遥测轮询", bool(started.get("polling")), "通道自带节拍线程")
    if session.video is None:
        rep.skip("视频取流", "按参数未装配视频服务")
    else:
        rep.check("视频取流", bool(started.get("video")), session.video.name)
    if session.record is None:
        rep.skip("流水落盘", "按参数未装配落盘服务")
    else:
        names = "、".join(Path(p).name for p in session.record.paths().values())
        rep.check("流水落盘", bool(started.get("record")), names)
    if session.motion is None:
        rep.skip("运动控制", "按参数未装配运动服务")
    elif not session.cfg.motion.enabled:
        rep.skip("运动控制", "motion.enabled=False")
    else:
        rep.check("运动控制", bool(started.get("motion")),
                  f"周期重发 {session.cfg.motion.send_period_ms}ms")
    for line in started.get("detail", []):
        rep.info("启动提示", line)


def _check_at(rep: Report, session: Any, args: argparse.Namespace) -> None:
    rep.section("2 AT 指令与信道遥测")
    radio = session.radio
    read = radio.read_many()
    rep.check("批量读取", bool(read["ok"]), read["detail"])
    ver = radio.read_line("VERSION")
    if ver["ok"]:
        lines = ver["response"] or [ver["line"]]
        rep.check("固件版本", True, str(lines[0]))
    else:
        rep.fail("固件版本", ver["detail"])
    snap = session.link.poll_once()
    if snap is None:
        rep.fail("链路快照", "poll_once 没有任何返回，检查轮询清单与通道")
    else:
        rep.check("链路快照", True,
                  f"RSSI {_num(snap.rssi_dbm)}dBm / EVM {_num(snap.evm_db)}dB / "
                  f"连接 {'是' if snap.connected else '否'} / 从机 {_num(snap.sta_count)} / "
                  f"频点 {_num(snap.freq_mhz, 3)}MHz")
    # 丢掉刚才读回的一整屏参数再改，否则 apply() 会把读到的每个字段都写回模块。
    radio.load_defaults()
    want = int(args.txpower)
    radio.params.txpower = want
    plan = radio.plan()
    rep.check("下发预览", bool(plan["ok"]),
              f"{plan['detail']}：{' | '.join(plan['lines'][:3])}")
    if not (session.mock or args.apply):
        rep.skip("参数下发", "实机默认只预览不写硬件（改功率直接影响链路余量），确认后再加 --apply")
        rep.skip("预览即所得", "同上")
        rep.skip("回读一致", "同上")
    else:
        applied = radio.apply()
        rep.check("参数下发", bool(applied["ok"]), applied["detail"])
        online = [r["line"] for r in applied["results"]]
        rep.check("预览即所得", bool(online) and online == plan["lines"],
                  f"预览 {plan['lines'][:2]} / 上线 {online[:2]}")
        back = radio.read("TXPOWER")
        rep.check("回读一致", bool(back.get("ok")) and back.get("value") == want,
                  f"设定 {want} → 模块回读 {back.get('value')}")
        act = radio.action("SCAN_AP", 1)
        rep.info("动作指令", "SCAN_AP：" + (_text(act.get("detail")) or "已试跑"))
    rep.info("未测指令", "LOADDEF（恢复出厂）自检绝不碰——真机上它会清掉整台模块的配置")


def _check_passthrough(rep: Report, session: Any) -> None:
    rep.section("3 485 透传与指令模板")
    pt = session.passthrough
    sent = pt.send("01 03 00 00 00 01")
    rep.check("只发不等", bool(sent["ok"]), f"{sent['detail']} {sent['hex']}")
    res = pt.request("01 03 00 00 00 01", name="selfcheck")
    if res.ok:
        rep.check("发并等应答", True,
                  f"应答 {len(res.reply)} 字节 / 往返 {res.rtt_ms:.0f}ms / "
                  f"匹配 {'是' if res.matched else '否'}")
    else:
        rep.fail("发并等应答", res.error or "超时无应答")
    lib = pt.templates
    s = lib.summary()
    rep.check("模板库", s["total"] > 0 and not s["errors"],
              f"{s['total']} 条 / {s['groups']} 组 · {Path(s['path']).name}"
              + (f"（{_text(s['loaded_from'])}）" if s["loaded_from"] else ""))
    run = pt.run_template("modbus_read_holding")
    rep.check("模板下发", bool(run.ok),
              f"{run.name} 应答 {len(run.reply)} 字节" if run.ok else run.error)
    pv = pt.preview("generic_on", {"reg": "0001", "ch": "1"})
    rep.check("模板预览", bool(pv["ok"]), f"{pv['detail']} {pv['hex']}")
    rep.info("透传计数", f"成功 {pt.sent} / 失败 {pt.failed}")


def _check_media(rep: Report, session: Any, args: argparse.Namespace) -> None:
    rep.section("4 视频、抓拍与录像")
    video = session.video
    record = session.record
    if video is None:
        rep.skip("取帧", "未装配视频服务")
        return
    got = video.wait_for_frame(args.frame_timeout)
    st = video.stats()
    rep.check("取帧", got,
              f"{st.resolution or '未知分辨率'} / 解码 {st.fps_decode:.1f}fps / "
              f"累计 {st.frames_decoded} 帧")
    if record is None:
        rep.skip("抓拍与录像", "未装配落盘服务")
        return
    cap = record.capture(label="selfcheck")
    rep.check("抓拍", bool(cap.get("ok")),
              str(cap.get("path")) if cap.get("ok") else _text(cap.get("detail")))
    begin = record.start_video()
    rep.check("录像启动", bool(begin.get("ok")), _text(begin.get("detail")))
    if begin.get("ok"):
        time.sleep(max(0.2, args.seconds))
        end = record.stop_video()
        target = Path(str(end.get("path", "")))
        rep.check("录像收尾", bool(end.get("ok")), _text(end.get("detail")))
        rep.check("录像文件", target.exists(), str(target))


def _check_files(rep: Report, session: Any) -> None:
    rep.section("5 落盘核对")
    record = session.record
    if record is None:
        rep.skip("落盘核对", "未装配落盘服务")
        return
    record.flush()
    s = record.summary()
    rep.check("写入行数", s["written"] > 0,
              f"文本+CSV 共 {s['written']} 行，其中遥测 {s['rows']} 行")
    rep.check("队列无积压", sum(s["pending"].values()) == 0, _text(s["pending"]))
    rep.check("未丢数据", s["dropped"] == 0 and s["errors"] == 0,
              f"丢弃 {s['dropped']} / 写盘错误 {s['errors']}")
    # video/csv/events_path 允许为空：没录像、没遥测帧、没告警时它们本来就不该有内容。
    optional = ("video", "csv", "events_path")
    empty: list[str] = []
    for name, raw in record.paths().items():
        p = Path(raw)
        if not p.exists():
            rep.fail("文件缺失", f"{name} -> {p}")
            continue
        size = p.stat().st_size if p.is_file() else sum(
            f.stat().st_size for f in p.glob("*") if f.is_file()
        )
        rep.info(f"产物 {name}", f"{size} 字节")
        if not size and name not in optional:
            empty.append(name)
    if len(empty) == len([n for n in record.paths() if n not in optional]):
        rep.check("流水非空", False, f"这些流水一个字都没写：{'、'.join(empty) or '无'}")
    else:
        rep.check("流水非空", not empty, "、".join(empty) or "各类流水均已落盘")


def _check_channel_switch(rep: Report, session: Any) -> None:
    rep.section("6 通道热切换")
    if not session.mock:
        rep.skip("通道切换", "实机模式不动通道，避免打断在跑的链路")
        return
    # reopen=False 是故意把链路关死，好验证下一段"从关到开"真的能重建传输层。
    off = session.link.set_channel("udp", reopen=False)
    # 仿真模式下 serial/udp 都会被换成假电台，通道名必须落地成 mock——
    # 真通道（COM 口、真网络探测）只属于实机，否则仿真自检会去广播找模块。
    rep.check("切到 UDP（仿真应换装假电台）", bool(off.get("ok"))
              and off.get("channel") == "mock" and not session.link.is_open,
              _text(off.get("detail")))
    back = session.link.set_channel("serial", reopen=True)
    rep.check("切回仿真", bool(back.get("open")) and back.get("channel") == "mock"
              and session.link.is_open, _text(back.get("detail")))
    rep.check("轮询已恢复", session.link.polling,
              "切换后遥询线程重新跑起来，信道面板不会冻在旧快照上")
    snap = session.link.poll_once()
    rep.check("切换后仍可遥测", snap is not None,
              "RSSI 已取回" if snap is not None else "切换后一次遥测都没读到")


def cmd_selfcheck(args: argparse.Namespace) -> int:
    """按依赖顺序把五类服务跑一遍，全部通过才给 0 退出码。"""
    from ..services import build_session

    rep = Report()
    session = build_session(
        args.config,
        mock=not args.real,
        video=not args.no_video,
        record=not args.no_record,
        endpoint=args.endpoint,
        **_parse_kv(args.set),
    )
    cfg = session.cfg
    setup_logging(args.log_level or cfg.app.log_level,
                  log_dir=str(project_root() / cfg.app.log_dir), console=False)
    rep.section("0 运行环境")
    rep.info("版本", _version())
    rep.info("配置", cfg.meta.path or "内置默认（未找到 config/app.yaml）")
    rep.info("模式", "仿真（无硬件）" if session.mock else "实机")
    rep.check("配置合法性", not cfg.meta.warnings,
              "；".join(str(w) for w in cfg.meta.warnings[:3]) or "无未知配置项")
    try:
        started = session.start(record=not args.no_record, video=not args.no_video)
        _check_startup(rep, session, started)
        _check_at(rep, session, args)
        _check_passthrough(rep, session)
        _check_media(rep, session, args)
        _check_files(rep, session)
        _check_channel_switch(rep, session)
    except Exception as exc:
        rep.fail("未捕获异常", f"{type(exc).__name__}: {exc}")
        if args.traceback:
            raise
    finally:
        _finish(rep, session)
    if args.summary:
        print(json.dumps(session.summary(), ensure_ascii=False, indent=2, default=str))
    return rep.render(as_json=args.json)


def _finish(rep: Report, session: Any) -> None:
    """停两遍：第二遍专门验证 ``stop()`` 幂等（关窗与异常退出会走两次）。"""
    try:
        session.stop()
        leaked = session.link.is_open
        session.stop()
        rep.check("停止与幂等", not leaked and not session.started,
                  "重复停止不报错，通道与节拍线程都已收干净")
    except Exception as exc:
        rep.fail("停止", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------- config
def cmd_config(args: argparse.Namespace) -> int:
    """打印生效配置；``--init`` 时把它写成 ``config/app.yaml``。"""
    cfg = load_config(args.config, **_parse_kv(args.set))
    for line in cfg.summary():
        print(line)
    if cfg.meta.warnings:
        print("\n配置警告：")
        for w in cfg.meta.warnings:
            print(f"  · {_text(w)}")
    if args.init:
        target = cfg.save()
        print(f"已写出默认配置：{target}")
    if args.verbose:
        print(json.dumps(json.loads(json.dumps(_as_dict(cfg), ensure_ascii=False, default=str)),
                         ensure_ascii=False, indent=2))
    return EXIT_OK


def _as_dict(cfg: Any) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(cfg)


def cmd_templates(args: argparse.Namespace) -> int:
    """列出指令模板；给了 ``--value`` 就顺带做一次不上线的校验。"""
    from ..protocol.templates import TemplateLibrary

    values = _parse_kv(args.value)
    lib = TemplateLibrary(path=Path(args.file) if args.file else None)
    s = lib.summary()
    print(f"模板文件：{s['path']}（来源：{_text(s['loaded_from']) or '未知'}）")
    print(f"共 {s['total']} 条，启用 {s['enabled']} 条，分组 {s['groups']} 个")
    if s["errors"]:
        print("加载问题：")
        for err in s["errors"]:
            print(f"  · {_text(err)}")
    bad = 0
    for tpl in lib.ordered():
        problems = lib.validate(tpl, values)
        bad += 1 if problems else 0
        mark = "？" if problems else "✓"
        print(f"  [{mark}] {tpl.group:<10} {tpl.id:<22} {tpl.name}  "
              f"({tpl.mode}{'/CRC' if tpl.crc16_append else ''}"
              f"{'/等应答' if tpl.expect_reply else '/不等'})")
        if problems and args.verbose:
            for p in problems:
                print(f"        · {p}")
    print("\n提示：带占位符的模板需要 --value reg=0001 这种形式才判定得完整。")
    return EXIT_FAILED if (s["errors"] or bad > s["total"]) else EXIT_OK


def cmd_ports(args: argparse.Namespace) -> int:
    """列出候选串口与本机网口，给"第一次接线"用。"""
    from ..transport import list_local_ips, list_serial_ports

    ports = list_serial_ports()
    print(f"串口（{len(ports)} 个）：")
    if not ports:
        print("  · 未发现串口。检查 485 转 USB 驱动（CH340/CP210x），或先跑仿真自检")
    for p in ports:
        print(f"  · {str(p.get('device')):<10} {p.get('description')}"
              f"{'  [USB转串口]' if p.get('likely_usb_uart') else ''}")
    ips = list_local_ips()
    print(f"\n本机网口 IP（{len(ips)} 个）：")
    for ip in ips:
        print(f"  · {ip}")
    if args.probe:
        if not ports:
            print("\n没有候选串口，猜不了。")
            return EXIT_OK
        from ..transport import guess_port

        cfg = load_config(args.config, **_parse_kv(args.set))
        picked = guess_port(cfg.serial, ports)
        hint = "不确定，请在 config/app.yaml 里手写 serial.port"
        print(f"\n最像 485 转 USB 的口：{picked or hint}")
        if picked:
            print(f"试一下：python run.py selfcheck --real --set serial.port={picked}")
    return EXIT_OK


def cmd_gui(args: argparse.Namespace) -> int:
    """启动 PyQt5 界面；界面未就绪时给出明确指引而不是抛栈。"""
    try:
        from ..ui.app import run_app
    except ImportError as exc:
        print("图形界面尚未就绪（属于步骤 6）。当前可以先跑：")
        print("  python run.py selfcheck        # 无硬件全链路自检")
        print("  python run.py config --init    # 生成 config/app.yaml")
        print(f"\n（导入详情：{exc}）")
        return EXIT_NOT_READY
    overrides = _parse_kv(args.set)
    if getattr(args, "font_size", None):
        overrides["ui.font_size"] = args.font_size
    cfg = load_config(args.config, **overrides)
    setup_logging(args.log_level or cfg.app.log_level,
                  log_dir=str(project_root() / cfg.app.log_dir))
    return int(run_app(args.config, mock=not args.real if args.real else None,
                       **overrides))


# ---------------------------------------------------------------------- 装配
def _version() -> str:
    from .. import __version__

    return __version__


def _add_common(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--config", help="配置文件路径，默认 config/app.yaml")
    sub.add_argument("--set", action="append", metavar="K=V",
                     help="临时覆盖配置项（点号路径），可重复")
    sub.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                     help="覆盖配置里的日志级别")
    sub.add_argument("--verbose", "-v", action="store_true", help="输出更多细节")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="EWD611 数传图传上位机命令行入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="不带子命令时等价于 `run.py selfcheck`，例如：python run.py --seconds 2",
    )
    p.add_argument("--version", action="version", version=f"hmi {_version()}")
    sub = p.add_subparsers(dest="command")

    chk = sub.add_parser("selfcheck", help="无硬件自检（默认走仿真源）")
    _add_common(chk)
    chk.add_argument("--real", action="store_true", help="用真实硬件而不是仿真源")
    chk.add_argument("--endpoint", default="local", help="本端标识，写进日志与快照")
    chk.add_argument("--txpower", type=float, default=17.0, help="自检下发的发射功率")
    chk.add_argument("--apply", action="store_true",
                     help="实机模式也真写参数（默认只预览，避免自检改动在跑的链路）")
    chk.add_argument("--seconds", type=float, default=1.0, help="录像持续秒数")
    chk.add_argument("--frame-timeout", type=float, default=3.0, help="等首帧超时秒数")
    chk.add_argument("--no-video", action="store_true", help="跳过视频与落盘影像检查")
    chk.add_argument("--no-record", action="store_true", help="跳过流水/抓拍/录像")
    chk.add_argument("--json", action="store_true", help="以 JSON 输出结论，便于 CI 收集")
    chk.add_argument("--summary", action="store_true", help="额外打印各服务汇总字典")
    chk.add_argument("--traceback", action="store_true", help="异常直接抛出，保留调用栈")
    chk.set_defaults(func=cmd_selfcheck)

    cfg = sub.add_parser("config", help="查看生效配置 / 生成 config/app.yaml")
    _add_common(cfg)
    cfg.add_argument("--init", action="store_true", help="把当前配置写成 YAML 文件")
    cfg.set_defaults(func=cmd_config)

    tpl = sub.add_parser("templates", help="列出并校验 485 指令模板")
    _add_common(tpl)
    tpl.add_argument("--file", help="模板文件路径，默认 config/templates.yaml")
    tpl.add_argument("--value", action="append", metavar="K=V",
                     help="给占位符赋值，可重复")
    tpl.set_defaults(func=cmd_templates)

    prt = sub.add_parser("ports", help="列出候选串口与本机网口")
    _add_common(prt)
    prt.add_argument("--probe", action="store_true", help="按 VID 猜哪个口最像 485 转 USB")
    prt.set_defaults(func=cmd_ports)

    gui = sub.add_parser("gui", help="启动图形界面")
    _add_common(gui)
    gui.add_argument("--real", action="store_true", help="用真实硬件启动")
    gui.add_argument("--font-size", type=int, metavar="PX",
                     help="界面正文字号（像素），覆盖配置里的 ui.font_size")
    gui.set_defaults(func=cmd_gui)
    return p


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw:
        raw = ["selfcheck"]
    args = build_parser().parse_args(raw)
    if getattr(args, "func", None) is None:
        build_parser().print_help()
        return EXIT_OK
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
