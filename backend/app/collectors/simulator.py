"""Network simulator: realistic, seasonal telemetry with injectable faults.

The simulator stands in for real SNMP/NetFlow/Syslog sources so the whole
platform can be demoed (and evaluated) without hardware. It emits the same
``Sample``/``LogEvent``/``Flow`` objects as the real collectors, and acts as
the remediation "driver" so playbooks have a visible effect on the network.
"""

import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.collectors.base import CollectorBatch, Flow, LogEvent, Sample

# (base, business-hours amplitude, noise sigma). Traffic values are
# fractions of link capacity.
PROFILES: dict[str, dict[str, tuple[float, float, float]]] = {
    "router": {
        "cpu": (18, 32, 2.5),
        "memory": (45, 8, 1.2),
        "traffic_in": (0.12, 0.33, 0.015),
        "traffic_out": (0.08, 0.22, 0.012),
        "if_errors": (0.2, 0.3, 0.25),
        "latency": (3, 2, 0.4),
        "packet_loss": (0.02, 0.03, 0.015),
        "temperature": (42, 6, 0.6),
    },
    "firewall": {
        "cpu": (14, 30, 2.5),
        "memory": (50, 8, 1.2),
        "traffic_in": (0.10, 0.30, 0.015),
        "traffic_out": (0.07, 0.20, 0.012),
        "if_errors": (0.1, 0.2, 0.2),
        "latency": (2, 1.5, 0.3),
        "packet_loss": (0.02, 0.02, 0.01),
        "temperature": (44, 5, 0.6),
    },
    "switch": {
        "cpu": (9, 14, 1.5),
        "memory": (35, 5, 0.8),
        "traffic_in": (0.08, 0.30, 0.015),
        "traffic_out": (0.08, 0.28, 0.015),
        "if_errors": (0.2, 0.3, 0.25),
        "latency": (1, 0.5, 0.15),
        "packet_loss": (0.01, 0.02, 0.01),
        "temperature": (38, 4, 0.5),
    },
    "access_point": {
        "cpu": (8, 18, 1.8),
        "memory": (30, 10, 1.2),
        "traffic_in": (0.05, 0.35, 0.02),
        "traffic_out": (0.04, 0.20, 0.015),
        "if_errors": (0.3, 0.5, 0.3),
        "latency": (4, 3, 0.6),
        "packet_loss": (0.1, 0.2, 0.05),
        "temperature": (40, 5, 0.6),
    },
    "server": {
        "cpu": (14, 33, 3.0),
        "memory": (55, 9, 1.2),
        "disk": (54, 0.5, 0.08),
        "traffic_in": (0.04, 0.18, 0.01),
        "traffic_out": (0.06, 0.25, 0.012),
        "if_errors": (0.05, 0.1, 0.1),
        "latency": (1, 0.5, 0.15),
        "packet_loss": (0.01, 0.01, 0.005),
        "temperature": (45, 8, 0.8),
    },
    "cloud": {
        "cpu": (22, 28, 3.0),
        "memory": (50, 10, 1.5),
        "traffic_in": (0.05, 0.20, 0.01),
        "traffic_out": (0.05, 0.20, 0.01),
        "latency": (18, 5, 1.5),
        "packet_loss": (0.02, 0.02, 0.01),
    },
}

# Scenario catalogue (also drives the "Simulator" panel in the UI).
SCENARIOS: dict[str, dict] = {
    "cpu_spike": {"label": "CPU runaway process", "types": ["router", "firewall", "server", "switch"]},
    "memory_leak": {"label": "Memory leak", "types": ["server", "cloud", "router"]},
    "bad_cable": {"label": "Bad cable / failing port", "types": ["switch", "router"]},
    "link_saturation": {"label": "WAN link saturation", "types": ["router"]},
    "disk_full": {"label": "Disk filling with logs", "types": ["server"]},
    "brute_force": {"label": "Brute-force login attack", "types": ["firewall"]},
    "exfiltration": {"label": "Data exfiltration", "types": ["switch"]},
    "device_down": {"label": "Device outage", "types": ["switch", "access_point", "server"]},
}

ATTACKER_IPS = ["45.155.205.17", "45.155.205.88", "193.32.162.41", "92.118.39.12"]
EXFIL_SRC, EXFIL_DST = "10.1.1.99", "185.220.101.5"


@dataclass
class SimDevice:
    id: str
    name: str
    type: str
    ip: str
    capacity_mbps: float = 1000.0


@dataclass
class Fault:
    kind: str
    device_id: str
    started: datetime
    stubborn: bool = False  # remediation will fail (demonstrates rollback)
    params: dict = field(default_factory=dict)

    def minutes(self, now: datetime) -> float:
        return max(0.0, (now - self.started).total_seconds() / 60)


def business_curve(t: datetime) -> float:
    """0..1 load factor: weekday business-hours peak, quiet nights/weekends."""
    hour = t.hour + t.minute / 60
    daily = max(0.0, math.sin(math.pi * (hour - 7) / 12)) if 7 <= hour <= 19 else 0.0
    return daily * (1.0 if t.weekday() < 5 else 0.2)


