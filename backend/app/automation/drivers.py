"""Execution drivers: how a playbook action is carried out on a device.

* ``SimulatorDriver`` applies actions to the network simulator, so fixes have
  a measurable effect in demo mode.
* ``DryRunDriver`` renders the vendor commands that *would* be pushed to a
  real device without touching it. A real SSH/NETCONF driver (e.g. Netmiko,
  Nornir) implements the same ``execute`` interface.
"""

from typing import Protocol

COMMANDS = {
    "snapshot_config": ["copy running-config flash:ng-snapshot.cfg"],
    "restore_config": ["configure replace flash:ng-snapshot.cfg force"],
    "restart_process": ["systemctl restart {process}"],
    "restart_service": ["systemctl restart {service}"],
    "switch_to_backup_port": ["interface {from_port}", " shutdown", "interface {to_port}", " no shutdown"],
    "reroute_traffic": ["route-map NG-WAN-SPLIT permit 10", " set ip next-hop <backup-gateway>"],
    "archive_logs": ["find {path} -name '*.log' -mtime +{older_than_days} -exec gzip {{}} \\;"],
    "block_ip": ["ip access-list extended NG-BLOCK", " deny ip host {ip} any"],
    "unblock_ip": ["ip access-list extended NG-BLOCK", " no deny ip host {ip} any"],
    "quarantine_host": ["interface <port-of-{host}>", " switchport access vlan {vlan}"],
    "release_host": ["interface <port-of-{host}>", " no switchport access vlan 999"],
    "power_cycle": ["pdu outlet <outlet> reboot"],
}


class Driver(Protocol):
    name: str

    async def execute(self, action: str, device: dict, params: dict) -> dict: ...


class SimulatorDriver:
    name = "simulator"

    def __init__(self, simulator, on_change=None) -> None:
        self.sim = simulator
        self.on_change = on_change  # async callback when the fault set changes

    async def execute(self, action: str, device: dict, params: dict) -> dict:
        before = len(self.sim.faults)
        result = self.sim.execute(action, device["id"], params)
        if self.on_change and len(self.sim.faults) != before:
            await self.on_change()
        return result


class DryRunDriver:
    name = "dry-run"

    async def execute(self, action: str, device: dict, params: dict) -> dict:
        templates = COMMANDS.get(action)
        if templates is None:
            if action in ("identify_top_process", "check_process_critical", "ping"):
                return {"dry_run": True, "note": f"{action} requires a live driver"}
            raise ValueError(f"no command template for action {action!r}")
        ips = params.get("ips") or [None]
        commands = []
        for ip in ips if isinstance(ips, list) else [ips]:
            fmt = {**params, "ip": ip}
            commands += [
                t.format(**{k: v for k, v in fmt.items() if v is not None}) if "{" in t else t for t in templates
            ]
        return {"applied": False, "dry_run": True, "commands": commands}
