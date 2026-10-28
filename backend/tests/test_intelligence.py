from datetime import UTC, datetime, timedelta

import numpy as np

from app.intelligence.anomaly import Detector
from app.intelligence.baseline import BaselineModel, Stats, hour_of_week
from app.intelligence.health import network_health
from app.intelligence.prediction import failure_risk, time_to_threshold
from app.intelligence.rca import AlertView, Topology, correlate

MONDAY_9AM = datetime(2026, 1, 5, 9, tzinfo=UTC)


def test_welford_matches_numpy():
    data = np.random.default_rng(1).normal(40, 7, 500)
    s = Stats()
    for x in data:
        s.update(float(x))
    assert abs(s.mean - data.mean()) < 1e-9
    assert abs(s.std - data.std(ddof=1)) < 1e-9


def test_baseline_prefers_hour_of_week_then_falls_back():
    b = BaselineModel(min_samples=5)
    for week in range(5):
        b.update("r1", "cpu", MONDAY_9AM + timedelta(weeks=week), 25.0 + week % 2)
    exp = b.expect("r1", "cpu", MONDAY_9AM)
    assert exp.granularity == "hour_of_week" and 25 <= exp.mean <= 26
    # Tuesday 9 AM has no hour-of-week data yet -> hour-of-day bucket
    assert b.expect("r1", "cpu", MONDAY_9AM + timedelta(days=1)).granularity == "hour_of_day"
    assert hour_of_week(MONDAY_9AM) == 9


def _trained_detector() -> Detector:
    b = BaselineModel(min_samples=12)
    rng = np.random.default_rng(0)
    t = MONDAY_9AM - timedelta(days=7)
    while t < MONDAY_9AM:
        b.update("r1", "cpu", t, float(rng.normal(25, 3)))
        t += timedelta(minutes=5)
    return Detector(b, z_threshold=3.5, consecutive=2)


def test_detector_needs_consecutive_samples_and_clears():
    d = _trained_detector()
    t = MONDAY_9AM
    assert d.observe("r1", t, {"cpu": 85}) == []  # first abnormal sample only arms it
    events = d.observe("r1", t + timedelta(minutes=1), {"cpu": 86})
    assert [k for k, _ in events] == ["raise"]
    det = events[0][1]
    assert det.signature == "cpu_high" and det.kind == "anomaly" and det.detail["z"] > 3.5
    cleared = []
    for i in range(2, 6):
        cleared += d.observe("r1", t + timedelta(minutes=i), {"cpu": 25})
    assert ("clear", det.key) in cleared


def test_detector_ignores_normal_noise():
    d = _trained_detector()
    rng = np.random.default_rng(5)
    events = []
    for i in range(120):
        events += d.observe("r1", MONDAY_9AM + timedelta(minutes=i), {"cpu": float(rng.normal(25, 3))})
    assert not [e for e in events if e[0] == "raise"]


def test_threshold_rule_requires_duration():
    d = Detector(BaselineModel(), time_scale=1.0)  # no baseline: only static rules apply
    t = MONDAY_9AM
    for i in range(5):  # 4 minutes over 90%
        assert not [e for e in d.observe("r1", t + timedelta(minutes=i), {"cpu": 95}) if e[0] == "raise"]
    events = d.observe("r1", t + timedelta(minutes=5), {"cpu": 95})
    assert any(k == "raise" and det.kind == "threshold" for k, det in events)


def test_forecast_linear_leak():
    t0 = MONDAY_9AM
    times = [t0 + timedelta(minutes=10 * i) for i in range(30)]
    values = [60 + 1.0 * i + (0.2 if i % 2 else -0.2) for i in range(30)]  # +6%/h
    f = time_to_threshold(times, values, 98.0, timedelta(hours=24), "memory")
    assert f is not None and f.kind == "exhaustion"
    assert 1.0 < f.detail["hours_remaining"] < 2.0  # (98 - 89) / 6 = 1.5h
    assert f.summary.startswith("Memory exhaustion")


def test_forecast_rejects_flat_noise_and_catches_recent_trend():
    rng = np.random.default_rng(2)
    times = [MONDAY_9AM + timedelta(minutes=i) for i in range(40)]
    flat = list(rng.normal(55, 0.8, 40))
    assert time_to_threshold(times, flat, 98.0, timedelta(hours=24), "memory") is None
    hockey = flat[:30] + [55 + 2.0 * i for i in range(1, 11)]  # leak only in the last 10 points
    assert time_to_threshold(times, hockey, 98.0, timedelta(hours=24), "memory") is not None


def test_failure_risk():
    times = [MONDAY_9AM + timedelta(minutes=i) for i in range(20)]
    sick = {"if_errors": (times, [60.0] * 20), "packet_loss": (times, [5.0] * 20), "cpu": (times, [92.0] * 20)}
    healthy = {"if_errors": (times, [0.2] * 20), "packet_loss": (times, [0.02] * 20), "cpu": (times, [20.0] * 20)}
    assert failure_risk(sick).probability > 0.8
    assert failure_risk(healthy) is None


def _alerts(*rows):
    return [
        AlertView(f"a{i}", dev, sig, sev, MONDAY_9AM + timedelta(seconds=i * 10), title=f"{dev} {sig}")
        for i, (dev, sig, sev) in enumerate(rows)
    ]


def test_rca_finds_upstream_root_and_builds_chain():
    topo = Topology([("core", "dist"), ("dist", "acc"), ("acc", "ap"), ("core", "srv")])
    alerts = _alerts(
        ("ap", "degraded_path", "warning"),
        ("acc", "degraded_path", "warning"),
        ("dist", "interface_errors", "critical"),
        ("dist", "degraded_path", "warning"),
    )
    [plan] = correlate(alerts, topo)
    assert plan.root_device_id == "dist"
    assert plan.signature == "interface_errors"
    assert "backup port" in plan.rca["recommendation"]
    chain = [s["device_id"] for s in plan.rca["investigation_chain"]]
    assert chain == ["ap", "acc", "dist"]
    assert plan.rca["impact"]["downstream_devices"] == 2 and len(plan.alert_ids) == 4


def test_rca_keeps_unrelated_and_security_alerts_apart():
    topo = Topology([("core", "a"), ("core", "b"), ("fw", "core")])
    alerts = _alerts(
        ("a", "cpu_high", "critical"),
        ("b", "disk_full", "warning"),
        ("fw", "brute_force", "critical"),
        ("core", "degraded_path", "warning"),
    )
    plans = correlate(alerts, topo)
    sigs = sorted(p.signature for p in plans)
    assert "brute_force" in sigs and len([p for p in plans if p.signature == "brute_force"][0].alert_ids) == 1


def test_network_health_weights_criticality():
    healthy = {"criticality": "critical", "status": "up", "values": {"cpu": 20}, "alerts": []}
    down_low = {"criticality": "low", "status": "down", "values": None, "alerts": []}
    down_critical = {**down_low, "criticality": "critical"}
    assert network_health([healthy]) == 100
    assert network_health([healthy, down_low]) > network_health([healthy, down_critical])