class NetworkSimulator:
    def __init__(
        self,
        devices: list[SimDevice],
        links: list[tuple[str, str]],
        seed: int | None = None,
        time_scale: float = 1.0,
    ) -> None:
        self.devices = {d.id: d for d in devices}
        self.children: dict[str, list[str]] = {}
        for up, down in links:
            self.children.setdefault(up, []).append(down)
        self.rng = random.Random(seed)
        self.faults: dict[str, Fault] = {}
        self.time_scale = time_scale  # >1 makes faults evolve faster (demo mode)

    # ------------------------------------------------------------------ topology
    def descendants(self, device_id: str) -> list[str]:
        seen: list[str] = []
        stack = list(self.children.get(device_id, []))
        while stack:
            d = stack.pop()
            if d not in seen:
                seen.append(d)
                stack.extend(self.children.get(d, []))
        return seen

    def _faults_for(self, device_id: str) -> list[Fault]:
        return [f for f in self.faults.values() if f.device_id == device_id]

    def _upstream_faults(self, device_id: str) -> list[Fault]:
        return [f for f in self.faults.values() if device_id in self.descendants(f.device_id)]

    def is_down(self, device_id: str) -> bool:
        return any(f.kind == "device_down" for f in self._faults_for(device_id)) or any(
            f.kind == "device_down" for f in self._upstream_faults(device_id)
        )

    # ------------------------------------------------------------------ samples
    def _baseline_values(self, dev: SimDevice, t: datetime) -> dict[str, float]:
        load = business_curve(t)
        out: dict[str, float] = {}
        for metric, (base, amp, sigma) in PROFILES[dev.type].items():
            v = base + amp * load + self.rng.gauss(0, sigma)
            if metric.startswith("traffic"):
                v *= dev.capacity_mbps
            out[metric] = v
        return out

    def sample(self, device_id: str, t: datetime) -> dict[str, float]:
        dev = self.devices[device_id]
        v = self._baseline_values(dev, t)
        for f in self._faults_for(device_id):
            m = f.minutes(t) * self.time_scale
            ramp = min(1.0, m / 0.5)
            if f.kind == "cpu_spike":
                v["cpu"] = v["cpu"] + (96 - v["cpu"]) * ramp + self.rng.uniform(-1.5, 1.5)
            elif f.kind == "memory_leak":
                v["memory"] = v["memory"] + 0.4 * m
            elif f.kind == "bad_cable":
                v["if_errors"] += ramp * (60 + self.rng.uniform(-15, 25))
                v["packet_loss"] += ramp * self.rng.uniform(3, 7)
                v["latency"] += ramp * self.rng.uniform(12, 20)
            elif f.kind == "link_saturation":
                v["traffic_in"] = dev.capacity_mbps * (0.96 + self.rng.uniform(0, 0.03))
                v["latency"] += ramp * self.rng.uniform(35, 50)
                v["packet_loss"] += ramp * self.rng.uniform(0.8, 1.6)
            elif f.kind == "disk_full":
                v["disk"] = min(99.5, v["disk"] + 0.5 * m)
            elif f.kind == "brute_force":
                v["cpu"] += 12 * ramp
            elif f.kind == "exfiltration":
                v["traffic_out"] += ramp * dev.capacity_mbps * 0.45
        # Upstream physical faults degrade everything behind them.
        for f in self._upstream_faults(device_id):
            ramp = min(1.0, f.minutes(t) * self.time_scale / 0.5)
            if f.kind == "bad_cable":
                v["latency"] += ramp * self.rng.uniform(10, 16)
                v["packet_loss"] += ramp * self.rng.uniform(1.5, 3)
            elif f.kind == "link_saturation":
                v["latency"] += ramp * self.rng.uniform(25, 40)
        for k, val in v.items():
            ceiling = 100.0 if k in ("cpu", "memory", "disk", "packet_loss") else None
            v[k] = round(max(0.0, min(ceiling, val) if ceiling else val), 3)
        return v

    def _logs(self, t: datetime) -> list[LogEvent]:
        logs: list[LogEvent] = []
        for f in self.faults.values():
            dev = self.devices[f.device_id]
            if f.kind == "brute_force":
                for _ in range(self.rng.randint(8, 18)):
                    ip = self.rng.choice(ATTACKER_IPS)
                    user = self.rng.choice(["admin", "root", "cisco", "user", "test"])
                    logs.append(
                        LogEvent(
                            t,
                            dev.name,
                            4,
                            f"Failed login attempt for user '{user}' from {ip}",
                            device_id=dev.id,
                            category="auth",
                            source_ip=ip,
                        )
                    )
            elif f.kind == "bad_cable" and self.rng.random() < 0.3:
                logs.append(
                    LogEvent(
                        t,
                        dev.name,
                        3,
                        "Interface GigabitEthernet0/24 CRC errors exceeded threshold",
                        device_id=dev.id,
                        category="interface",
                    )
                )
            elif f.kind == "device_down" and self.rng.random() < 0.05:
                up = next((u for u, kids in self.children.items() if f.device_id in kids), None)
                if up:
                    logs.append(
                        LogEvent(
                            t,
                            self.devices[up].name,
                            3,
                            f"Interface to {dev.name} changed state to down",
                            device_id=up,
                            category="interface",
                        )
                    )
        # Ambient background noise.
        if self.rng.random() < 0.08:
            dev = self.rng.choice(list(self.devices.values()))
            msg, sev, cat = self.rng.choice(
                [
                    ("Configuration saved by admin", 6, "config"),
                    ("User admin logged in via SSH", 6, "auth"),
                    (f"Failed login attempt for user 'guest' from 10.1.2.{self.rng.randint(2, 250)}", 4, "auth"),
                    ("NTP synchronised", 6, "system"),
                ]
            )
            src = msg.rsplit(" ", 1)[-1] if "from" in msg else None
            logs.append(LogEvent(t, dev.name, sev, msg, device_id=dev.id, category=cat, source_ip=src))
        return logs

    def _flows(self, t: datetime) -> list[Flow]:
        flows = [
            Flow(
                t,
                f"10.1.{self.rng.randint(1, 4)}.{self.rng.randint(2, 250)}",
                self.rng.choice(["8.8.8.8", "1.1.1.1", "142.250.72.14", "13.107.42.14"]),
                bytes=self.rng.randint(2_000, 400_000),
                packets=self.rng.randint(5, 400),
                dst_port=self.rng.choice([53, 443, 443, 80]),
            )
            for _ in range(self.rng.randint(1, 4))
        ]
        for f in self.faults.values():
            if f.kind == "exfiltration":
                flows.append(
                    Flow(
                        t,
                        EXFIL_SRC,
                        EXFIL_DST,
                        bytes=self.rng.randint(180, 320) * 1_000_000,
                        packets=self.rng.randint(120_000, 220_000),
                        dst_port=443,
                        device_id=f.device_id,
                    )
                )
        return flows

    def tick(self, t: datetime) -> CollectorBatch:
        batch = CollectorBatch()
        for device_id in self.devices:
            if not self.is_down(device_id):
                batch.samples.append(Sample(device_id, t, self.sample(device_id, t)))
        batch.logs = self._logs(t)
        batch.flows = self._flows(t)
        return batch

    def backfill(self, start: datetime, end: datetime, step: timedelta):
        """Fault-free history used to learn behavioural baselines."""
        t = start
        while t < end:
            yield [Sample(d, t, self._baseline_values(self.devices[d], t)) for d in self.devices]
            t += step

    # ------------------------------------------------------------------ faults
    def inject(
        self, kind: str, device_id: str | None = None, now: datetime | None = None, stubborn: bool = False
    ) -> Fault:
        if kind not in SCENARIOS:
            raise ValueError(f"unknown scenario {kind!r}")
        if device_id is None:
            candidates = [d.id for d in self.devices.values() if d.type in SCENARIOS[kind]["types"]]
            if kind == "link_saturation":
                candidates = [d for d in candidates if "wan" in self.devices[d].name] or candidates
            device_id = self.rng.choice(candidates)
        if device_id not in self.devices:
            raise ValueError(f"unknown device {device_id!r}")
        fault = Fault(kind, device_id, now or datetime.now(UTC), stubborn=stubborn)
        self.faults[f"{kind}:{device_id}"] = fault
        return fault

    def clear(self, device_id: str | None = None, kind: str | None = None) -> int:
        keys = [
            k
            for k, f in self.faults.items()
            if (device_id is None or f.device_id == device_id) and (kind is None or f.kind == kind)
        ]
        for k in keys:
            del self.faults[k]
        return len(keys)

    # ------------------------------------------------------------------ driver
    # Which remediation actions resolve which faults.
    RESOLVES = {
        "restart_process": {"cpu_spike"},
        "restart_service": {"memory_leak"},
        "switch_to_backup_port": {"bad_cable"},
        "reroute_traffic": {"link_saturation", "bad_cable"},
        "archive_logs": {"disk_full"},
        "block_ip": {"brute_force"},
        "quarantine_host": {"exfiltration"},
        "power_cycle": {"device_down"},
    }

    def execute(self, action: str, device_id: str, params: dict) -> dict:
        faults = self._faults_for(device_id)
        if action == "identify_top_process":
            busy = any(f.kind == "cpu_spike" for f in faults)
            return {"top_process": "backup-agent" if busy else "snmpd", "cpu_percent": 71.0 if busy else 3.0}
        if action == "check_process_critical":
            return {"critical": params.get("process") in {"routing", "bgpd", "postgres"}}
        if action == "ping":
            return {"reachable": not self.is_down(device_id)}
        if action in self.RESOLVES:
            targets = [f for f in faults if f.kind in self.RESOLVES[action]]
            if any(f.stubborn for f in targets):
                return {"applied": True, "note": "action applied but fault persists"}
            for f in targets:
                self.faults.pop(f"{f.kind}:{f.device_id}", None)
            return {"applied": True, "resolved_faults": [f.kind for f in targets]}
        # Rollback / informational actions are no-ops against the simulation.
        return {"applied": True}
