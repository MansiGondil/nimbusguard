from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.deps import Session, Viewer
from app.cache import get_cache
from app.config import get_settings
from app.models import Alert, Device, FlowRecord, Incident, Metric, PlaybookRun, Prediction, SyslogEvent, utcnow

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
SEV_ORDER = {"critical": 0, "warning": 1, "info": 2}


@router.get("/executive")
async def executive(session: Session, _: Viewer):
    settings = get_settings()
    now = utcnow()
    month = now - timedelta(days=30)
    devices = (await session.execute(select(func.count()).select_from(Device))).scalar_one() or 1

    # Uptime from device-down alert durations.
    down = (
        await session.execute(
            select(Alert.first_seen, Alert.resolved_at).where(
                Alert.kind == "availability", (Alert.resolved_at.is_(None)) | (Alert.resolved_at >= month)
            )
        )
    ).all()
    down_seconds = sum(((r or now) - max(f, month)).total_seconds() for f, r in down)
    uptime = 100 * (1 - down_seconds / (devices * 30 * 86400))

    runs = (
        await session.execute(
            select(PlaybookRun.status, func.count()).where(PlaybookRun.created_at >= month).group_by(PlaybookRun.status)
        )
    ).all()
    by_status = dict(runs)
    succeeded = by_status.get("succeeded", 0)
    attempted = succeeded + by_status.get("failed", 0) + by_status.get("rolled_back", 0)
    prevented = (
        await session.execute(
            select(func.count()).select_from(Incident).where(Incident.prevented.is_(True), Incident.created_at >= month)
        )
    ).scalar_one()
    prevented = max(prevented, succeeded)

    resolved = (
        await session.execute(
            select(Incident.created_at, Incident.resolved_at).where(
                Incident.resolved_at.is_not(None), Incident.created_at >= month
            )
        )
    ).all()
    mttr = (sum((r - c).total_seconds() for c, r in resolved) / len(resolved) / 60) if resolved else None

    health = await get_cache().get_json("health:score") or {}
    trend_rows = (
        await session.execute(
            select(Metric.time, Metric.value)
            .where(Metric.device_id == "network", Metric.metric == "health", Metric.time >= now - timedelta(days=7))
            .order_by(Metric.time)
        )
    ).all()
    hourly: dict[int, list[float]] = defaultdict(list)
    for t, v in trend_rows:
        hourly[int(t.timestamp() // 3600)].append(v)
    trend = [{"t": h * 3600 * 1000, "v": round(sum(v) / len(v), 2)} for h, v in sorted(hourly.items())]

    week = now - timedelta(days=7)
    inc_rows = (
        await session.execute(select(Incident.created_at, Incident.prevented).where(Incident.created_at >= week))
    ).all()
    daily: dict[str, dict] = {}
    for i in range(7, -1, -1):
        day = (now - timedelta(days=i)).date().isoformat()
        daily[day] = {"day": day, "incidents": 0, "prevented": 0}
    for created, prev in inc_rows:
        d = created.date().isoformat()
        if d in daily:
            daily[d]["incidents"] += 1
            daily[d]["prevented"] += int(bool(prev))

    hours_saved = prevented * settings.avg_incident_hours_avoided
    return {
        "health_score": health.get("score"),
        "uptime_percent": round(uptime, 3),
        "incidents_prevented": prevented,
        "automated_fixes": succeeded,
        "remediation_success_rate": round(100 * succeeded / attempted, 1) if attempted else None,
        "mttr_minutes": round(mttr, 1) if mttr is not None else None,
        "money_saved": round(hours_saved * settings.downtime_cost_per_hour),
        "downtime_hours_avoided": round(hours_saved, 1),
        "cost_model": {
            "downtime_cost_per_hour": settings.downtime_cost_per_hour,
            "avg_incident_hours_avoided": settings.avg_incident_hours_avoided,
        },
        "health_trend": trend,
        "incidents_by_day": list(daily.values()),
        "period_days": 30,
    }


@router.get("/operations")
async def operations(session: Session, _: Viewer):
    now = utcnow()
    devices = (await session.execute(select(Device))).scalars().all()
    names = {d.id: d.name for d in devices}
    status_counts: dict[str, int] = defaultdict(int)
    for d in devices:
        status_counts[d.status] += 1
    alerts = (
        (await session.execute(select(Alert).where(Alert.status != "resolved", Alert.kind != "prediction")))
        .scalars()
        .all()
    )
    alerts.sort(key=lambda a: (SEV_ORDER.get(a.severity, 9), a.first_seen))
    preds = (
        (await session.execute(select(Prediction).where(Prediction.active.is_(True)).order_by(Prediction.eta)))
        .scalars()
        .all()
    )
    pending = (
        await session.execute(
            select(func.count()).select_from(PlaybookRun).where(PlaybookRun.status == "pending_approval")
        )
    ).scalar_one()
    running = (
        await session.execute(
            select(func.count()).select_from(PlaybookRun).where(PlaybookRun.status.in_(["approved", "running"]))
        )
    ).scalar_one()
    logs = (await session.execute(select(SyslogEvent).order_by(SyslogEvent.time.desc()).limit(25))).scalars().all()
    talkers = (
        await session.execute(
            select(FlowRecord.src_ip, FlowRecord.dst_ip, func.sum(FlowRecord.bytes).label("bytes"))
            .where(FlowRecord.time >= now - timedelta(minutes=15))
            .group_by(FlowRecord.src_ip, FlowRecord.dst_ip)
            .order_by(func.sum(FlowRecord.bytes).desc())
            .limit(8)
        )
    ).all()
    open_incidents = (
        await session.execute(select(func.count()).select_from(Incident).where(Incident.status != "resolved"))
    ).scalar_one()
    health = await get_cache().get_json("health:score") or {}
    faults = await get_cache().get_json("sim:faults") or []
    return {
        "health_score": health.get("score"),
        "devices_total": len(devices),
        "devices_online": status_counts["up"] + status_counts["degraded"],
        "status_counts": status_counts,
        "open_incidents": open_incidents,
        "pending_approvals": pending,
        "active_remediations": running,
        "current_issues": [
            {
                "id": a.id,
                "device_id": a.device_id,
                "device": names.get(a.device_id),
                "title": a.title,
                "severity": a.severity,
                "kind": a.kind,
                "status": a.status,
                "first_seen": a.first_seen,
                "incident_id": a.incident_id,
            }
            for a in alerts[:30]
        ],
        "predicted_issues": [
            {
                "id": p.id,
                "device_id": p.device_id,
                "device": names.get(p.device_id),
                "summary": p.summary,
                "probability": p.probability,
                "eta": p.eta,
                "kind": p.kind,
            }
            for p in preds
        ],
        "recent_logs": [
            {"time": e.time, "host": e.host, "severity": e.severity, "category": e.category, "message": e.message}
            for e in logs
        ],
        "top_talkers": [{"src": s, "dst": d, "bytes": int(b)} for s, d, b in talkers],
        "simulated_faults": faults,
    }
