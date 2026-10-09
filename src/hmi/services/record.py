"""落盘服务：透传流水、AT/指令记录、遥测 CSV、抓拍与录像。

有一件事决定了这一层的形状：**总线回调跑在发布者的线程里**。485 读线程一发
``passthrough/bytes`` 就直接调进这里，如果回调里同步写文件，一次杀软扫描或网盘
同步就能把分流线程拖住，表现出来是"AT 应答随机丢"——一个极难定位的症状。所以
回调只做一件事：把成品文本塞进队列；真正落盘由 ``Ticker`` 按批完成。队列有上限，
溢出丢最旧并计数，宁可少几条流水也不能吃满内存。

录像优先用 PyAV 编码 mp4（工程已依赖 ``av``）。编码器缺失或打开失败时**退化成
连拍序列目录**而不是直接报错：功能上不丢画面，代价是占空间。退化会写进日志，
用户能从 ``summary()`` 里看到实际用了哪种容器。

抓拍/录像读帧用 ``FrameChannel.peek()``（看不消费），绝不 ``pull()``——
否则会把 UI 该显示的那一帧吃掉，画面卡住不动。
"""

from __future__ import annotations

import csv
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from ..core.config import AppConfig
from ..core.events import EventBus, Events, get_bus
from ..core.logger import get_logger
from ..core.models import AlarmEvent, AtExchange, ByteRecord, CommandResult, LinkSnapshot
from .periodic import Ticker
from .video import VideoService, as_rgb_array, save_frame

__all__ = ["RecordService", "CSV_FIELDS", "SINK_TOPICS", "CSV_TOPICS", "SINK_DIRS"]

#: 遥测 CSV 列（顺序与 ``_snap_row`` 严格一一对应）
CSV_FIELDS: tuple[str, ...] = (
    "ts",
    "time",
    "endpoint",
    "role",
    "connected",
    "rssi_dbm",
    "evm_db",
    "tx_mcs",
    "bandwidth_mhz",
    "freq_mhz",
    "freq_range",
    "chan_list",
    "txpower_dbm",
    "acktmo_us",
    "heart_int_ms",
    "ssid",
    "keymgmt",
    "sta_count",
    "degraded",
    "errors",
)

#: 每类文本流水订阅哪些主题
SINK_TOPICS: dict[str, tuple[str, ...]] = {
    "bytes": (Events.PT_BYTES,),
    "at": (Events.AT_EXCHANGE, Events.CMD_RESULT),
    "events": (Events.TRANSPORT_STATE, Events.ALARM),
}

#: 遥测快照不是文本行，单独走 CSV 行队列
CSV_TOPICS: tuple[str, ...] = (Events.LINK_SNAPSHOT,)

#: 各类流水落在哪个目录段（透传原始字节与遥测留在 byte_dir，其余是日志）
SINK_DIRS: dict[str, str] = {
    "bytes": "byte_dir",
    "at": "log_dir",
    "events": "log_dir",
    "csv": "byte_dir",
}

_QUEUE_CAP = 20000


def _fmt_ts(ts: float | None = None) -> str:
    """毫秒级本地时间串，供流水与 CSV 复用。"""
    ref = time.time() if ts is None else float(ts)
    return (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ref))
        + f".{int((ref % 1) * 1000):03d}"
    )


def _num(value: Any, digits: int = 1) -> Any:
    return "" if value is None else round(float(value), digits)


