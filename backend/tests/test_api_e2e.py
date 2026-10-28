"""End-to-end: boot the real app (simulator + pipeline + engine) and heal faults via the API."""

import asyncio
import time

import httpx
import pytest

from app.main import app
from app.services.runtime import get_runtime


@pytest.fixture(scope="module")
async def client():
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            await asyncio.wait_for(get_runtime().ready.wait(), timeout=120)
            r = await c.post("/api/auth/login", data={"username": "admin", "password": "nimbusguard"})
            c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
            yield c


async def wait_for(fn, limit=60.0, interval=0.5):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if result := await fn():
            return result
        await asyncio.sleep(interval)
    raise AssertionError("condition not met in time")


async def test_auth_and_rbac(client):
    anon = httpx.AsyncClient(transport=client._transport, base_url="http://test")
    assert (await anon.get("/api/devices")).status_code == 401
    assert (await anon.post("/api/auth/login", data={"username": "admin", "password": "nope"})).status_code == 401
    r = await client.post("/api/users", json={"username": "viewer1", "password": "viewerpass1", "role": "viewer"})
    assert r.status_code == 201
    tok = (await anon.post("/api/auth/login", data={"username": "viewer1", "password": "viewerpass1"})).json()
    viewer = {"Authorization": f"Bearer {tok['access_token']}"}
    assert (await anon.get("/api/devices", headers=viewer)).status_code == 200
    assert (await anon.post("/api/simulator/inject", json={"scenario": "cpu_spike"}, headers=viewer)).status_code == 403
    assert (await anon.get("/api/audit", headers=viewer)).status_code == 403


async def test_inventory_and_metrics(client):
    devices = (await client.get("/api/devices")).json()
    assert len(devices) == 8
    topo = (await client.get("/api/topology")).json()
    assert len(topo["nodes"]) == 8 and topo["edges"]
    core = next(d for d in devices if d["name"] == "rtr-core-01")
    series = (await client.get(f"/api/devices/{core['id']}/metrics?metrics=cpu&hours=24")).json()["series"]["cpu"]
    assert len(series) > 50  # backfilled history
    detail = (await client.get(f"/api/devices/{core['id']}")).json()
    assert detail["downstream"] and detail["upstream"]
    assert "credentials" not in str(detail).replace("has_credentials", "")


async def _device(client, name):
    return next(d for d in (await client.get("/api/devices")).json() if d["name"] == name)


async def test_cpu_runaway_is_detected_and_auto_healed(client):
    dev = await _device(client, "sw-acc-01")
    r = await client.post("/api/simulator/inject", json={"scenario": "cpu_spike", "device_id": dev["id"]})
    assert r.json()["ok"]

    async def finished_run():
        runs = (await client.get("/api/remediation/runs")).json()
        done = [
            x
            for x in runs
            if x["device_id"] == dev["id"]
            and x["playbook"] == "high-cpu"
            and x["status"] not in ("approved", "running")
        ]
        return done[0] if done else None

    run = await wait_for(finished_run, limit=90)
    assert run["status"] == "succeeded", run
    assert run["approved_by"] == "auto"
    assert [s["action"] for s in run["steps"]][-1] == "verify"
    assert (await client.get("/api/simulator")).json()["faults"] == []

    async def resolved():
        alerts = (await client.get(f"/api/alerts?device_id={dev['id']}")).json()
        return alerts and all(a["status"] == "resolved" for a in alerts if a["kind"] == "threshold")

    await wait_for(resolved, limit=30)


