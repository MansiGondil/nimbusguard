from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.api.deps import Operator, Session, Viewer
from app.cache import get_cache
from app.models import Alert, Device, Incident, PlaybookRun, Prediction, utcnow
from app.services import audit

router = APIRouter(tags=["alerts"])


def alert_json(a: Alert, names: dict | None = None) -> dict:
    return {
        "id": a.id,
        "device_id": a.device_id,
        "device": (names or {}).get(a.device_id),
        "kind": a.kind,
        "metric": a.metric,
        "severity": a.severity,
        "title": a.title,
        "detail": a.detail,
        "score": a.score,
        "status": a.status,
        "incident_id": a.incident_id,
        "acknowledged_by": a.acknowledged_by,
        "first_seen": a.first_seen,
        "last_seen": a.last_seen,
        "resolved_at": a.resolved_at,
    }


def incident_json(i: Incident, names: dict, alert_count: int | None = None) -> dict:
    return {
        "id": i.id,
        "title": i.title,
        "severity": i.severity,
        "status": i.status,
        "root_cause_device_id": i.root_cause_device_id,
        "root_cause_device": names.get(i.root_cause_device_id),
        "rca": i.rca,
        "prevented": i.prevented,
        "created_at": i.created_at,
        "resolved_at": i.resolved_at,
        "alert_count": alert_count,
    }


async def _names(session) -> dict:
    return dict((await session.execute(select(Device.id, Device.name))).all())


@router.get("/alerts")
async def list_alerts(
    session: Session,
    _: Viewer,
    status: str | None = None,
    severity: str | None = None,
    device_id: str | None = None,
    limit: int = Query(100, le=1000),
):
    q = select(Alert).order_by(Alert.first_seen.desc()).limit(limit)
    if status == "active":
        q = q.where(Alert.status != "resolved")
    elif status:
        q = q.where(Alert.status == status)
    if severity:
        q = q.where(Alert.severity == severity)
    if device_id:
        q = q.where(Alert.device_id == device_id)
    names = await _names(session)
    return [alert_json(a, names) for a in (await session.execute(q)).scalars()]


async def _set_status(session, alert_id: str, user: str, status: str) -> dict:
    a = await session.get(Alert, alert_id)
    if a is None:
        raise HTTPException(404, "Alert not found")
    a.status = status
    if status == "acknowledged":
        a.acknowledged_by = user
    else:
        a.resolved_at = utcnow()
    await session.commit()
    await audit.record(session, user, f"alert.{status}", a.title, {"alert_id": a.id})
    await get_cache().publish({"type": "alert", "action": status, "alert": {"id": a.id}})
    return alert_json(a)


@router.post("/alerts/{alert_id}/ack")
async def ack_alert(alert_id: str, session: Session, user: Operator):
    return await _set_status(session, alert_id, user.username, "acknowledged")


@router.post("/alerts/{alert_id}/resolve")
async def resolve_alert(alert_id: str, session: Session, user: Operator):
    return await _set_status(session, alert_id, user.username, "resolved")


@router.get("/incidents")
async def list_incidents(session: Session, _: Viewer, status: str | None = None, limit: int = Query(50, le=500)):
    q = select(Incident).order_by(Incident.created_at.desc()).limit(limit)
    if status == "active":
        q = q.where(Incident.status != "resolved")
    elif status:
        q = q.where(Incident.status == status)
    incidents = (await session.execute(q)).scalars().all()
    counts: dict[str, int] = {}
    for (iid,) in (
        await session.execute(select(Alert.incident_id).where(Alert.incident_id.in_([i.id for i in incidents])))
    ).all():
        counts[iid] = counts.get(iid, 0) + 1
    names = await _names(session)
    return [incident_json(i, names, counts.get(i.id, 0)) for i in incidents]


@router.get("/incidents/{incident_id}")
async def get_incident(incident_id: str, session: Session, _: Viewer):
    inc = await session.get(Incident, incident_id)
    if inc is None:
        raise HTTPException(404, "Incident not found")
    names = await _names(session)
    alerts = (
        (await session.execute(select(Alert).where(Alert.incident_id == inc.id).order_by(Alert.first_seen)))
        .scalars()
        .all()
    )
    runs = (
        (await session.execute(select(PlaybookRun).where(PlaybookRun.alert_id.in_([a.id for a in alerts]))))
        .scalars()
        .all()
    )
    return {
        **incident_json(inc, names, len(alerts)),
        "alerts": [alert_json(a, names) for a in alerts],
        "remediations": [
            {"id": r.id, "playbook": r.playbook, "status": r.status, "created_at": r.created_at} for r in runs
        ],
    }


@router.post("/incidents/{incident_id}/ack")
async def ack_incident(incident_id: str, session: Session, user: Operator):
    """One-tap acknowledgement (mobile): acknowledges every open alert in the incident."""
    inc = await session.get(Incident, incident_id)
    if inc is None:
        raise HTTPException(404, "Incident not found")
    alerts = (
        (await session.execute(select(Alert).where(Alert.incident_id == inc.id, Alert.status == "open")))
        .scalars()
        .all()
    )
    for a in alerts:
        a.status, a.acknowledged_by = "acknowledged", user.username
    await session.commit()
    await audit.record(session, user.username, "incident.acknowledged", inc.title, {"alerts": len(alerts)})
    return {"acknowledged": len(alerts)}


@router.get("/predictions")
async def list_predictions(session: Session, _: Viewer, active: bool = True):
    q = select(Prediction).order_by(Prediction.eta)
    if active:
        q = q.where(Prediction.active.is_(True))
    names = await _names(session)
    return [
        {
            "id": p.id,
            "device_id": p.device_id,
            "device": names.get(p.device_id),
            "metric": p.metric,
            "kind": p.kind,
            "probability": p.probability,
            "eta": p.eta,
            "summary": p.summary,
            "detail": p.detail,
            "created_at": p.created_at,
        }
        for p in (await session.execute(q.limit(100))).scalars()
    ]
