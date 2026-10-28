"""NetFlow v5 collector: UDP listener + packet decoder."""

import asyncio
import ipaddress
import logging
import struct
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.collectors.base import Flow

log = logging.getLogger(__name__)

HEADER = struct.Struct("!HHIIIIBBH")  # 24 bytes
RECORD = struct.Struct("!IIIHHIIIIHHBBBBHHBBH")  # 48 bytes


def _ip(n: int) -> str:
    return str(ipaddress.IPv4Address(n))


def decode_v5(packet: bytes) -> list[Flow]:
    if len(packet) < HEADER.size:
        raise ValueError("packet too short")
    version, count, _uptime, secs, _nsecs, _seq, _etype, _eid, _sampling = HEADER.unpack_from(packet)
    if version != 5:
        raise ValueError(f"unsupported NetFlow version {version}")
    if len(packet) < HEADER.size + count * RECORD.size:
        raise ValueError("truncated packet")
    ts = datetime.fromtimestamp(secs, UTC)
    flows = []
    for i in range(count):
        r = RECORD.unpack_from(packet, HEADER.size + i * RECORD.size)
        src, dst, _nexthop, _in, _out, pkts, octets, _first, _last, sport, dport, _pad, _flags, proto = r[:14]
        flows.append(
            Flow(ts, _ip(src), _ip(dst), bytes=octets, packets=pkts, src_port=sport, dst_port=dport, protocol=proto)
        )
    return flows


def encode_v5(flows: list[Flow], unix_secs: int) -> bytes:
    """Build a NetFlow v5 packet (used by tests and the traffic replay tool)."""
    out = HEADER.pack(5, len(flows), 0, unix_secs, 0, 0, 0, 0, 0)
    for f in flows:
        out += RECORD.pack(
            int(ipaddress.IPv4Address(f.src_ip)),
            int(ipaddress.IPv4Address(f.dst_ip)),
            0,
            0,
            0,
            f.packets,
            f.bytes,
            0,
            0,
            f.src_port,
            f.dst_port,
            0,
            0,
            f.protocol,
            0,
            0,
            0,
            0,
            0,
            0,
        )
    return out


class _NetflowProtocol(asyncio.DatagramProtocol):
    def __init__(self, handler: Callable[[list[Flow]], Awaitable[None]]) -> None:
        self.handler = handler

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            flows = decode_v5(data)
        except ValueError as exc:
            log.debug("Dropping NetFlow datagram from %s: %s", addr, exc)
            return
        asyncio.get_running_loop().create_task(self.handler(flows))


async def serve(port: int, handler: Callable[[list[Flow]], Awaitable[None]], host: str = "0.0.0.0"):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: _NetflowProtocol(handler), local_addr=(host, port))
    log.info("NetFlow v5 collector listening on udp/%s", port)
    return transport
