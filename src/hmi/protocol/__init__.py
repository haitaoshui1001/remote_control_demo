"""协议层：AT 编解码/客户端、共享通道分流、参数注册表、信道遥测、485 透传、指令模板。

这一层对上只给"参数名 + 值 + 快照 + 记录"，绝不给字节；UI 与 services 因此
不需要知道 485 上到底怎么一问一答。
"""

from .at_client import AtClient, AtError, response_value
from .at_codec import (
    CHUNK_AT,
    CHUNK_PASS,
    AtLine,
    LineSplitter,
    classify,
    classify_chunk,
    format_at,
    normalize_name,
    parse_bool_state,
    parse_float,
    parse_int,
    parse_int_list,
    parse_kv_pairs,
    split_command,
    split_value_line,
)
from .channel import DEFAULT_AT_MARKERS, ChannelDemux
from .param_registry import (
    GROUPS,
    ParamError,
    ParamSpec,
    all_specs,
    apply_to_params,
    by_group,
    decode,
    encode,
    readable_specs,
    resolve_key,
    spec_for,
    summary_rows,
    validate,
    writable_specs,
)
from .passthrough import PassthroughEngine, build_frame, reply_matches
from .telemetry import (
    LinkMonitor,
    build_snapshot,
    evaluate,
    loss_percent,
    params_view,
    parse_sta_rows,
)
from .telemetry_frames import (
    AsciiLineParser,
    BinaryFrameParser,
    TelemetryFrameParser,
    TelemetrySample,
    WheeltecFrameParser,
    build_speed_frame,
    create_parser,
    describe_wheeltec_frame,
    encode_ascii_line,
    encode_binary_frame,
    encode_wheeltec_frame,
)
from .templates import (
    TemplateLibrary,
    default_templates,
    missing_placeholders,
    render_payload,
)

__all__ = [
    # at_codec
    "AtLine",
    "LineSplitter",
    "classify",
    "classify_chunk",
    "format_at",
    "normalize_name",
    "parse_bool_state",
    "parse_float",
    "parse_int",
    "parse_int_list",
    "parse_kv_pairs",
    "split_command",
    "split_value_line",
    "CHUNK_AT",
    "CHUNK_PASS",
    # channel
    "ChannelDemux",
    "DEFAULT_AT_MARKERS",
    # at_client
    "AtClient",
    "AtError",
    "response_value",
    # param_registry
    "ParamSpec",
    "ParamError",
    "GROUPS",
    "all_specs",
    "by_group",
    "spec_for",
    "resolve_key",
    "readable_specs",
    "writable_specs",
    "validate",
    "encode",
    "decode",
    "apply_to_params",
    "summary_rows",
    # telemetry
    "LinkMonitor",
    "build_snapshot",
    "evaluate",
    "loss_percent",
    "parse_sta_rows",
    "params_view",
    # passthrough
    "PassthroughEngine",
    "build_frame",
    "reply_matches",
    # telemetry_frames
    "TelemetrySample",
    "TelemetryFrameParser",
    "BinaryFrameParser",
    "AsciiLineParser",
    "WheeltecFrameParser",
    "build_speed_frame",
    "create_parser",
    "describe_wheeltec_frame",
    "encode_binary_frame",
    "encode_ascii_line",
    "encode_wheeltec_frame",
    # templates
    "TemplateLibrary",
    "default_templates",
    "render_payload",
    "missing_placeholders",
]
