"""Remediation engine: gate, approve, execute, verify and roll back playbooks."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.automation import safety
from app.automation.drivers import Driver
from app.automation.playbooks import Playbook, render
from app.models import Alert, Device, Incident, PlaybookRun, utcnow
from app.services import audit

log = logging.getLogger(__name__)

ACTIVE = ("pending_approval", "approved", "running")
EXECUTED = ("running", "succeeded", "failed", "rolled_back", "escalated")


def device_dict(d: Device) -> dict:
    return {
        "id": d.id,
        "name": d.name,
        "type": d.type,
        "ip": d.ip,
        "criticality": d.criticality,
        "maintenance_mode": d.maintenance_mode,
        "collector": d.collector,
        "capacity_mbps": d.capacity_mbps,
        "services": d.services or [],
    }


async def _publish_run(run: PlaybookRun) -> None:
    from app.cache import get_cache

    await get_cache().publish(
        {
            "type": "remediation",
            "run": {
                "id": run.id,
                "playbook": run.playbook,
                "device_id": run.device_id,
                "status": run.status,
                "steps": run.steps,
                "reason": run.reason,
            },
        }
    )


async def approve(session: AsyncSession, run: PlaybookRun, user: str) -> PlaybookRun:
    """Mark a held run approved. The caller then dispatches ``execute_run``."""
    if run.status != "pending_approval":
        raise ValueError(f"run is {run.status}, not pending approval")
    run.status, run.approved_by = "approved", user
    await session.commit()
    await audit.record(session, user, "remediation.approve", run.device_id, {"run_id": run.id})
    await _publish_run(run)
    return run


async def reject(session: AsyncSession, run: PlaybookRun, user: str, reason: str | None) -> PlaybookRun:
    if run.status != "pending_approval":
        raise ValueError(f"run is {run.status}, not pending approval")
    run.status, run.reason, run.finished_at = "rejected", reason or "Rejected by operator", utcnow()
    await session.commit()
    await audit.record(session, user, "remediation.reject", run.device_id, {"run_id": run.id, "reason": reason})
    await _publish_run(run)
    return run


class RemediationEngine:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        playbooks: dict[str, Playbook],
        driver_for: Callable[[dict], Driver],
        latest: Callable[[str], Awaitable[dict | None]],
        downstream_count: Callable[[str], int],
        publish: Callable[[dict], Awaitable[None]],
        notify: Callable[..., Awaitable[None]],
        settings,
    ) -> None:
        self.session_factory = session_factory
        self.playbooks = playbooks
        self.driver_for = driver_for
        self.latest = latest
        self.downstream_count = downstream_count
        self.publish = publish
        self.notify = notify
        self.settings = settings
        self.tasks: set[asyncio.Task] = set()

    def playbook_for(self, signature: str, kind: str | None = None) -> Playbook | None:
        return next(
            (
                p
                for p in self.playbooks.values()
                if signature in p.triggers and (not p.kinds or kind is None or kind in p.kinds)
            ),
            None,
        )

    async def _event(self, run: PlaybookRun) -> None:
        await self.publish(
            {
                "type": "remediation",
                "run": {
                    "id": run.id,
                    "playbook": run.playbook,
                    "device_id": run.device_id,
                    "status": run.status,
                    "steps": run.steps,
                    "reason": run.reason,
                },
            }
        )

    # ------------------------------------------------------------------ request
    async def request(
        self,
        session: AsyncSession,
        playbook_name: str,
        device: Device,
        alert: Alert | None = None,
        requested_by: str = "system",
        context: dict | None = None,
    ) -> PlaybookRun | None:
        pb = self.playbooks.get(playbook_name)
        if pb is None:
            raise ValueError(f"unknown playbook {playbook_name!r}")
        duplicate = (
            await session.execute(
                select(PlaybookRun.id).where(
                    PlaybookRun.device_id == device.id, PlaybookRun.playbook == pb.name, PlaybookRun.status.in_(ACTIVE)
                )
            )
        ).first()
        if duplicate:
            return None
        hour_ago = utcnow() - timedelta(hours=1)
        recent = (
            await session.execute(
                select(func.count())
                .select_from(PlaybookRun)
                .where(
                    PlaybookRun.device_id == device.id,
                    PlaybookRun.created_at >= hour_ago,
                    PlaybookRun.status.in_(EXECUTED),
                )
            )
        ).scalar_one()
        s = self.settings
        cooldown = utcnow() - timedelta(minutes=s.remediation_cooldown_minutes / s.time_scale)
        recently_succeeded = (
            requested_by == "system"
            and (
                await session.execute(
                    select(PlaybookRun.id).where(
                        PlaybookRun.device_id == device.id,
                        PlaybookRun.playbook == pb.name,
                        PlaybookRun.status == "succeeded",
                        PlaybookRun.finished_at >= cooldown,
                    )
                )
            ).first()
            is not None
        )
        gates = safety.evaluate(
            now=datetime.now(UTC),
            playbook=pb,
            device=device_dict(device),
            recent_runs=recent,
            downstream=self.downstream_count(device.id),
            automation_enabled=s.auto_remediation_enabled or requested_by != "system",
            max_per_hour=s.max_fixes_per_device_per_hour,
            impact_threshold=s.impact_approval_threshold,
            window=s.maintenance_window,
            recently_succeeded=recently_succeeded,
        )
        status = safety.decision(gates)
        if requested_by != "system" and status == "pending_approval":
            # A human operator requesting the run is itself the approval.
            status, approved_by = "approved", requested_by
        else:
            approved_by = "auto" if status == "approved" else None
        reasons = [g.detail for g in gates if g.effect]
        run = PlaybookRun(
            playbook=pb.name,
            device_id=device.id,
            alert_id=alert.id if alert else None,
            status=status,
            reason="; ".join(reasons) or None,
            gates=[g.to_dict() for g in gates],
            steps=[],
            requested_by=requested_by,
            approved_by=approved_by,
            context=context or {},
        )
        session.add(run)
        await session.commit()
        await audit.record(
            session,
            requested_by,
            f"remediation.{status}",
            device.name,
            {"run_id": run.id, "playbook": pb.name, "gates": run.gates},
        )
        await self._event(run)
        if status == "pending_approval":
            await self.notify(
                f"Approval needed: {pb.title} on {device.name}", run.reason or "", priority="high", tags=["warning"]
            )
        elif status == "approved":
            self.spawn(run.id)
        return run

    def spawn(self, run_id: str) -> asyncio.Task:
        task = asyncio.create_task(self.execute(run_id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    # ------------------------------------------------------------------ execute
    async def _verify(self, device: dict, params: dict) -> dict:
        attempts, delay = 3, max(1.0, 10 / self.settings.time_scale)
        result: dict = {}
        for attempt in range(attempts):
            latest = await self.latest(device["id"])
            if "reachable" in params:
                fresh = latest and (
                    datetime.now(UTC) - datetime.fromisoformat(latest["time"])
                ).total_seconds() < 3 * max(self.settings.simulator_interval_seconds, 5)
                result = {"ok": bool(fresh) == bool(params["reachable"]), "reachable": bool(fresh)}
            else:
                values = dict((latest or {}).get("values", {}))
                if "traffic_in" in values:
                    values["utilization_in"] = values["traffic_in"] / (device.get("capacity_mbps") or 1000)
                metric = params["metric"]
                value = values.get(metric)
                ok = value is not None and (
                    ("below" not in params or value < params["below"])
                    and ("above" not in params or value > params["above"])
                )
                result = {
                    "ok": ok,
                    "metric": metric,
                    "observed": None if value is None else round(value, 3),
                    "target": {k: params[k] for k in ("below", "above") if k in params},
                }
            if result["ok"]:
                break
            if attempt < attempts - 1:
                await asyncio.sleep(delay)
        return result

    async def _run_steps(self, steps, ctx, device, driver, run, session, phase: str) -> tuple[str, str | None]:
        """Execute steps; returns (outcome, message) with outcome ok|failed|escalated."""
        for step in steps:
            if step.when and not ctx.get(step.when):
                continue
            if step.unless and ctx.get(step.unless):
                continue
            params = render(step.params, ctx)
            record = {
                "phase": phase,
                "action": step.action,
                "params": params,
                "started_at": utcnow().isoformat(),
                "status": "ok",
            }
            outcome, message = "ok", None
            try:
                if step.action == "wait":
                    seconds = float(params.get("seconds", 30)) / self.settings.time_scale
                    await asyncio.sleep(seconds)
                    result = {"waited_seconds": round(seconds, 1)}
                elif step.action == "verify":
                    result = await self._verify(device, params)
                    if not result["ok"]:
                        outcome, message = "failed", f"Verification failed: {result}"
                elif step.action in ("notify", "escalate"):
                    msg = params.get("message", f"{run.playbook} on {device['name']}")
                    await self.notify(
                        f"NimbusGuard: {device['name']}",
                        msg,
                        priority="high" if step.action == "escalate" else "default",
                    )
                    result = {"message": msg}
                    if step.action == "escalate":
                        outcome, message = "escalated", msg
                else:
                    result = await driver.execute(step.action, device, params)
            except Exception as exc:  # driver/IO failure
                log.exception("Step %s failed", step.action)
                result, outcome, message = {"error": str(exc)}, "failed", f"{step.action} raised: {exc}"
            record.update(
                result=result,
                finished_at=utcnow().isoformat(),
                status={"ok": "ok", "failed": "failed", "escalated": "escalated"}[outcome],
            )
            ctx.update({k: v for k, v in result.items() if k != "ok"})
            run.steps = [*run.steps, record]
            await session.commit()
            await self._event(run)
            if outcome != "ok":
                return outcome, message
        return "ok", None

    async def execute(self, run_id: str) -> None:
        async with self.session_factory() as session:
            run = await session.get(PlaybookRun, run_id)
            if run is None or run.status != "approved":
                return
            device = await session.get(Device, run.device_id)
            pb = self.playbooks[run.playbook]
            dev = device_dict(device)
            driver = self.driver_for(dev)
            run.status, run.driver = "running", driver.name
            await session.commit()
            await self._event(run)
            await audit.record(
                session,
                run.approved_by or "system",
                "remediation.start",
                device.name,
                {"run_id": run.id, "playbook": pb.name, "driver": driver.name},
            )
            ctx = {
                "device": device.name,
                "device_id": device.id,
                "ip": device.ip,
                "primary_service": (device.services or ["application"])[0],
                **(run.context or {}),
            }

            outcome, message = await self._run_steps(pb.steps, ctx, dev, driver, run, session, "main")
            if outcome == "ok":
                run.status = "succeeded"
            elif outcome == "escalated":
                run.status, run.reason = "escalated", message
            else:
                rb, _ = await self._run_steps(pb.rollback, ctx, dev, driver, run, session, "rollback")
                run.status = "rolled_back" if rb == "ok" else "failed"
                run.reason = message
            run.finished_at = utcnow()
            if run.status == "succeeded" and run.alert_id:
                alert = await session.get(Alert, run.alert_id)
                if alert and alert.incident_id:
                    incident = await session.get(Incident, alert.incident_id)
                    if incident:
                        incident.prevented = True
                        incident.status = "mitigating" if incident.status == "open" else incident.status
            await session.commit()
            await self._event(run)
            await audit.record(
                session,
                "system",
                f"remediation.{run.status}",
                device.name,
                {"run_id": run.id, "playbook": pb.name, "reason": run.reason},
            )
            if run.status != "succeeded":
                await self.notify(
                    f"Remediation {run.status}: {pb.title}",
                    f"{device.name}: {run.reason}",
                    priority="high",
                    tags=["rotating_light"],
                )