async def test_exfiltration_requires_human_approval(client):
    dev = await _device(client, "sw-acc-01")
    await client.post("/api/simulator/inject", json={"scenario": "exfiltration", "device_id": dev["id"]})

    async def pending():
        runs = (await client.get("/api/remediation/runs?status=pending_approval")).json()
        return next((x for x in runs if x["playbook"] == "exfiltration"), None)

    run = await wait_for(pending, limit=60)
    assert any(g["name"] == "playbook_policy" and g["effect"] == "approval" for g in run["gates"])
    assert run["context"]["source_ip"] == "10.1.1.99"
    r = await client.post(f"/api/remediation/runs/{run['id']}/approve")
    assert r.status_code == 200

    async def done():
        r = (await client.get(f"/api/remediation/runs/{run['id']}")).json()
        return r if r["status"] in ("succeeded", "failed", "rolled_back") else None

    assert (await wait_for(done))["status"] == "succeeded"
    assert (await client.post(f"/api/remediation/runs/{run['id']}/approve")).status_code == 409


async def test_stubborn_fault_rolls_back(client):
    dev = await _device(client, "sw-dist-02")
    await client.post("/api/simulator/inject", json={"scenario": "cpu_spike", "device_id": dev["id"], "stubborn": True})

    async def finished():
        runs = (await client.get("/api/remediation/runs")).json()
        return next(
            (x for x in runs if x["device_id"] == dev["id"] and x["status"] in ("rolled_back", "failed", "succeeded")),
            None,
        )

    run = await wait_for(finished, limit=90)
    assert run["status"] == "rolled_back"
    assert any(s["phase"] == "rollback" for s in run["steps"])
    await client.post("/api/simulator/clear", json={})


async def test_dashboards_incidents_and_audit(client):
    ex = (await client.get("/api/dashboard/executive")).json()
    assert ex["automated_fixes"] >= 2 and ex["health_trend"] and ex["money_saved"] > 0
    ops = (await client.get("/api/dashboard/operations")).json()
    assert ops["devices_total"] == 8 and "predicted_issues" in ops
    incidents = (await client.get("/api/incidents")).json()
    assert incidents
    detail = (await client.get(f"/api/incidents/{incidents[-1]['id']}")).json()
    assert detail["alerts"] and detail["rca"]["recommendation"]
    actions = {e["action"] for e in (await client.get("/api/audit?limit=500")).json()}
    assert {
        "auth.login",
        "simulator.inject",
        "remediation.approved",
        "remediation.succeeded",
        "remediation.approve",
    } <= actions
    assert (await client.get("/api/audit/verify")).json()["valid"]


async def test_restart_rehydrates_open_alerts():
    """Alerts left open by a previous worker must clear once the condition is gone."""
    from app.db import SessionLocal
    from app.models import Alert, Device
    from app.services.pipeline import Pipeline

    async with SessionLocal() as s:
        dev = (await s.execute(Device.__table__.select().limit(1))).first()
        s.add(
            Alert(
                fingerprint=f"{dev.id}:anomaly:cpu",
                device_id=dev.id,
                kind="anomaly",
                metric="cpu",
                severity="warning",
                title="stale",
                detail={"signature": "cpu_high"},
            )
        )
        await s.commit()
    rt = get_runtime()
    fresh = Pipeline(rt.settings, rt.cache)
    await fresh.load()
    assert f"{dev.id}:anomaly:cpu" in fresh.detector.active
    rt.pipeline.detector.active.update(fresh.detector.active)  # simulate the restarted process

    async def cleared():
        async with SessionLocal() as s:
            a = (await s.execute(Alert.__table__.select().where(Alert.title == "stale"))).first()
            return a.status == "resolved"

    await wait_for(cleared, limit=30)


async def test_register_real_device_encrypts_credentials(client):
    core = await _device(client, "rtr-core-01")
    r = await client.post(
        "/api/devices",
        json={
            "name": "edge-sw-9",
            "type": "switch",
            "ip": "192.0.2.9",
            "collector": "snmp",
            "snmp_community": "s3cret",
            "upstream_id": core["id"],
        },
    )
    assert r.status_code == 201 and r.json()["has_credentials"]
    assert "s3cret" not in r.text
    assert (await client.post("/api/devices", json={"name": "edge-sw-9", "type": "switch"})).status_code == 409
    assert (await client.delete(f"/api/devices/{r.json()['id']}")).status_code == 204
