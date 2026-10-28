import asyncio
import contextlib
import json

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import Admin, Operator, Session, Viewer
from app.cache import get_cache
from app.collectors.simulator import SCENARIOS
from app.config import get_settings
from app.db import engine
from app.models import AuditLog
from app.security import decode_token
from app.services import audit
from app.services.runtime import dispatch, get_runtime

router = APIRouter(tags=["system"])
ws_router = APIRouter()


class InjectBody(BaseModel):
    scenario: str
    device_id: str | None = None
    stubborn: bool = False


class ClearBody(BaseModel):
    device_id: str | None = None
    scenario: str | None = None


@router.get("/system/info")
async def info(_: Viewer):
    s = get_settings()
    rt = get_runtime()
    return {
        "app": s.app_name,
        "environment": s.environment,
        "database": engine.dialect.name,
        "cache": type(get_cache()).__name__,
        "workers": "in-process" if rt else "external",
        "simulator": s.simulator_enabled,
        "time_scale": s.time_scale,
        "auto_remediation": s.auto_remediation_enabled,
        "ready": rt.ready.is_set() if rt else None,
    }


@router.get("/audit")
async def audit_log(session: Session, _: Admin, limit: int = Query(100, le=1000), action: str | None = None):
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if action:
        q = q.where(AuditLog.action.like(f"{action}%"))
    return [
        {
            "id": e.id,
            "time": e.time,
            "actor": e.actor,
            "action": e.action,
            "target": e.target,
            "detail": e.detail,
            "hash": e.hash,
        }
        for e in (await session.execute(q)).scalars()
    ]


@router.get("/audit/verify")
async def verify_audit(session: Session, _: Admin):
    return await audit.verify_chain(session)


@router.get("/simulator")
async def simulator_state(_: Viewer):
    return {
        "enabled": get_settings().simulator_enabled,
        "scenarios": SCENARIOS,
        "faults": await get_cache().get_json("sim:faults") or [],
    }


@router.post("/simulator/inject")
async def inject(body: InjectBody, user: Operator):
    if body.scenario not in SCENARIOS:
        raise HTTPException(404, "Unknown scenario")
    try:
        return await dispatch(
            {
                "command": "inject",
                "scenario": body.scenario,
                "device_id": body.device_id,
                "stubborn": body.stubborn,
                "actor": user.username,
            }
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/simulator/clear")
async def clear(body: ClearBody, user: Operator):
    try:
        return await dispatch(
            {"command": "clear", "device_id": body.device_id, "scenario": body.scenario, "actor": user.username}
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@ws_router.websocket("/ws")
async def events(ws: WebSocket, token: str = Query(...)):
    """Real-time stream of metrics, alerts, incidents and remediation progress."""
    try:
        decode_token(token)
    except Exception:
        await ws.close(code=4401)
        return
    await ws.accept()

    async def pump() -> None:
        async for event in get_cache().subscribe():
            await ws.send_text(json.dumps(event, default=str))

    task = asyncio.create_task(pump())
    try:
        while True:
            await ws.receive_text()  # keep-alive pings from the client
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
