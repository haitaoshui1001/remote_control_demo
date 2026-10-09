"""仿真层：无硬件时替换 transport 层的真实实现，接口签名保持一致。"""

from .radio import RO, RW, MockByteTransport, MockRadioModule, build_pair
from .video import MockVideoSource

__all__ = [
    "MockRadioModule",
    "MockByteTransport",
    "MockVideoSource",
    "build_pair",
    "RO",
    "RW",
]
