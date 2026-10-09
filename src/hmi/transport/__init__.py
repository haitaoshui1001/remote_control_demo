"""传输层：对上只暴露"字节通道 / 帧通道"两类接口，屏蔽串口、UDP、RTSP 差异。"""

from .base import ByteTransport, FrameChannel
from .rtsp import RtspVideoSource
from .serial_transport import SerialTransport, guess_port, list_serial_ports
from .udp_at import UdpAtTransport, list_local_ips

__all__ = [
    "ByteTransport",
    "FrameChannel",
    "RtspVideoSource",
    "SerialTransport",
    "UdpAtTransport",
    "list_serial_ports",
    "guess_port",
    "list_local_ips",
]
