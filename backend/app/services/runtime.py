"""Runtime: wires collectors, pipeline and remediation engine together.

Runs inside the API process (``NG_RUN_WORKERS=true``, the default demo mode)
or as a standalone worker (``python -m app.worker``). The API talks to a
remote worker through the command channel on Redis.
"""

import asyncio
import contextlib
import logging
import random
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.automation.drivers import DryRunDriver, SimulatorDriver
from app.automation.engine import RemediationEngine
from app.automation.playbooks import load_all
from app.cache import COMMANDS, get_cache
from app.collectors import netflow, syslog
from app.collectors.base import CollectorBatch
from app.collectors.cloud import CloudWatchCollector
from app.collectors.simulator import SCENARIOS, NetworkSimulator, SimDevice
from app.collectors.snmp import SnmpPoller
from app.config import get_settings
from app.db import SessionLocal, init_db, startup_lock
from app.models import Device, Metric
from app.security import decrypt_credentials
from app.services import audit
from app.services.notifier import notify
from app.services.pipeline import Pipeline
from app.services.seed import seed

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.cache = get_cache()
        self.pipeline = Pipeline(self.settings, self.cache)
        self.simulator: NetworkSimulator | None = None
        self.playbooks = load_all()
        self.engine: RemediationEngine | None = None
        self.tasks: list[asyncio.Task] = []
        self.transports: list = []
        self.ready = asyncio.Event()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        async with startup_lock():
            await init_db()
            await seed()
        await self.pipeline.load()
        self._build_simulator()
        self.engine = RemediationEngine(
            SessionLocal,
            self.playbooks,
            self._driver_for,
            self._latest,
            lambda d: len(self.pipeline.topology.descendants(d)),
            self.cache.publish,
            notify,
            self.settings,
        )
        self.pipeline.engine = self.engine
        self.tasks.append(asyncio.create_task(self._bootstrap()))
        self.tasks.append(asyncio.create_task(self._listen_commands()))

    async def _bootstrap(self) -> None:
        try:
            if self.simulator and self.settings.simulator_backfill_days > 0:
                async with SessionLocal() as s:
                    has_history = (await s.execute(select(func.count()).select_from(Metric))).scalar_one()
                if not has_history:
                    await self.pipeline.backfill(self.simulator, self.settings.simulator_backfill_days)
            await self.pipeline.train_multivariate()
        except Exception:
            log.exception("Bootstrap failed")
        self.ready.set()
        loops = [
            self._loop(self.pipeline.flush, self.settings.persist_interval_seconds, "flush"),
            self._loop(self.pipeline.analyze, self.settings.analysis_interval_seconds, "analyze"),
        ]
        if self.simulator:
            loops.append(self._simulate())
            if self.settings.simulator_chaos_minutes > 0:
                loops.append(self._chaos())
        if self.settings.snmp_enabled:
            loops.append(self._loop(self._poll_snmp, self.settings.snmp_interval_seconds, "snmp"))
        if self.settings.cloud_enabled:
            loops.append(self._loop(self._poll_cloud, self.settings.cloud_interval_seconds, "cloud"))
        if self.settings.syslog_enabled:
            self.transports.append(await syslog.serve(self.settings.syslog_port, self._on_log))
        if self.settings.netflow_enabled:
            self.transports.append(await netflow.serve(self.settings.netflow_port, self._on_flows))
        self.tasks += [asyncio.create_task(c) for c in loops]

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        for tr in self.transports:
            tr.close()
        with contextlib.suppress(Exception):
            await self.pipeline.flush()

    async def _loop(self, fn, interval: float, name: str) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await fn()
            except Exception:
                log.exception("%s loop iteration failed", name)

    # ------------------------------------------------------------------ simulator
    def _build_simulator(self) -> None:
        if not self.settings.simulator_enabled:
            return
        sims = [
            SimDevice(d["id"], d["name"], d["type"], d["ip"] or "", d["capacity_mbps"] or 1000)
            for d in self.pipeline.devices.values()
            if d["collector"] == "simulated"
        ]
        links = [(u, d) for d, ups in self.pipeline.topology.parents.items() for u in ups]
        self.simulator = NetworkSimulator(sims, links, time_scale=self.settings.time_scale)

    async def _simulate(self) -> None:
        while True:
            try:
                await self.pipeline.ingest(self.simulator.tick(datetime.now(UTC)))
            except Exception:
                log.exception("Simulator tick failed")
            await asyncio.sleep(self.settings.simulator_interval_seconds)

    async def _chaos(self) -> None:
        rng = random.Random()
        while True:
            await asyncio.sleep(self.settings.simulator_chaos_minutes * 60 * rng.uniform(0.7, 1.3))
            if not self.simulator.faults:
                scenario = rng.choice([k for k in SCENARIOS if k != "device_down"])
                await self.handle({"command": "inject", "scenario": scenario, "actor": "chaos-monkey"})

    async def _publish_faults(self) -> None:
        faults = [
            {
                "kind": f.kind,
                "device_id": f.device_id,
                "device": self.pipeline.devices[f.device_id]["name"],
                "started": f.started.isoformat(),
                "stubborn": f.stubborn,
            }
            for f in (self.simulator.faults.values() if self.simulator else [])
        ]
        await self.cache.set_json("sim:faults", faults)
        await self.cache.publish({"type": "simulator", "faults": faults})

    # ------------------------------------------------------------------ collectors
    async def _poll_snmp(self) -> None:
        poller = self._snmp = getattr(self, "_snmp", None) or SnmpPoller()
        async with SessionLocal() as s:
            devices = (await s.execute(select(Device).where(Device.collector == "snmp"))).scalars().all()
        polls = []
        for d in devices:
            creds = decrypt_credentials(d.credentials_enc)
            if d.ip:
                polls.append(
                    poller.poll(
                        d.id,
                        d.ip,
                        creds.get("snmp_community", "public"),
                        creds.get("snmp_profile", "net-snmp"),
                        d.capacity_mbps,
                    )
                )
        results = await asyncio.gather(*polls)
        batch = CollectorBatch(samples=[r for r in results if r])
        if batch.samples:
            await self.pipeline.ingest(batch)

    async def _poll_cloud(self) -> None:
        collectors = self._cloud = getattr(self, "_cloud", {})
        async with SessionLocal() as s:
            devices = (await s.execute(select(Device).where(Device.collector == "cloud"))).scalars().all()
        polls = []
        for d in devices:
            creds = decrypt_credentials(d.credentials_enc)
            if not creds.get("instance_id"):
                continue
            region = creds.get("region", "us-east-1")
            if region not in collectors:
                collectors[region] = CloudWatchCollector(region)
            polls.append(collectors[region].collect(d.id, creds))
        results = await asyncio.gather(*polls)
        batch = CollectorBatch(samples=[r for r in results if r])
        if batch.samples:
            await self.pipeline.ingest(batch)

    async def _on_log(self, event) -> None:
        await self.pipeline.ingest(CollectorBatch(logs=[event]))

    async def _on_flows(self, flows) -> None:
        await self.pipeline.ingest(CollectorBatch(flows=flows))

    # ------------------------------------------------------------------ engine hooks
    def _driver_for(self, device: dict):
        if device.get("collector") == "simulated" and self.simulator:
            return SimulatorDriver(self.simulator, on_change=self._publish_faults)
        return DryRunDriver()

    async def _latest(self, device_id: str) -> dict | None:
        return self.pipeline.latest.get(device_id)

    # ------------------------------------------------------------------ commands
    async def _listen_commands(self) -> None:
        async for cmd in self.cache.subscribe(COMMANDS):
            try:
                await self.handle(cmd)
            except Exception:
                log.exception("Command %s failed", cmd)

    async def handle(self, cmd: dict) -> dict:
        kind = cmd.get("command")
        actor = cmd.get("actor", "system")
        if kind == "execute_run":
            self.engine.spawn(cmd["run_id"])
            return {"ok": True}
        if kind == "request_run":
            async with SessionLocal() as s:
                device = await s.get(Device, cmd["device_id"])
                run = await self.engine.request(s, cmd["playbook"], device, None, actor, cmd.get("context"))
            return {
                "ok": run is not None,
                "run_id": run.id if run else None,
                "status": run.status if run else "duplicate",
            }
        if kind == "reload_devices":
            await self.pipeline.load()
            old = self.simulator
            self._build_simulator()
            if old and self.simulator:
                self.simulator.faults = {k: f for k, f in old.faults.items() if f.device_id in self.simulator.devices}
            return {"ok": True}
        if not self.simulator:
            raise ValueError("simulator is disabled")
        if kind == "inject":
            fault = self.simulator.inject(cmd["scenario"], cmd.get("device_id"), stubborn=cmd.get("stubborn", False))
            await self._publish_faults()
            async with SessionLocal() as s:
                await audit.record(
                    s,
                    actor,
                    "simulator.inject",
                    self.pipeline.devices[fault.device_id]["name"],
                    {"scenario": fault.kind, "stubborn": fault.stubborn},
                )
            return {"ok": True, "device_id": fault.device_id, "scenario": fault.kind}
        if kind == "clear":
            n = self.simulator.clear(cmd.get("device_id"), cmd.get("scenario"))
            await self._publish_faults()
            async with SessionLocal() as s:
                await audit.record(s, actor, "simulator.clear", cmd.get("device_id"), {"cleared": n})
            return {"ok": True, "cleared": n}
        raise ValueError(f"unknown command {kind!r}")


_runtime: Runtime | None = None


def get_runtime() -> Runtime | None:
    return _runtime


def set_runtime(rt: Runtime | None) -> None:
    global _runtime
    _runtime = rt


async def dispatch(cmd: dict) -> dict:
    """Run a command locally if workers live in this process, else send it to the worker."""
    if _runtime is not None:
        return await _runtime.handle(cmd)
    await get_cache().publish(cmd, COMMANDS)
    return {"ok": True, "queued": True}
