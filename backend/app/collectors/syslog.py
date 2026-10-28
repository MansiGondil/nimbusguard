"""Syslog collector: UDP listener that parses RFC 3164 and RFC 5424 messages."""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.collectors.base import LogEvent

log = logging.getLogger(__name__)

_PRI = re.compile(r"^<(?P<pri>\d{1,3})>(?P<rest>.*)$", re.S)
_RFC5424 = re.compile(
    r"^1 (?P<ts>\S+) (?P<host>\S+) (?P<app>\S+) (?P<proc>\S+) (?P<msgid>\S+) "
    r"(?P<sd>-|\[.*?\]) ?(?P<msg>.*)$",
    re.S,
)
_RFC3164 = re.compile(r"^(?P<ts>[A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d) (?P<host>\S+) (?P<msg>.*)$", re.S)
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

CATEGORIES = [
    ("auth", re.compile(r"fail(ed)? (login|password)|authentication fail|invalid user|login fail", re.I)),
    ("auth", re.compile(r"logged in|login success|accepted password", re.I)),
    ("config", re.compile(r"config(uration)? (saved|changed)|%SYS-5-CONFIG", re.I)),
    ("interface", re.compile(r"interface .* (down|up)|link(-| )(down|up)|CRC|%LINK-|%LINEPROTO-", re.I)),
    ("security", re.compile(r"attack|intrusion|denied|blocked|malware", re.I)),
]


def categorize(message: str) -> str:
    for name, pattern in CATEGORIES:
        if pattern.search(message):
            return name
    return "system"


def parse(data: bytes | str, source_ip: str | None = None, now: datetime | None = None) -> LogEvent:
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    text = text.strip()
    now = now or datetime.now(UTC)
    pri, rest = 13, text  # default: user.notice
    if m := _PRI.match(text):
        pri, rest = int(m["pri"]), m["rest"]
    facility, severity = divmod(pri, 8)
    host, msg, ts = source_ip or "unknown", rest, now

    if m := _RFC5424.match(rest):
        host, msg = m["host"], m["msg"]
        try:
            ts = datetime.fromisoformat(m["ts"].replace("Z", "+00:00"))
        except ValueError:
            pass
    elif m := _RFC3164.match(rest):
        host, msg = m["host"], m["msg"]
        try:
            ts = datetime.strptime(f"{now.year} {m['ts']}", "%Y %b %d %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            pass

    ips = _IP.findall(msg)
    return LogEvent(
        time=ts,
        host=host,
        severity=severity,
        facility=facility,
        message=msg.strip(),
        category=categorize(msg),
        source_ip=ips[-1] if ips else None,
    )


class _SyslogProtocol(asyncio.DatagramProtocol):
    def __init__(self, handler: Callable[[LogEvent], Awaitable[None]]) -> None:
        self.handler = handler

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            event = parse(data, source_ip=addr[0])
        except Exception:  # pragma: no cover - defensive
            log.exception("Unparseable syslog datagram from %s", addr)
            return
        asyncio.get_running_loop().create_task(self.handler(event))


async def serve(port: int, handler: Callable[[LogEvent], Awaitable[None]], host: str = "0.0.0.0"):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: _SyslogProtocol(handler), local_addr=(host, port))
    log.info("Syslog collector listening on udp/%s", port)
    return transport
