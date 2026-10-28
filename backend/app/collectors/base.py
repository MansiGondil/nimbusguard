"""Common data types emitted by every collector."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class Sample:
    device_id: str
    time: datetime
    values: dict[str, float]


@dataclass(slots=True)
class LogEvent:
    time: datetime
    host: str
    severity: int
    message: str
    facility: int = 1
    device_id: str | None = None
    category: str = "system"
    source_ip: str | None = None


@dataclass(slots=True)
class Flow:
    time: datetime
    src_ip: str
    dst_ip: str
    bytes: int
    packets: int
    src_port: int = 0
    dst_port: int = 0
    protocol: int = 6
    device_id: str | None = None


@dataclass
class CollectorBatch:
    samples: list[Sample] = field(default_factory=list)
    logs: list[LogEvent] = field(default_factory=list)
    flows: list[Flow] = field(default_factory=list)


# Metric catalogue: unit + the ceiling used for headroom/health calculations.
METRICS: dict[str, dict] = {
    "cpu": {"unit": "%", "max": 100.0, "label": "CPU"},
    "memory": {"unit": "%", "max": 100.0, "label": "Memory"},
    "disk": {"unit": "%", "max": 100.0, "label": "Disk"},
    "traffic_in": {"unit": "Mbps", "max": None, "label": "Traffic in"},
    "traffic_out": {"unit": "Mbps", "max": None, "label": "Traffic out"},
    "if_errors": {"unit": "err/min", "max": None, "label": "Interface errors"},
    "latency": {"unit": "ms", "max": None, "label": "Latency"},
    "packet_loss": {"unit": "%", "max": 100.0, "label": "Packet loss"},
    "temperature": {"unit": "°C", "max": 85.0, "label": "Temperature"},
}
