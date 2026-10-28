from datetime import UTC, datetime

import pytest

from app.automation import safety
from app.automation.drivers import COMMANDS, DryRunDriver
from app.automation.playbooks import BUILTIN_ACTIONS, load_all, parse, render
from app.collectors.simulator import NetworkSimulator

PLAYBOOKS = load_all()
SIM_ACTIONS = set(NetworkSimulator.RESOLVES) | {"identify_top_process", "check_process_critical", "ping"}


def test_all_playbooks_load_and_use_known_actions():
    assert len(PLAYBOOKS) >= 8
    known = BUILTIN_ACTIONS | set(COMMANDS) | SIM_ACTIONS
    for pb in PLAYBOOKS.values():
        for step in pb.steps + pb.rollback:
            assert step.action in known, f"{pb.name}: unknown action {step.action}"
        assert pb.triggers


def test_signatures_route_to_playbooks():
    triggers = {t for pb in PLAYBOOKS.values() for t in pb.triggers}
    for sig in (
        "cpu_high",
        "memory_exhaustion",
        "interface_errors",
        "link_saturation",
        "disk_full",
        "brute_force",
        "exfiltration",
        "device_down",
    ):
        assert sig in triggers


def test_parse_validation():
    with pytest.raises(ValueError):
        parse({"name": "Bad Name", "triggers": ["x"], "steps": [{"action": "wait"}]})
    with pytest.raises(ValueError):
        parse({"name": "ok", "triggers": ["x"], "steps": [{"params": {}}]})
    with pytest.raises(ValueError):
        parse({"name": "ok", "triggers": ["x"], "risk": "extreme", "steps": [{"action": "wait"}]})


def test_render_keeps_lists_and_formats_strings():
    ctx = {"source_ips": ["1.2.3.4", "5.6.7.8"], "device": "fw-edge-01"}
    assert render({"ips": "{source_ips}"}, ctx) == {"ips": ["1.2.3.4", "5.6.7.8"]}
    assert (
        render("Blocked {source_ips} on {device} ({missing})", ctx)
        == "Blocked 1.2.3.4, 5.6.7.8 on fw-edge-01 ({missing})"
    )


async def test_dry_run_driver_renders_vendor_commands():
    out = await DryRunDriver().execute("block_ip", {"id": "fw"}, {"ips": ["1.2.3.4", "5.6.7.8"], "duration_hours": 24})
    assert out["dry_run"] and " deny ip host 5.6.7.8 any" in out["commands"]
    out = await DryRunDriver().execute(
        "switch_to_backup_port", {"id": "s"}, {"from_port": "Gi0/24", "to_port": "Gi0/23"}
    )
    assert out["commands"][:2] == ["interface Gi0/24", " shutdown"]


def _gates(pb, **kw):
    args = dict(
        now=datetime(2026, 1, 5, 12, tzinfo=UTC),
        playbook=pb,
        device={"criticality": "medium"},
        recent_runs=0,
        downstream=0,
        automation_enabled=True,
        max_per_hour=3,
        impact_threshold=3,
        window="00:00-06:00",
    )
    args.update(kw)
    return safety.decision(safety.evaluate(**args))


def test_safety_gates():
    cpu, cable, exfil, brute = (PLAYBOOKS[n] for n in ("high-cpu", "interface-errors", "exfiltration", "brute-force"))
    assert _gates(cpu) == "approved"
    assert _gates(cpu, recent_runs=3) == "blocked"  # max 3 fixes / device / hour
    assert _gates(cpu, device={"maintenance_mode": True}) == "blocked"
    assert _gates(cable, downstream=5) == "pending_approval"  # impact analysis
    assert _gates(brute, downstream=19) == "approved"  # ACL change has no downstream impact
    assert _gates(cable, device={"criticality": "critical"}) == "pending_approval"
    assert _gates(exfil) == "pending_approval"  # policy + outside change window
    assert _gates(cpu, automation_enabled=False) == "pending_approval"


def test_maintenance_window_wraps_midnight():
    assert safety.in_window(datetime(2026, 1, 1, 23, 30), "22:00-02:00")
    assert safety.in_window(datetime(2026, 1, 1, 1, 0), "22:00-02:00")
    assert not safety.in_window(datetime(2026, 1, 1, 12, 0), "22:00-02:00")


def test_cooldown_blocks_repeat_fix():
    assert _gates(PLAYBOOKS["disk-cleanup"], recently_succeeded=True) == "blocked"
