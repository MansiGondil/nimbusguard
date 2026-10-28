"""Ingest -> detect -> alert -> correlate -> remediate.

``Pipeline`` is the stateful core that every collector feeds. It persists
telemetry (downsampled), keeps the latest state in the cache, runs the
streaming detectors, and periodically runs the slower analysis jobs
(predictions, availability checks, root-cause correlation).
"""

import asyncio
import ipaddress
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import delete, select

from app.automation.engine import RemediationEngine
from app.cache import MemoryCache
from app.collectors.base import CollectorBatch, Flow, LogEvent, Sample
from app.config import Settings
from app.db import SessionLocal
from app.intelligence import prediction
from app.intelligence.anomaly import Detection, Detector
from app.intelligence.baseline import BaselineModel
from app.intelligence.health import network_health
from app.intelligence.rca import AlertView, Topology, correlate
from app.models import (
    Alert,
    Baseline,
    Device,
    DeviceLink,
    FlowRecord,
    Incident,
    Metric,
    Prediction,
    SyslogEvent,
    utcnow,
)
from app.services import audit
from app.services.notifier import notify

log = logging.getLogger(__name__)

BRUTE_FORCE_WINDOW = timedelta(minutes=5)
BRUTE_FORCE_THRESHOLD = 20
EXFIL_WINDOW = timedelta(minutes=5)
EXFIL_BYTES = 500_000_000


