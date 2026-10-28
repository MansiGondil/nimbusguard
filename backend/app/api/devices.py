from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import Admin, Operator, Session, Viewer
from app.cache import get_cache
from app.collectors.base import METRICS
from app.models import Alert, Device, DeviceLink, Metric, PlaybookRun, SyslogEvent, utcnow
from app.security import encrypt_credentials
from app.services import audit
from app.services.runtime import dispatch

router = APIRouter(tags=["devices"])

DeviceType = Literal["router", "switch", "firewall", "server", "access_point", "cloud"]
Criticality = Literal["low", "medium", "high", "critical"]


class DeviceCreate(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    type: DeviceType
    ip: str | None = None
    site: str = "main"
    vendor: str | None = None
    criticality: Criticality = "medium"
    collector: Literal["snmp", "cloud", "simulated"] = "snmp"
    capacity_mbps: float = 1000.0
    services: list[str] = []
    snmp_community: str | None = None
    snmp_profile: Literal["net-snmp", "cisco-ios"] = "net-snmp"
    cloud_instance_id: str | None = None  # e.g. EC2 instance id for collector="cloud"
    cloud_region: str | None = None
    upstream_id: str | None = None


class DeviceUpdate(BaseModel):
    criticality: Criticality | None = None
    maintenance_mode: bool | None = None
    site: str | None = None
    capacity_mbps: float | None = None
    services: list[str] | None = None


def device_json(d: Device, latest: dict | None = None) -> dict:
    return {
        "id": d.id,
        "name": d.name,
        "type": d.type,
        "ip": d.ip,
        "site": d.site,
        "vendor": d.vendor,
        "criticality": d.criticality,
        "status": d.status,
        "collector": d.collector,
        "capacity_mbps": d.capacity_mbps,
        "services": d.services or [],
        "maintenance_mode": d.maintenance_mode,
        "last_seen": (latest or {}).get("time") or d.last_seen,
        "created_at": d.created_at,
        "metrics": (latest or {}).get("values", {}),
        "has_credentials": bool(d.credentials_enc),
    }


async def _latest_all() -> dict:
    return await get_cache().get_json("latest:all") or {}


@router.get("/devices")
async def list_devices(session: Session, _: Viewer):
    latest = await _latest_all()
    devices = (await session.execute(select(Device).order_by(Device.name))).scalars().all()
    return [device_json(d, latest.get(d.id)) for d in devices]


@router.get("/topology")
async def topology(session: Session, _: Viewer):
    devices = (await session.execute(select(Device))).scalars().all()
    links = (await session.execute(select(DeviceLink))).scalars().all()
    return {
        "nodes": [
            {
                "id": d.id,
                "name": d.name,
                "type": d.type,
                "status": d.status,
                "criticality": d.criticality,
                "site": d.site,
            }
            for d in devices
        ],
        "edges": [{"source": lnk.upstream_id, "target": lnk.downstream_id, "kind": lnk.kind} for lnk in links],
    }


async def _get(session, device_id: str) -> Device:
    d = await session.get(Device, device_id)
    if d is None:
        d = (await session.execute(select(Device).where(Device.name == device_id))).scalar()
    if d is None:
        raise HTTPException(404, "Device not found")
    return d


@router.get("/devices/{device_id}")
async def get_device(device_id: str, session: Session, _: Viewer):
    d = await _get(session, device_id)
    latest = (await _latest_all()).get(d.id)
    links = (
        (
            await session.execute(
                select(DeviceLink).where((DeviceLink.upstream_id == d.id) | (DeviceLink.downstream_id == d.id))
            )
        )
        .scalars()
        .all()
    )
    names = dict((await session.execute(select(Device.id, Device.name))).all())
    runs = (
        (
            await session.execute(
                select(PlaybookRun)
                .where(PlaybookRun.device_id == d.id)
                .order_by(PlaybookRun.created_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    changes = (
        (
            await session.execute(
                select(SyslogEvent)
                .where(SyslogEvent.device_id == d.id, SyslogEvent.category.in_(["config", "interface"]))
                .order_by(SyslogEvent.time.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    alerts = (
        (
            await session.execute(
                select(Alert).where(Alert.device_id == d.id).order_by(Alert.first_seen.desc()).limit(15)
            )
        )
        .scalars()
        .all()
    )
    recent_changes = sorted(
        [
            {
                "time": r.finished_at or r.created_at,
                "kind": "remediation",
                "summary": f"{r.playbook} ({r.status})",
                "run_id": r.id,
            }
            for r in runs
        ]
        + [{"time": e.time, "kind": e.category, "summary": e.message} for e in changes],
        key=lambda x: x["time"],
        reverse=True,
    )[:12]
    return {
        **device_json(d, latest),
        "upstream": [
            {"id": lnk.upstream_id, "name": names.get(lnk.upstream_id), "kind": lnk.kind}
            for lnk in links
            if lnk.downstream_id == d.id
        ],
        "downstream": [
            {"id": lnk.downstream_id, "name": names.get(lnk.downstream_id), "kind": lnk.kind}
            for lnk in links
            if lnk.upstream_id == d.id
        ],
        "recent_changes": recent_changes,
        "alerts": [
            {
                "id": a.id,
                "title": a.title,
                "severity": a.severity,
                "status": a.status,
                "kind": a.kind,
                "first_seen": a.first_seen,
            }
            for a in alerts
        ],
        "metric_catalog": METRICS,
    }


@router.get("/devices/{device_id}/metrics")
async def device_metrics(
    device_id: str,
    session: Session,
    _: Viewer,
    metrics: str = Query("cpu,memory,traffic_in,traffic_out,latency,packet_loss"),
    hours: float = Query(24, gt=0, le=24 * 30),
    points: int = Query(180, ge=10, le=2000),
):
    d = await _get(session, device_id)
    names = [m for m in metrics.split(",") if m]
    since = utcnow() - timedelta(hours=hours)
    rows = (
        await session.execute(
            select(Metric.metric, Metric.time, Metric.value)
            .where(Metric.device_id == d.id, Metric.metric.in_(names), Metric.time >= since)
            .order_by(Metric.time)
        )
    ).all()
    bucket_s = max(1.0, hours * 3600 / points)
    buckets: dict[str, dict[int, list[float]]] = {m: {} for m in names}
    for metric, t, v in rows:
        b = int(t.timestamp() // bucket_s)
        buckets[metric].setdefault(b, []).append(v)
    series = {
        m: [{"t": int(b * bucket_s * 1000), "v": round(sum(vs) / len(vs), 3)} for b, vs in sorted(buckets[m].items())]
        for m in names
    }
    return {"device_id": d.id, "bucket_seconds": bucket_s, "series": series}


@router.get("/devices/{device_id}/logs")
async def device_logs(device_id: str, session: Session, _: Viewer, limit: int = Query(50, le=500)):
    d = await _get(session, device_id)
    rows = (
        (
            await session.execute(
                select(SyslogEvent).where(SyslogEvent.device_id == d.id).order_by(SyslogEvent.time.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {"time": e.time, "severity": e.severity, "category": e.category, "message": e.message, "source_ip": e.source_ip}
        for e in rows
    ]


@router.post("/devices", status_code=201)
async def create_device(body: DeviceCreate, session: Session, user: Operator):
    if (await session.execute(select(Device).where(Device.name == body.name))).scalar():
        raise HTTPException(409, "A device with that name exists")
    creds = {}
    if body.snmp_community:
        creds |= {"snmp_community": body.snmp_community, "snmp_profile": body.snmp_profile}
    if body.cloud_instance_id:
        creds |= {"provider": "aws", "instance_id": body.cloud_instance_id, "region": body.cloud_region or "us-east-1"}
    d = Device(
        name=body.name,
        type=body.type,
        ip=body.ip,
        site=body.site,
        vendor=body.vendor,
        criticality=body.criticality,
        collector=body.collector,
        capacity_mbps=body.capacity_mbps,
        services=body.services,
        credentials_enc=encrypt_credentials(creds) if creds else None,
    )
    session.add(d)
    await session.flush()
    if body.upstream_id:
        if not await session.get(Device, body.upstream_id):
            raise HTTPException(400, "Unknown upstream device")
        session.add(DeviceLink(upstream_id=body.upstream_id, downstream_id=d.id))
    await session.commit()
    await audit.record(
        session, user.username, "device.created", d.name, {"type": d.type, "ip": d.ip, "collector": d.collector}
    )
    await dispatch({"command": "reload_devices"})
    return device_json(d)


@router.patch("/devices/{device_id}")
async def update_device(device_id: str, body: DeviceUpdate, session: Session, user: Operator):
    d = await _get(session, device_id)
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(d, k, v)
    await session.commit()
    await audit.record(session, user.username, "device.updated", d.name, changes)
    await dispatch({"command": "reload_devices"})
    return device_json(d)


@router.delete("/devices/{device_id}", status_code=204)
async def delete_device(device_id: str, session: Session, user: Admin):
    d = await _get(session, device_id)
    await session.delete(d)
    await session.commit()
    await audit.record(session, user.username, "device.deleted", d.name)
    await dispatch({"command": "reload_devices"})
