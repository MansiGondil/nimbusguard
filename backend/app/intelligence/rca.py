"""Root-cause analysis: topology-aware alert correlation.

Alerts on devices that are connected in the dependency graph and occur
within a short time window are grouped into one incident (smart alert
grouping). The root cause is the most-upstream alerting device; its metric
signature selects a diagnosis and recommended fix, and the path from an
impacted device to the root becomes the investigation chain.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}

# signature -> (diagnosis, recommendation)
DIAGNOSES = {
    "interface_errors": ("Physical layer fault (bad cable or failing port)", "Replace cable or switch to backup port"),
    "link_saturation": ("Link congestion", "Reroute traffic to the backup link or apply QoS"),
    "cpu_high": ("Runaway process exhausting CPU", "Restart the offending process"),
    "memory_high": ("Memory exhaustion / leak", "Gracefully restart the leaking service"),
    "disk_full": ("Disk filling up", "Archive and compress old logs"),
    "device_down": ("Device unreachable", "Power-cycle via out-of-band management, then fail over"),
    "brute_force": ("Brute-force login attack", "Block attacking IPs and enforce MFA"),
    "exfiltration": ("Possible data exfiltration", "Quarantine the host and investigate"),
    "overheating": ("Hardware overheating", "Check cooling / fans; reduce load"),
    "degraded_path": ("Degraded network path", "Trace the path upstream for a failing hop"),
    "traffic_spike": ("Unusual traffic volume", "Inspect top talkers in NetFlow"),
    "multivariate": ("Unusual combined behaviour", "Review device metrics"),
}
# When the root device shows several signatures, prefer the more causal one.
SIGNATURE_PRIORITY = [
    "device_down",
    "interface_errors",
    "link_saturation",
    "exfiltration",
    "brute_force",
    "memory_high",
    "cpu_high",
    "disk_full",
    "overheating",
    "traffic_spike",
    "degraded_path",
    "multivariate",
]


@dataclass
class AlertView:
    id: str
    device_id: str | None
    signature: str
    severity: str
    first_seen: datetime
    title: str = ""
    metric: str | None = None


@dataclass
class IncidentPlan:
    alert_ids: list[str]
    root_device_id: str | None
    signature: str
    severity: str
    title: str
    rca: dict = field(default_factory=dict)


class Topology:
    def __init__(self, links: list[tuple[str, str]], names: dict[str, str] | None = None) -> None:
        self.parents: dict[str, set[str]] = {}
        self.children: dict[str, set[str]] = {}
        for up, down in links:
            self.parents.setdefault(down, set()).add(up)
            self.children.setdefault(up, set()).add(down)
        self.names = names or {}

    def name(self, device_id: str | None) -> str:
        return self.names.get(device_id, device_id or "network")

    def ancestors(self, d: str) -> set[str]:
        out, stack = set(), list(self.parents.get(d, ()))
        while stack:
            p = stack.pop()
            if p not in out:
                out.add(p)
                stack.extend(self.parents.get(p, ()))
        return out

    def descendants(self, d: str) -> set[str]:
        out, stack = set(), list(self.children.get(d, ()))
        while stack:
            c = stack.pop()
            if c not in out:
                out.add(c)
                stack.extend(self.children.get(c, ()))
        return out

    def path_up(self, start: str, root: str) -> list[str]:
        """Shortest upward path from ``start`` to ``root`` (inclusive)."""
        frontier, prev = [start], {start: None}
        while frontier:
            nxt = []
            for node in frontier:
                if node == root:
                    path = [node]
                    while prev[path[-1]] is not None:
                        path.append(prev[path[-1]])
                    return path[::-1]
                for p in self.parents.get(node, ()):
                    if p not in prev:
                        prev[p] = node
                        nxt.append(p)
            frontier = nxt
        return [start]


def correlate(
    alerts: list[AlertView],
    topo: Topology,
    window: timedelta = timedelta(minutes=10),
    latest: dict[str, dict] | None = None,
) -> list[IncidentPlan]:
    """Group related alerts and determine a root cause for each group."""
    latest = latest or {}
    alerts = sorted(alerts, key=lambda a: a.first_seen)
    groups: list[list[AlertView]] = []
    for a in alerts:
        placed = False
        for g in groups:
            if a.first_seen - g[0].first_seen > window and a.device_id not in {x.device_id for x in g}:
                continue
            for b in g:
                same = a.device_id == b.device_id
                related = (
                    a.device_id
                    and b.device_id
                    and (a.device_id in topo.ancestors(b.device_id) or b.device_id in topo.ancestors(a.device_id))
                )
                # Security and network-wide alerts only group with the same device.
                if same or (
                    related
                    and a.signature not in ("brute_force", "exfiltration")
                    and b.signature not in ("brute_force", "exfiltration")
                ):
                    g.append(a)
                    placed = True
                    break
            if placed:
                break
        if not placed:
            groups.append([a])

    plans = []
    for g in groups:
        devices = {a.device_id for a in g if a.device_id}
        # Most upstream device: none of its ancestors are alerting.
        roots = [d for d in devices if not (topo.ancestors(d) & devices)]
        by_dev = {d: [a for a in g if a.device_id == d] for d in devices}

        def root_key(d: str, by_dev=by_dev) -> tuple:
            sigs = {a.signature for a in by_dev[d]}
            best = min((SIGNATURE_PRIORITY.index(s) for s in sigs if s in SIGNATURE_PRIORITY), default=99)
            return (best, min(a.first_seen for a in by_dev[d]))

        root = min(roots, key=root_key) if roots else None
        root_alerts = by_dev.get(root, g)
        sig = min(
            (a.signature for a in root_alerts),
            key=lambda s: SIGNATURE_PRIORITY.index(s) if s in SIGNATURE_PRIORITY else 99,
        )
        diagnosis, recommendation = DIAGNOSES.get(sig, ("Unknown", "Investigate"))
        severity = max((a.severity for a in g), key=lambda s: SEVERITY_RANK.get(s, 0))

        # Investigation chain: walk from the furthest impacted device to the root.
        chain = []
        impacted = sorted(devices - {root}, key=lambda d: -len(topo.ancestors(d))) if root else []
        if root and impacted:
            for d in topo.path_up(impacted[0], root):
                alerts_here = by_dev.get(d, [])
                vals = latest.get(d, {})
                chain.append(
                    {
                        "device_id": d,
                        "device": topo.name(d),
                        "status": "abnormal" if alerts_here else "normal",
                        "finding": "; ".join(a.title for a in alerts_here) or "Metrics normal",
                        "latency": vals.get("latency"),
                        "packet_loss": vals.get("packet_loss"),
                    }
                )
        elif root:
            chain.append(
                {
                    "device_id": root,
                    "device": topo.name(root),
                    "status": "abnormal",
                    "finding": "; ".join(a.title for a in root_alerts),
                }
            )

        blast = topo.descendants(root) if root else set()
        title = f"{diagnosis} on {topo.name(root)}" if root else diagnosis
        if len(g) > 1:
            title += f" ({len(g)} related alerts)"
        plans.append(
            IncidentPlan(
                alert_ids=[a.id for a in g],
                root_device_id=root,
                signature=sig,
                severity=severity,
                title=title,
                rca={
                    "root_cause": f"{diagnosis} on {topo.name(root)}" if root else diagnosis,
                    "signature": sig,
                    "diagnosis": diagnosis,
                    "recommendation": recommendation,
                    "investigation_chain": chain,
                    "affected_devices": sorted(topo.name(d) for d in devices - {root}),
                    "impact": {
                        "downstream_devices": len(blast),
                        "alerting_devices": len(devices),
                        "grouped_alerts": len(g),
                    },
                    "confidence": round(min(0.95, 0.55 + 0.1 * len(g) + (0.15 if chain else 0)), 2),
                },
            )
        )
    return plans
