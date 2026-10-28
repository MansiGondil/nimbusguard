from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import Operator, Session, Viewer
from app.automation import engine
from app.automation.playbooks import load_all
from app.models import Device, PlaybookRun
from app.services.runtime import dispatch

router = APIRouter(tags=["remediation"])
PLAYBOOKS = load_all()


class RunRequest(BaseModel):
    playbook: str
    device_id: str
    context: dict = {}


class RejectBody(BaseModel):
    reason: str | None = None


def run_json(r: PlaybookRun, names: dict | None = None) -> dict:
    pb = PLAYBOOKS.get(r.playbook)
    return {
        "id": r.id,
        "playbook": r.playbook,
        "title": pb.title if pb else r.playbook,
        "risk": pb.risk if pb else None,
        "device_id": r.device_id,
        "device": (names or {}).get(r.device_id),
        "alert_id": r.alert_id,
        "status": r.status,
        "reason": r.reason,
        "gates": r.gates,
        "steps": r.steps,
        "driver": r.driver,
        "requested_by": r.requested_by,
        "approved_by": r.approved_by,
        "context": r.context,
        "created_at": r.created_at,
        "finished_at": r.finished_at,
    }


@router.get("/playbooks")
async def list_playbooks(_: Viewer):
    return [p.to_dict() for p in PLAYBOOKS.values()]


@router.get("/remediation/runs")
async def list_runs(session: Session, _: Viewer, status: str | None = None, limit: int = Query(100, le=500)):
    q = select(PlaybookRun).order_by(PlaybookRun.created_at.desc()).limit(limit)
    if status:
        q = q.where(PlaybookRun.status.in_(status.split(",")))
    names = dict((await session.execute(select(Device.id, Device.name))).all())
    return [run_json(r, names) for r in (await session.execute(q)).scalars()]


async def _run(session, run_id: str) -> PlaybookRun:
    run = await session.get(PlaybookRun, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return run


@router.get("/remediation/runs/{run_id}")
async def get_run(run_id: str, session: Session, _: Viewer):
    names = dict((await session.execute(select(Device.id, Device.name))).all())
    return run_json(await _run(session, run_id), names)


@router.post("/remediation/runs", status_code=202)
async def request_run(body: RunRequest, session: Session, user: Operator):
    if body.playbook not in PLAYBOOKS:
        raise HTTPException(404, "Unknown playbook")
    if not await session.get(Device, body.device_id):
        raise HTTPException(404, "Unknown device")
    return await dispatch(
        {
            "command": "request_run",
            "playbook": body.playbook,
            "device_id": body.device_id,
            "context": body.context,
            "actor": user.username,
        }
    )


@router.post("/remediation/runs/{run_id}/approve")
async def approve_run(run_id: str, session: Session, user: Operator):
    run = await _run(session, run_id)
    try:
        await engine.approve(session, run, user.username)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await dispatch({"command": "execute_run", "run_id": run.id, "actor": user.username})
    return run_json(run)


@router.post("/remediation/runs/{run_id}/reject")
async def reject_run(run_id: str, body: RejectBody, session: Session, user: Operator):
    run = await _run(session, run_id)
    try:
        await engine.reject(session, run, user.username, body.reason)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return run_json(run)