def _is_internal(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


class Pipeline:
    def __init__(self, settings: Settings, cache: MemoryCache) -> None:
        self.settings = settings
        self.cache = cache
        self.baseline = BaselineModel(settings.baseline_min_samples)
        self.detector = Detector(
            self.baseline, settings.anomaly_z_threshold, settings.anomaly_consecutive, time_scale=settings.time_scale
        )
        self.devices: dict[str, dict] = {}
        self.by_name: dict[str, str] = {}
        self.by_ip: dict[str, str] = {}
        self.topology = Topology([])
        self.latest: dict[str, dict] = {}
        self.engine: RemediationEngine | None = None
        self._agg: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0])
        self._agg_time: dict[str, datetime] = {}
        self._auth_failures: dict[str, deque] = defaultdict(deque)
        self._exfil: dict[tuple[str, str], deque] = defaultdict(deque)
        self._security_active: dict[str, datetime] = {}
        self._lock = asyncio.Lock()
        self._last_health_persist: datetime | None = None

    # ------------------------------------------------------------------ setup
    async def load(self) -> None:
        async with SessionLocal() as s:
            devices = (await s.execute(select(Device))).scalars().all()
            links = (await s.execute(select(DeviceLink))).scalars().all()
            for b in (await s.execute(select(Baseline))).scalars():
                self.baseline.load_row(b.device_id, b.metric, b.bucket, b.count, b.mean, b.m2)
            open_alerts = (await s.execute(select(Alert).where(Alert.status != "resolved"))).scalars().all()
        # Rehydrate detector state from alerts left open by a previous process,
        # so they clear (or keep refreshing) instead of staying open forever.
        for a in open_alerts:
            det = Detection(
                a.device_id,
                a.metric,
                a.kind,
                a.severity,
                (a.detail or {}).get("signature", a.kind),
                a.title,
                None,
                detail=dict(a.detail or {}),
            )
            self.detector.active.setdefault(a.fingerprint, det)
            if a.kind == "security":
                self._security_active.setdefault(a.fingerprint, utcnow())
        self.devices = {
            d.id: {
                "id": d.id,
                "name": d.name,
                "type": d.type,
                "ip": d.ip,
                "criticality": d.criticality,
                "capacity_mbps": d.capacity_mbps,
                "collector": d.collector,
                "status": d.status,
            }
            for d in devices
        }
        self.by_name = {d.name: d.id for d in devices}
        self.by_ip = {d.ip: d.id for d in devices if d.ip}
        self.topology = Topology(
            [(lnk.upstream_id, lnk.downstream_id) for lnk in links if lnk.kind == "primary"],
            {d.id: d.name for d in devices},
        )

    async def train_multivariate(self, hours: int = 48) -> None:
        """Fit the Isolation Forest on recent history (z-score vectors)."""
        since = utcnow() - timedelta(hours=hours)
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(Metric.time, Metric.device_id, Metric.metric, Metric.value).where(Metric.time >= since)
                )
            ).all()
        frames: dict[tuple[str, datetime], dict[str, float]] = defaultdict(dict)
        for t, dev, metric, value in rows:
            if dev in self.devices:
                frames[(dev, t)][metric] = value
        vectors = [self.detector.z_vector(dev, t, vals) for (dev, t), vals in frames.items()]
        if len(vectors) > 20_000:
            idx = np.random.default_rng(0).choice(len(vectors), 20_000, replace=False)
            vectors = [vectors[i] for i in idx]
        await asyncio.to_thread(self.detector.multivariate.fit, vectors)
        log.info("Isolation Forest trained on %d vectors", len(vectors))

    # ------------------------------------------------------------------ ingest
    async def ingest(self, batch: CollectorBatch) -> None:
        async with self._lock:
            raised: list[Detection] = []
            cleared: list[str] = []
            for sample in batch.samples:
                r, c = self._observe(sample)
                raised += r
                cleared += c
            await self._publish_latest(batch.samples)
            async with SessionLocal() as s:
                if batch.logs:
                    s.add_all(self._log_rows(batch.logs))
                if batch.flows:
                    s.add_all(self._flow_rows(batch.flows))
                await s.commit()
                raised += self._security_from_logs(batch.logs) + self._security_from_flows(batch.flows)
                for det in raised:
                    await self._raise(s, det)
                for key in cleared:
                    await self._clear(s, key)
            if batch.logs:
                await self.cache.publish(
                    {
                        "type": "logs",
                        "events": [
                            {
                                "time": e.time.isoformat(),
                                "host": e.host,
                                "severity": e.severity,
                                "message": e.message,
                                "category": e.category,
                            }
                            for e in batch.logs[-20:]
                        ],
                    }
                )

    def _observe(self, sample: Sample) -> tuple[list[Detection], list[str]]:
        dev = self.devices.get(sample.device_id)
        if dev is None:
            return [], []
        self.latest[sample.device_id] = {"time": sample.time.isoformat(), "values": sample.values}
        for metric, value in sample.values.items():
            acc = self._agg[(sample.device_id, metric)]
            acc[0] += value
            acc[1] += 1
        self._agg_time.setdefault(sample.device_id, sample.time)
        raised, cleared = [], []
        for kind, item in self.detector.observe(
            sample.device_id, sample.time, sample.values, dev.get("capacity_mbps") or 1000.0
        ):
            (raised if kind == "raise" else cleared).append(item)
        return raised, cleared

    async def _publish_latest(self, samples: list[Sample]) -> None:
        if not samples:
            return
        await self.cache.set_json("latest:all", self.latest)
        for sm in samples:
            await self.cache.set_json(f"latest:{sm.device_id}", self.latest[sm.device_id], ttl=3600)
        await self.cache.publish(
            {
                "type": "metrics",
                "time": samples[-1].time.isoformat(),
                "devices": {sm.device_id: sm.values for sm in samples},
            }
        )

    def _log_rows(self, logs: list[LogEvent]) -> list[SyslogEvent]:
        rows = []
        for e in logs:
            device_id = e.device_id or self.by_name.get(e.host) or self.by_ip.get(e.host)
            e.device_id = device_id
            rows.append(
                SyslogEvent(
                    time=e.time,
                    device_id=device_id,
                    host=e.host,
                    severity=e.severity,
                    facility=e.facility,
                    category=e.category,
                    source_ip=e.source_ip,
                    message=e.message[:2000],
                )
            )
        return rows

    def _flow_rows(self, flows: list[Flow]) -> list[FlowRecord]:
        return [
            FlowRecord(
                time=f.time,
                device_id=f.device_id,
                src_ip=f.src_ip,
                dst_ip=f.dst_ip,
                src_port=f.src_port,
                dst_port=f.dst_port,
                protocol=f.protocol,
                bytes=min(f.bytes, 2**31 - 1),
                packets=min(f.packets, 2**31 - 1),
            )
            for f in flows
        ]

    # ------------------------------------------------------------------ security
    def _security_from_logs(self, logs: list[LogEvent]) -> list[Detection]:
        out = []
        touched = set()
        for e in logs:
            if e.category != "auth" or "fail" not in e.message.lower() or not e.device_id:
                continue
            self._auth_failures[e.device_id].append((e.time, e.source_ip))
            touched.add(e.device_id)
        now = utcnow()
        for device_id, q in self._auth_failures.items():
            while q and q[0][0] < now - BRUTE_FORCE_WINDOW:
                q.popleft()
            key = f"{device_id}:security:brute_force"
            if device_id in touched and len(q) >= BRUTE_FORCE_THRESHOLD:
                counts: dict[str, int] = defaultdict(int)
                for _, ip in q:
                    if ip:
                        counts[ip] += 1
                attackers = sorted(
                    (ip for ip, n in counts.items() if n >= 3 and not _is_internal(ip)), key=lambda ip: -counts[ip]
                )
                fresh = key not in self._security_active
                self._security_active[key] = now
                if fresh and attackers:
                    out.append(
                        Detection(
                            device_id,
                            None,
                            "security",
                            "critical",
                            "brute_force",
                            f"Possible brute-force attack: {len(q)} failed logins in 5 min",
                            float(len(q)),
                            5.0,
                            1.0,
                            {"source_ips": attackers, "attempts": dict(counts)},
                        )
                    )
        return out

    def _security_from_flows(self, flows: list[Flow]) -> list[Detection]:
        out = []
        now = utcnow()
        touched = set()
        for f in flows:
            if _is_internal(f.src_ip) and not _is_internal(f.dst_ip):
                self._exfil[(f.src_ip, f.dst_ip)].append((f.time, f.bytes, f.device_id))
                touched.add((f.src_ip, f.dst_ip))
        for (src, dst), q in list(self._exfil.items()):
            while q and q[0][0] < now - EXFIL_WINDOW:
                q.popleft()
            if not q:
                del self._exfil[(src, dst)]
                continue
            total = sum(b for _, b, _ in q)
            if (src, dst) not in touched or total < EXFIL_BYTES:
                continue
            device_id = next((d for *_, d in q if d), None) or self.by_ip.get(src)
            if not device_id:
                continue
            key = f"{device_id}:security:exfiltration"
            fresh = key not in self._security_active
            self._security_active[key] = now
            if fresh:
                out.append(
                    Detection(
                        device_id,
                        None,
                        "security",
                        "critical",
                        "exfiltration",
                        f"Suspicious upload: {total / 1e9:.1f} GB from {src} to {dst} in 5 min",
                        float(total),
                        None,
                        1.0,
                        {"source_ip": src, "destination_ip": dst, "bytes": total},
                    )
                )
        return out

    # ------------------------------------------------------------------ alerts
    async def _raise(self, s, det: Detection) -> None:
        alert = (
            await s.execute(select(Alert).where(Alert.fingerprint == det.key, Alert.status != "resolved"))
        ).scalar()
        now = utcnow()
        if alert:
            alert.last_seen, alert.score, alert.detail = now, det.score, {**alert.detail, **det.detail}
            await s.commit()
            return
        alert = Alert(
            fingerprint=det.key,
            device_id=det.device_id,
            kind=det.kind,
            metric=det.metric,
            severity=det.severity,
            title=det.title,
            score=det.score,
            detail={**det.detail, "signature": det.signature, "value": det.value, "expected": det.expected},
            first_seen=now,
            last_seen=now,
        )
        s.add(alert)
        await s.commit()
        await self.cache.publish({"type": "alert", "action": "raised", "alert": _alert_json(alert)})
        name = self.devices.get(det.device_id, {}).get("name", det.device_id)
        if det.severity == "critical":
            await notify(f"[CRITICAL] {name}", det.title, priority="high", tags=["rotating_light"])
        await self._maybe_remediate(s, alert, det)

    async def _maybe_remediate(self, s, alert: Alert, det: Detection) -> None:
        if self.engine is None or not alert.device_id:
            return
        pb = self.engine.playbook_for(det.signature, det.kind)
        if pb is None:
            return
        device = await s.get(Device, alert.device_id)
        if device is None:
            return
        context = {k: v for k, v in det.detail.items() if k in ("source_ips", "source_ip", "destination_ip")}
        await self.engine.request(s, pb.name, device, alert, "system", context)

    async def _clear(self, s, key: str) -> None:
        alert = (await s.execute(select(Alert).where(Alert.fingerprint == key, Alert.status != "resolved"))).scalar()
        if alert is None:
            return
        alert.status, alert.resolved_at = "resolved", utcnow()
        await s.commit()
        await self.cache.publish({"type": "alert", "action": "resolved", "alert": _alert_json(alert)})

    # ------------------------------------------------------------------ periodic
    async def flush(self) -> None:
        """Persist downsampled metrics, dirty baselines and device status."""
        async with self._lock:
            agg, self._agg = self._agg, defaultdict(lambda: [0.0, 0])
            times, self._agg_time = self._agg_time, {}
            dirty = self.baseline.drain_dirty()
        rows = [
            Metric(time=times[dev].replace(microsecond=0), device_id=dev, metric=m, value=round(tot / n, 3))
            for (dev, m), (tot, n) in agg.items()
            if n
        ]
        async with SessionLocal() as s:
            if rows:
                s.add_all(rows)
            for dev, metric, bucket, st in dirty:
                await s.merge(
                    Baseline(
                        device_id=dev,
                        metric=metric,
                        bucket=bucket,
                        count=st.count,
                        mean=st.mean,
                        m2=st.m2,
                        updated_at=utcnow(),
                    )
                )
            await s.commit()

    async def analyze(self) -> None:
        async with SessionLocal() as s:
            await self._availability(s)
            await self._expire_security(s)
            await self._predict(s)
            await self._correlate(s)
            await self._update_status(s)

    async def _availability(self, s) -> None:
        now = utcnow()
        stale_after = timedelta(seconds=max(3 * self.settings.simulator_interval_seconds, 30))
        if any(d["collector"] == "snmp" for d in self.devices.values()):
            stale_after = max(stale_after, timedelta(seconds=3 * self.settings.snmp_interval_seconds))
        down = set()
        for dev_id in self.devices:
            latest = self.latest.get(dev_id)
            if latest and now - datetime.fromisoformat(latest["time"]) > stale_after:
                down.add(dev_id)
        for dev_id in self.devices:
            key = f"{dev_id}:availability:device_down"
            if dev_id in down:
                if key in self.detector.active:
                    continue
                det = Detection(
                    dev_id,
                    None,
                    "availability",
                    "critical",
                    "device_down",
                    f"{self.devices[dev_id]['name']} is unreachable",
                    None,
                )
                self.detector.active[key] = det
                # Only the most-upstream unreachable device gets remediated.
                collateral = bool(self.topology.ancestors(dev_id) & down)
                det.detail = {"collateral": collateral}
                engine, self.engine = self.engine, (None if collateral else self.engine)
                try:
                    await self._raise(s, det)
                finally:
                    self.engine = engine
            elif key in self.detector.active:
                self.detector.active.pop(key)
                await self._clear(s, key)

    async def _expire_security(self, s) -> None:
        now = utcnow()
        quiet = timedelta(seconds=max(60, 4 * self.settings.simulator_interval_seconds))
        for key, last in list(self._security_active.items()):
            if now - last > quiet:
                self._security_active.pop(key)
                await self._clear(s, key)

    async def _predict(self, s) -> None:
        lookback = utcnow() - timedelta(minutes=self.settings.prediction_lookback_minutes)
        horizon = timedelta(hours=self.settings.prediction_horizon_hours)
        rows = (
            await s.execute(
                select(Metric.device_id, Metric.metric, Metric.time, Metric.value)
                .where(
                    Metric.time >= lookback,
                    Metric.metric.in_(
                        ["memory", "disk", "traffic_in", "cpu", "if_errors", "packet_loss", "temperature"]
                    ),
                )
                .order_by(Metric.time)
            )
        ).all()
        series: dict[str, dict[str, tuple[list, list]]] = defaultdict(lambda: defaultdict(lambda: ([], [])))
        for dev, metric, t, v in rows:
            ts, vs = series[dev][metric]
            ts.append(t)
            vs.append(v)
        seen: set[str] = set()
        for dev_id, metrics in series.items():
            dev = self.devices.get(dev_id)
            if not dev:
                continue
            forecasts = []
            for metric, threshold in (("memory", 98.0), ("disk", 95.0)):
                if metric in metrics:
                    f = prediction.time_to_threshold(*metrics[metric], threshold, horizon, metric)
                    if f:
                        forecasts.append(f)
            if "traffic_in" in metrics:
                f = prediction.time_to_threshold(
                    *metrics["traffic_in"],
                    0.95 * dev["capacity_mbps"],
                    horizon,
                    "traffic_in",
                    kind="saturation",
                    min_rise=0.1 * dev["capacity_mbps"],
                )
                if f:
                    forecasts.append(f)
            if f := prediction.failure_risk(dict(metrics), self.settings.prediction_horizon_hours):
                forecasts.append(f)
            for f in forecasts:
                fp = _prediction_fp(dev_id, f.metric)
                seen.add(fp)
                await self._upsert_prediction(s, dev_id, fp, f)
        # Deactivate forecasts that no longer hold.
        for p in (await s.execute(select(Prediction).where(Prediction.active.is_(True)))).scalars():
            fp = _prediction_fp(p.device_id, p.metric)
            if fp not in seen:
                p.active = False
                self.detector.active.pop(fp, None)
                await self._clear(s, fp)
        await s.commit()

    async def _upsert_prediction(self, s, dev_id: str, fp: str, f: prediction.Forecast) -> None:
        existing = (
            await s.execute(
                select(Prediction).where(
                    Prediction.device_id == dev_id,
                    Prediction.active.is_(True),
                    Prediction.kind == f.kind,
                    Prediction.metric == f.metric if f.metric else Prediction.metric.is_(None),
                )
            )
        ).scalar()
        if existing:
            existing.probability, existing.eta, existing.summary, existing.detail = (
                f.probability,
                f.eta,
                f.summary,
                f.detail,
            )
        else:
            s.add(
                Prediction(
                    device_id=dev_id,
                    metric=f.metric,
                    kind=f.kind,
                    probability=f.probability,
                    eta=f.eta,
                    summary=f.summary,
                    detail=f.detail,
                )
            )
            await self.cache.publish({"type": "prediction", "device_id": dev_id, "summary": f.summary})
        await s.commit()
        if fp in self.detector.active:
            return
        signature = {"memory": "memory_exhaustion", "disk": "disk_exhaustion", "traffic_in": "link_saturation"}.get(
            f.metric or "", "failure_risk"
        )
        name = self.devices[dev_id]["name"]
        det = Detection(
            dev_id,
            f.metric,
            "prediction",
            "warning",
            signature,
            f"Predicted: {f.summary} on {name}",
            f.detail.get("current"),
            f.detail.get("threshold"),
            f.probability,
            {"probability": f.probability, "eta": f.eta.isoformat() if f.eta else None, **f.detail},
        )
        self.detector.active[fp] = det
        await self._raise(s, det)

    async def _correlate(self, s) -> None:
        alerts = (await s.execute(select(Alert).where(Alert.status != "resolved"))).scalars().all()
        views = [
            AlertView(
                a.id,
                a.device_id,
                (a.detail or {}).get("signature", a.kind),
                a.severity,
                a.first_seen,
                a.title,
                a.metric,
            )
            for a in alerts
            if a.kind != "prediction"
        ]
        by_id = {a.id: a for a in alerts}
        plans = correlate(views, self.topology, latest={d: v["values"] for d, v in self.latest.items()})
        for plan in plans:
            members = [by_id[i] for i in plan.alert_ids]
            incident_id = next((a.incident_id for a in members if a.incident_id), None)
            incident = await s.get(Incident, incident_id) if incident_id else None
            if incident is None or incident.status == "resolved":
                incident = Incident(
                    title=plan.title, severity=plan.severity, root_cause_device_id=plan.root_device_id, rca=plan.rca
                )
                s.add(incident)
                await s.flush()
                await audit.record(s, "system", "incident.opened", plan.title, {"incident_id": incident.id})
                await self.cache.publish(
                    {
                        "type": "incident",
                        "action": "opened",
                        "id": incident.id,
                        "title": plan.title,
                        "severity": plan.severity,
                    }
                )
                if len(members) > 1 or plan.severity == "critical":
                    await notify(
                        f"Incident: {plan.title}",
                        plan.rca.get("recommendation", ""),
                        priority="high" if plan.severity == "critical" else "default",
                    )
            else:
                incident.title, incident.severity = plan.title, plan.severity
                incident.root_cause_device_id, incident.rca = plan.root_device_id, plan.rca
            for a in members:
                a.incident_id = incident.id
        # Close incidents whose alerts have all resolved.
        open_incidents = (await s.execute(select(Incident).where(Incident.status != "resolved"))).scalars().all()
        for inc in open_incidents:
            still_open = (
                await s.execute(select(Alert.id).where(Alert.incident_id == inc.id, Alert.status != "resolved"))
            ).first()
            if not still_open:
                inc.status, inc.resolved_at = "resolved", utcnow()
                await self.cache.publish({"type": "incident", "action": "resolved", "id": inc.id})
        await s.commit()

    async def _update_status(self, s) -> None:
        alerts = (
            await s.execute(
                select(Alert.device_id, Alert.severity).where(Alert.status != "resolved", Alert.kind != "prediction")
            )
        ).all()
        sev: dict[str, list[str]] = defaultdict(list)
        for dev_id, severity in alerts:
            sev[dev_id].append(severity)
        summary = []
        for dev_id, dev in self.devices.items():
            if f"{dev_id}:availability:device_down" in self.detector.active:
                status = "down"
            elif dev_id not in self.latest:
                status = "unknown"
            elif sev.get(dev_id):
                status = "degraded"
            else:
                status = "up"
            if status != dev["status"]:
                dev["status"] = status
                db_dev = await s.get(Device, dev_id)
                if db_dev:
                    db_dev.status = status
                    if status != "down" and dev_id in self.latest:
                        db_dev.last_seen = datetime.fromisoformat(self.latest[dev_id]["time"])
            summary.append(
                {
                    "criticality": dev["criticality"],
                    "status": status,
                    "values": (self.latest.get(dev_id) or {}).get("values"),
                    "alerts": sev.get(dev_id, []),
                }
            )
        await s.commit()
        score = network_health(summary)
        now = utcnow()
        if self._last_health_persist is None or now - self._last_health_persist >= timedelta(minutes=5):
            self._last_health_persist = now
            s.add(Metric(time=now.replace(microsecond=0), device_id="network", metric="health", value=score))
            await s.commit()
        await self.cache.set_json("health:score", {"score": score, "time": now.isoformat()})
        await self.cache.publish(
            {"type": "health", "score": score, "statuses": {d: v["status"] for d, v in self.devices.items()}}
        )

    # ------------------------------------------------------------------ backfill
    async def backfill(self, simulator, days: int) -> None:
        """Generate fault-free history and learn baselines from it."""
        end = utcnow().replace(second=0, microsecond=0)
        start = end - timedelta(days=days)
        step = timedelta(minutes=5)
        log.info("Backfilling %d days of simulated history to learn baselines ...", days)
        buffer: list[dict] = []
        async with SessionLocal() as s:
            for samples in simulator.backfill(start, end, step):
                health = network_health(
                    [
                        {
                            "criticality": self.devices[sm.device_id]["criticality"],
                            "status": "up",
                            "values": sm.values,
                            "alerts": [],
                        }
                        for sm in samples
                    ]
                )
                buffer.append({"time": samples[0].time, "device_id": "network", "metric": "health", "value": health})
                for sm in samples:
                    for metric, value in sm.values.items():
                        self.baseline.update(sm.device_id, metric, sm.time, value)
                        buffer.append(
                            {"time": sm.time, "device_id": sm.device_id, "metric": metric, "value": round(value, 3)}
                        )
                if len(buffer) >= 20_000:
                    await s.execute(Metric.__table__.insert(), buffer)
                    buffer = []
            if buffer:
                await s.execute(Metric.__table__.insert(), buffer)
            await s.execute(delete(Baseline))
            await s.commit()
        await self.flush()
        log.info("Backfill complete")


def _prediction_fp(device_id: str, metric: str | None) -> str:
    # Must match Detection.key for prediction alerts (metric, else signature).
    return f"{device_id}:prediction:{metric or 'failure_risk'}"


def _alert_json(a: Alert) -> dict:
    return {
        "id": a.id,
        "device_id": a.device_id,
        "kind": a.kind,
        "metric": a.metric,
        "severity": a.severity,
        "title": a.title,
        "status": a.status,
        "first_seen": a.first_seen.isoformat() if a.first_seen else None,
        "detail": a.detail,
    }
