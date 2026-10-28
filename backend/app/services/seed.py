"""Seed the admin user and a demo network (a school + clinic campus)."""

import logging

from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Device, DeviceLink, User
from app.security import encrypt_credentials, hash_password
from app.services import audit

log = logging.getLogger(__name__)

# name, type, ip, criticality, capacity (Mbps), services, upstream (primary), backup upstream
DEMO_NETWORK = [
    ("rtr-wan-01", "router", "10.0.0.1", "critical", 1000, ["bgp"], None, None),
    ("rtr-wan-02", "router", "10.0.0.2", "high", 500, ["bgp"], None, None),
    ("fw-edge-01", "firewall", "10.0.0.10", "critical", 1000, ["vpn", "ids"], "rtr-wan-01", "rtr-wan-02"),
    ("rtr-core-01", "router", "10.0.1.1", "critical", 10000, ["ospf"], "fw-edge-01", None),
    ("sw-dc-01", "switch", "10.0.2.1", "high", 10000, [], "rtr-core-01", None),
    ("sw-dist-01", "switch", "10.0.3.1", "high", 10000, [], "rtr-core-01", None),
    ("sw-dist-02", "switch", "10.0.3.2", "high", 10000, [], "rtr-core-01", None),
    ("sw-acc-01", "switch", "10.1.1.2", "medium", 1000, [], "sw-dist-01", None),
    ("sw-acc-02", "switch", "10.1.2.2", "medium", 1000, [], "sw-dist-01", None),
    ("sw-acc-03", "switch", "10.1.3.2", "medium", 1000, [], "sw-dist-02", None),
    ("sw-acc-04", "switch", "10.1.4.2", "medium", 1000, [], "sw-dist-02", None),
    ("ap-library-01", "access_point", "10.1.1.10", "low", 300, [], "sw-acc-01", None),
    ("ap-classroom-01", "access_point", "10.1.1.11", "low", 300, [], "sw-acc-01", None),
    ("ap-classroom-02", "access_point", "10.1.2.10", "low", 300, [], "sw-acc-02", None),
    ("ap-clinic-01", "access_point", "10.1.3.10", "medium", 300, [], "sw-acc-03", None),
    ("ap-clinic-02", "access_point", "10.1.3.11", "medium", 300, [], "sw-acc-03", None),
    ("ap-office-01", "access_point", "10.1.4.10", "low", 300, [], "sw-acc-04", None),
    ("srv-web-01", "server", "10.0.2.10", "high", 1000, ["nginx"], "sw-dc-01", None),
    ("srv-db-01", "server", "10.0.2.11", "critical", 1000, ["postgres"], "sw-dc-01", None),
    ("srv-mail-01", "server", "10.0.2.12", "high", 1000, ["postfix"], "sw-dc-01", None),
    ("srv-file-01", "server", "10.0.2.13", "medium", 1000, ["samba"], "sw-dc-01", None),
    ("srv-ehr-01", "server", "10.0.2.14", "critical", 1000, ["ehr-app"], "sw-dc-01", None),
    ("cloud-app-01", "cloud", "172.31.10.5", "high", 1000, ["api"], "rtr-wan-01", None),
    ("cloud-backup-01", "cloud", "172.31.10.9", "medium", 500, ["backup"], "rtr-wan-01", None),
]

SITES = {"ap-clinic": "clinic", "sw-acc-03": "clinic"}


async def seed() -> None:
    settings = get_settings()
    async with SessionLocal() as s:
        if not (await s.execute(select(User).where(User.username == settings.admin_username))).scalar():
            s.add(
                User(
                    username=settings.admin_username,
                    hashed_password=hash_password(settings.admin_password),
                    role="admin",
                )
            )
            await s.commit()
            await audit.record(s, "system", "user.created", settings.admin_username, {"role": "admin"})
            log.info("Created admin user %r", settings.admin_username)

        if not settings.simulator_enabled:
            return
        if (await s.execute(select(func.count()).select_from(Device))).scalar_one():
            return
        devices = {}
        for name, typ, ip, crit, cap, services, *_ in DEMO_NETWORK[: settings.simulator_devices]:
            site = next((v for k, v in SITES.items() if name.startswith(k)), "school")
            d = Device(
                name=name,
                type=typ,
                ip=ip,
                criticality=crit,
                capacity_mbps=cap,
                services=services,
                site=site,
                vendor="simulated",
                collector="simulated",
                status="unknown",
                credentials_enc=encrypt_credentials({"snmp_community": "demo-public"}),
            )
            s.add(d)
            devices[name] = d
        await s.flush()
        for name, *_rest, up, backup in DEMO_NETWORK[: settings.simulator_devices]:
            if up and up in devices and name in devices:
                s.add(DeviceLink(upstream_id=devices[up].id, downstream_id=devices[name].id, kind="primary"))
            if backup and backup in devices and name in devices:
                s.add(DeviceLink(upstream_id=devices[backup].id, downstream_id=devices[name].id, kind="backup"))
        await s.commit()
        await audit.record(s, "system", "inventory.seeded", None, {"devices": len(devices)})
        log.info("Seeded demo network with %d devices", len(devices))
