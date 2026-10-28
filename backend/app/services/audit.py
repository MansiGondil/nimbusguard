"""Tamper-evident audit log: every entry hashes the previous entry's hash."""

import asyncio
import hashlib
import json

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, utcnow

GENESIS = "0" * 64
_lock = asyncio.Lock()


def _digest(prev_hash: str, entry: AuditLog) -> str:
    body = json.dumps(
        {
            "prev": prev_hash,
            "time": entry.time.isoformat(),
            "actor": entry.actor,
            "action": entry.action,
            "target": entry.target,
            "detail": entry.detail,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(body.encode()).hexdigest()


async def record(
    session: AsyncSession, actor: str, action: str, target: str | None = None, detail: dict | None = None
) -> AuditLog:
    """Append an audit entry and commit it."""
    async with _lock:
        if session.bind.dialect.name == "postgresql":
            # Serialise writers across processes (API + worker) for the chain.
            await session.execute(text("SELECT pg_advisory_xact_lock(724001)"))
        last = (await session.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(1))).scalar()
        prev = last.hash if last else GENESIS
        entry = AuditLog(
            time=utcnow().replace(microsecond=0),
            actor=actor,
            action=action,
            target=target,
            detail=json.loads(json.dumps(detail or {}, default=str)),
            prev_hash=prev,
        )
        entry.hash = _digest(prev, entry)
        session.add(entry)
        await session.commit()
        return entry


async def verify_chain(session: AsyncSession) -> dict:
    prev = GENESIS
    count = 0
    for entry in (await session.execute(select(AuditLog).order_by(AuditLog.id))).scalars():
        if entry.prev_hash != prev or _digest(prev, entry) != entry.hash:
            return {"valid": False, "entries": count, "broken_at": entry.id}
        prev = entry.hash
        count += 1
    return {"valid": True, "entries": count, "broken_at": None}
