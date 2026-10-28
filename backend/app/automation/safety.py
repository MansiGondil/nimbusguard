"""Safety gates that stop automation from making things worse.

Each gate returns a ``Gate``; ``effect`` is ``"block"`` (do not run),
``"approval"`` (hold for a human) or ``None`` (pass).
"""

from dataclasses import asdict, dataclass
from datetime import datetime, time


@dataclass
class Gate:
    name: str
    passed: bool
    detail: str
    effect: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def in_window(now: datetime, window: str) -> bool:
    start_s, end_s = window.split("-")
    start, end = time.fromisoformat(start_s), time.fromisoformat(end_s)
    t = now.time()
    return start <= t < end if start <= end else (t >= start or t < end)


def evaluate(
    *,
    now: datetime,
    playbook,
    device: dict,
    recent_runs: int,
    downstream: int,
    automation_enabled: bool,
    max_per_hour: int,
    impact_threshold: int,
    window: str,
    recently_succeeded: bool = False,
) -> list[Gate]:
    gates = [
        Gate(
            "automation_enabled",
            automation_enabled,
            "Auto-remediation is enabled" if automation_enabled else "Auto-remediation is disabled globally",
            None if automation_enabled else "approval",
        ),
        Gate(
            "maintenance_mode",
            not device.get("maintenance_mode"),
            "Device is under manual maintenance" if device.get("maintenance_mode") else "Device not in maintenance",
            "block" if device.get("maintenance_mode") else None,
        ),
        Gate(
            "change_frequency",
            recent_runs < max_per_hour,
            f"{recent_runs}/{max_per_hour} automated fixes on this device in the last hour",
            None if recent_runs < max_per_hour else "block",
        ),
        Gate(
            "cooldown",
            not recently_succeeded,
            "This fix just succeeded here; observing before acting again"
            if recently_succeeded
            else "No recent identical fix",
            "block" if recently_succeeded else None,
        ),
    ]
    if playbook.risk == "high":
        ok = in_window(now, window)
        gates.append(
            Gate(
                "change_window",
                ok,
                f"High-risk change {'inside' if ok else 'outside'} maintenance window {window} UTC",
                None if ok else "approval",
            )
        )
    if playbook.impact == "none":
        gates.append(Gate("impact_analysis", True, "Change is scoped to this device; no downstream impact"))
    else:
        impact_ok = downstream < impact_threshold
        gates.append(
            Gate(
                "impact_analysis",
                impact_ok,
                f"{downstream} downstream device(s) affected (approval at >= {impact_threshold})",
                None if impact_ok else "approval",
            )
        )
    critical = device.get("criticality") == "critical" and playbook.risk != "low"
    gates.append(
        Gate(
            "critical_system",
            not critical,
            "Critical system - human approval required" if critical else "Not a critical-system change",
            "approval" if critical else None,
        )
    )
    gates.append(
        Gate(
            "playbook_policy",
            not playbook.requires_approval,
            "Playbook requires human approval" if playbook.requires_approval else "Playbook allows automation",
            "approval" if playbook.requires_approval else None,
        )
    )
    return gates


def decision(gates: list[Gate]) -> str:
    if any(g.effect == "block" for g in gates):
        return "blocked"
    if any(g.effect == "approval" for g in gates):
        return "pending_approval"
    return "approved"