def _seq(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ";".join(str(v) for v in value)
    return str(value)


def _session_path(directory: Path, base: str, suffix: str) -> Path:
    """同秒内多次开启也不会互相覆盖。"""
    directory.mkdir(parents=True, exist_ok=True)
    for n in range(1000):
        cand = directory / f"{base}{'' if n == 0 else f'_{n}'}{suffix}"
        if not cand.exists():
            return cand
    return directory / f"{base}_{time.time_ns()}{suffix}"


class _Mp4Writer:
    """PyAV 编码器；分辨率中途变化时跳过该帧而不是炸容器。"""

    kind = "mp4"

    def __init__(self, path: Path, *, fps: float) -> None:
        import av

        self._av = av
        self.path = path
        self.frames = 0
        self.skipped = 0
        self._shape: tuple[int, int] | None = None
        self.container = av.open(str(path), "w", format="mp4")
        try:
            self.stream = self.container.add_stream("mpeg4", rate=max(1, int(round(fps))))
            self.stream.pix_fmt = "yuv420p"
        except Exception:
            # 流建不起来就别把容器句柄留给调用方的降级分支去捡
            self.container.close()
            path.unlink(missing_ok=True)
            raise

    def write(self, image: Any, meta: Any = None) -> bool:  # noqa: ARG002
        arr = as_rgb_array(image)
        if arr.ndim != 3 or arr.shape[0] < 2 or arr.shape[1] < 2:
            self.skipped += 1
            return False
        height, width = arr.shape[0] - (arr.shape[0] % 2), arr.shape[1] - (arr.shape[1] % 2)
        arr = arr[:height, :width]
        shape = (int(width), int(height))
        if self._shape is None:
            self.stream.width, self.stream.height = shape
            self._shape = shape
        elif shape != self._shape:
            self.skipped += 1
            return False
        frame = self._av.VideoFrame.from_ndarray(arr, format="rgb24")
        if frame.format.name != "yuv420p":
            frame = frame.reformat(format="yuv420p")
        for packet in self.stream.encode(frame):
            self.container.mux(packet)
        self.frames += 1
        return True

    def close(self) -> None:
        try:
            if self._shape is not None:
                for packet in self.stream.encode():
                    self.container.mux(packet)
        finally:
            self.container.close()


class _SequenceWriter:
    """连拍序列 + index.csv：编码器缺失时的降级容器。"""

    kind = "sequence"

    def __init__(self, directory: Path, *, fps: float) -> None:
        self.path = directory
        self.fps = float(fps)
        self.frames = 0
        self.skipped = 0
        directory.mkdir(parents=True, exist_ok=True)
        self._index = (directory / "index.csv").open("w", encoding="utf-8", newline="")
        self._csv = csv.writer(self._index)
        self._csv.writerow(["index", "file", "ts", "written"])

    def write(self, image: Any, meta: Any = None) -> bool:
        self.frames += 1
        now = time.time()
        try:
            saved = save_frame(image, self.path / f"frame_{self.frames:06d}.jpg")
        except Exception:
            self.skipped += 1
            self.frames -= 1
            raise
        self._csv.writerow([self.frames, saved.name, f"{now:.3f}", saved.suffix])
        return True

    def close(self) -> None:
        self._index.close()


class RecordService:
    """把总线事件与视频帧落成可复盘的文件。"""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        bus: EventBus | None = None,
        video: VideoService | None = None,
        log: logging.Logger | None = None,
        flush_interval_s: float = 1.0,
        snapshot_interval_s: float = 0.0,
        record_fps: float = 10.0,
        queue_cap: int = _QUEUE_CAP,
    ) -> None:
        self.cfg = cfg
        self.bus = bus if bus is not None else get_bus()
        self.log = log or get_logger("hmi.services.record")
        self.video = video
        self.record_fps = max(1.0, float(record_fps))
        self.snapshot_interval_s = max(0.0, float(snapshot_interval_s))
        self.queue_cap = max(100, int(queue_cap))
        self._flush_interval_s = max(0.05, float(flush_interval_s))
        self._lock = threading.Lock()
        self._queues: dict[str, deque[str]] = {name: deque() for name in SINK_TOPICS}
        self._rows: deque[list[Any]] = deque()
        self._handles: dict[str, Any] = {}
        self._tokens: list[int] = []
        self._csv_writer: Any = None
        self._csv_path: Path | None = None
        self._ticker = Ticker("record-flush", self.flush, self._flush_interval_s, log=self.log)
        self._snap_ticker: Ticker | None = None
        self._rec_ticker: Ticker | None = None
        self._writer: _Mp4Writer | _SequenceWriter | None = None
        self._record_path = ""
        self._last_snap_version = -1
        self.started = False
        self.written = 0
        self.rows = 0
        self.snapshots = 0
        self.dropped = 0
        self.errors = 0

    # ------------------------------------------------------------------ 起停
    def start(self, *, topics: dict[str, tuple[str, ...]] | None = None) -> bool:
        """开文件 + 订阅 + 起刷盘节拍；目录不可写时返回 False 而不抛异常。"""
        if self.started:
            return True
        sinks = topics or SINK_TOPICS
        try:
            for name in sinks:
                self._open_sink(name)
            self._open_csv()
        except OSError as exc:
            self.errors += 1
            self.log.error("落盘目录不可用：%s", exc)
            self.stop()
            return False
        for name, names in sinks.items():
            for topic in names:
                self._tokens.append(self.bus.subscribe(topic, self._make_handler(name)))
        if self._csv_writer is not None:
            for topic in CSV_TOPICS:
                self._tokens.append(self.bus.subscribe(topic, self._make_handler("csv")))
        self._ticker.start()
        if self.snapshot_interval_s > 0 and self.video is not None:
            self._snap_ticker = Ticker(
                "record-snapshot",
                self._auto_snapshot,
                self.snapshot_interval_s,
                log=self.log,
            )
            self._snap_ticker.start()
        self.started = True
        self.log.info("落盘开始：%s", ", ".join(str(p) for p in self.paths().values()))
        return True

    def stop(self) -> None:
        self._ticker.stop()
        if self._rec_ticker is not None:
            self.stop_video()
        if self._snap_ticker is not None:
            self._snap_ticker.stop()
            self._snap_ticker = None
        for token in self._tokens:
            self.bus.unsubscribe(token)
        self._tokens.clear()
        self.flush()
        for handle in list(self._handles.values()):
            self._close_handle(handle)
        self._handles.clear()
        if self._csv_writer is not None:
            self._close_handle(self._csv_writer)
            self._csv_writer = None
        self.started = False

    def _make_handler(self, sink: str) -> Any:
        def handler(_topic: str, payload: object) -> None:
            self._capture(sink, payload)

        return handler

    # ------------------------------------------------------------------ 文件
    def _sink_dir(self, name: str) -> Path:
        return self.cfg.abs_dir(SINK_DIRS.get(name, "log_dir"))

    def _open_sink(self, name: str) -> None:
        path = _session_path(
            self._sink_dir(name), f"{name}_{time.strftime('%Y%m%d_%H%M%S')}", ".log"
        )
        self._handles[name] = path.open(
            "w", encoding="utf-8", errors="replace", newline="\n"
        )
        self._handles[f"{name}_path"] = path

    def _open_csv(self) -> None:
        if not bool(self.cfg.record.export_csv):
            return
        self._csv_path = _session_path(
            self._sink_dir("csv"), f"link_{time.strftime('%Y%m%d_%H%M%S')}", ".csv"
        )
        handle = self._csv_path.open("w", encoding="utf-8", newline="")
        writer = csv.writer(handle)
        writer.writerow(CSV_FIELDS)
        handle.flush()
        self._handles["csv_handle"] = handle
        self._csv_writer = writer

    @staticmethod
    def _close_handle(handle: Any) -> None:
        try:
            handle.close()
        except Exception:
            logging.getLogger("hmi.services.record").debug("关闭文件句柄失败", exc_info=True)

    def paths(self) -> dict[str, str]:
        out = {name: str(p) for name, p in self._handles.items() if name.endswith("_path")}
        if self._csv_path is not None:
            out["csv"] = str(self._csv_path)
        if self._record_path:
            out["video"] = self._record_path
        return out

    # ------------------------------------------------------------------ 采集
    def _capture(self, sink: str, payload: object) -> None:
        line = self._format(sink, payload)
        if line is None:
            return
        queue = self._queues.get(sink)
        if queue is None:
            return
        with self._lock:
            queue.append(line)
            while len(queue) > self.queue_cap:
                queue.popleft()
                self.dropped += 1

    def _format(self, sink: str, payload: object) -> str | None:
        if isinstance(payload, LinkSnapshot):
            # 遥测快照是结构化数据，进 CSV 行队列而不是文本流水
            self._enqueue_row(payload)
            return None
        if sink == "bytes" and isinstance(payload, ByteRecord):
            return self._byte_line(payload)
        if sink == "at" and isinstance(payload, AtExchange):
            return self._at_line(payload)
        if sink == "at" and isinstance(payload, CommandResult):
            return self._cmd_line(payload)
        if sink == "events" and isinstance(payload, AlarmEvent):
            return self._alarm_line(payload)
        if sink == "events" and isinstance(payload, dict):
            return self._state_line(payload)
        return None

    @staticmethod
    def _byte_line(rec: ByteRecord) -> str:
        note = f" {rec.note}" if rec.note else ""
        return (
            f"{_fmt_ts(rec.ts)} {rec.direction:>3} {rec.channel or '-'} "
            f"[{rec.kind}]{note} {len(rec.data)}B | {rec.hex()} | {rec.ascii()}"
        )

    @staticmethod
    def _at_line(ex: AtExchange) -> str:
        tail = " / ".join(ex.response) if ex.response else ""
        err = f" ! {ex.error}" if ex.error else ""
        flag = "OK " if ex.ok else "ERR"
        return (
            f"{_fmt_ts(ex.ts)} AT {ex.channel or '-'} [{ex.kind}:{ex.name}] {flag} "
            f"rtt={ex.rtt_ms:.1f}ms | {ex.line} | {tail}{err}"
        )

    @staticmethod
    def _cmd_line(res: CommandResult) -> str:
        flag = "OK " if res.ok else "ERR"
        sent = ByteRecord(data=res.sent_bytes).hex()
        reply = ByteRecord(data=res.reply).hex() if res.reply else "-"
        err = f" ! {res.error}" if res.error else ""
        return (
            f"{_fmt_ts(res.ts)} CMD {res.template_id or '-'} [{res.name}] {flag} "
            f"rtt={res.rtt_ms:.1f}ms | tx={sent} | rx={reply}{err}"
        )

    @staticmethod
    def _alarm_line(ev: AlarmEvent) -> str:
        return f"{_fmt_ts(ev.ts)} ALARM [{ev.level}] {ev.source}/{ev.code} {ev.message}"

    @staticmethod
    def _state_line(payload: dict[str, Any]) -> str:
        return (
            f"{_fmt_ts(payload.get('ts'))} STATE {payload.get('kind')}/{payload.get('name')} "
            f"-> {payload.get('state')} {payload.get('detail') or ''}"
        )

    def _enqueue_row(self, snap: LinkSnapshot) -> None:
        if self._csv_writer is None:
            return
        with self._lock:
            self._rows.append(self._snap_row(snap))
            while len(self._rows) > self.queue_cap:
                self._rows.popleft()
                self.dropped += 1

    def _snap_row(self, snap: LinkSnapshot) -> list[Any]:
        return [
            f"{snap.ts:.3f}",
            _fmt_ts(snap.ts),
            snap.endpoint,
            snap.role or "",
            "" if snap.connected is None else int(bool(snap.connected)),
            _num(snap.rssi_dbm),
            _num(snap.evm_db),
            "" if snap.tx_mcs is None else snap.tx_mcs,
            "" if snap.bandwidth_mhz is None else snap.bandwidth_mhz,
            _num(snap.freq_mhz, 3),
            _seq(snap.freq_range),
            _seq(snap.chan_list),
            _num(snap.txpower_dbm),
            "" if snap.acktmo_us is None else snap.acktmo_us,
            "" if snap.heart_int_ms is None else snap.heart_int_ms,
            snap.ssid or "",
            snap.keymgmt or "",
            snap.sta_count,
            int(bool(snap.degraded)),
            ";".join(snap.errors),
        ]

    # ------------------------------------------------------------------ 刷盘
    def flush(self) -> int:
        """把队列里的行一次性写下去；返回本次写出的行数。"""
        total = 0
        for name, queue in self._queues.items():
            handle = self._handles.get(name)
            if handle is None:
                continue
            with self._lock:
                lines = list(queue)
                queue.clear()
            if not lines:
                continue
            try:
                handle.write("\n".join(lines) + "\n")
                handle.flush()
                total += len(lines)
            except OSError as exc:
                self.errors += 1
                self.dropped += len(lines)
                self.log.error("写 %s 流水失败：%s", name, exc)
        total += self._flush_rows()
        if total:
            self.written += total
        return total

    def _flush_rows(self) -> int:
        if self._csv_writer is None:
            with self._lock:
                self._rows.clear()
            return 0
        with self._lock:
            rows = list(self._rows)
            self._rows.clear()
        if not rows:
            return 0
        try:
            self._csv_writer.writerows(rows)
            self._handles["csv_handle"].flush()
            self.rows += len(rows)
            return len(rows)
        except (OSError, ValueError) as exc:
            self.errors += 1
            self.dropped += len(rows)
            self.log.error("写遥测 CSV 失败：%s", exc)
            return 0

    # ------------------------------------------------------------------ 抓拍
    def capture(self, path: str | Path | None = None, *, label: str = "") -> dict[str, Any]:
        """手动抓拍一帧；帧来自 ``peek()``，不影响界面显示。"""
        if self.video is None:
            return {"ok": False, "detail": "未装配视频服务"}
        res = self.video.snapshot(path)
        if res.get("ok"):
            self.snapshots += 1
            self._capture("events", {"ts": time.time(), "kind": "snapshot", **label_dict(label),
                                     "name": str(res.get("path"))})
        return res

    def _auto_snapshot(self) -> None:
        if self.video is None:
            return
        version = self.video.frames.version
        if version == self._last_snap_version:
            return
        self._last_snap_version = version
        self.capture(label="auto")

    # ------------------------------------------------------------------ 录像
    def start_video(self) -> dict[str, Any]:
        """开始录像；mp4 不可用自动降级为连拍序列。"""
        if self.video is None:
            return {"ok": False, "detail": "未装配视频服务"}
        if self._writer is not None:
            return {"ok": True, "detail": "已在录像", "path": self._record_path}
        directory = self.cfg.abs_dir("video_dir")
        base = f"rec_{time.strftime('%Y%m%d_%H%M%S')}"
        writer: _Mp4Writer | _SequenceWriter
        try:
            writer = _Mp4Writer(
                _session_path(directory, base, ".mp4"), fps=self.record_fps
            )
        except Exception as exc:
            self.log.warning("mp4 容器不可用（%s），改用连拍序列", exc)
            writer = _SequenceWriter(directory / f"{base}_seq", fps=self.record_fps)
        self._writer = writer
        self._record_path = str(writer.path)
        self._rec_ticker = Ticker(
            "record-video", self._pump_frame, 1.0 / self.record_fps, log=self.log
        )
        self._rec_ticker.start()
        return {
            "ok": True,
            "detail": f"录像开始（{writer.kind} @ {self.record_fps:.0f}fps）",
            "path": self._record_path,
        }

    def _pump_frame(self) -> None:
        writer = self._writer
        if writer is None or self.video is None:
            return
        hit = self.video.frames.peek()
        if hit is None:
            writer.skipped += 1
            return
        image, meta, _dwell = hit
        try:
            writer.write(image, meta)
        except Exception as exc:
            self.errors += 1
            self.log.error("写帧失败：%s", exc)

    def stop_video(self) -> dict[str, Any]:
        writer = self._writer
        if writer is None:
            return {"ok": False, "detail": "当前没有录像", "path": self._record_path}
        if self._rec_ticker is not None:
            self._rec_ticker.stop()
            self._rec_ticker = None
        self._writer = None
        try:
            writer.close()
        except Exception as exc:
            self.errors += 1
            self.log.error("收尾录像失败：%s", exc)
        detail = f"{writer.frames} 帧，跳过 {writer.skipped} 帧（{writer.kind}）"
        self._capture(
            "events",
            {
                "ts": time.time(),
                "kind": "record",
                "name": str(writer.path),
                "state": "stopped",
                "detail": detail,
            },
        )
        return {"ok": True, "detail": detail, "path": str(writer.path), "frames": writer.frames}

    @property
    def recording(self) -> bool:
        return self._writer is not None

    # ------------------------------------------------------------------ 汇总
    def summary(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "written": self.written,
            "rows": self.rows,
            "snapshots": self.snapshots,
            "dropped": self.dropped,
            "errors": self.errors,
            "pending": {**{name: len(q) for name, q in self._queues.items()},
                        "csv": len(self._rows)},
            "recording": self.recording,
            "record_fps": self.record_fps,
            "paths": self.paths(),
            "ticker": self._ticker.stats(),
        }

    def __repr__(self) -> str:
        return f"<RecordService written={self.written} rows={self.rows} rec={self.recording}>"


def label_dict(label: str) -> dict[str, str]:
    return {"label": label} if label else {}
