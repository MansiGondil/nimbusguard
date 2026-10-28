"""Measure anomaly-detection accuracy against labelled, simulated faults.

The simulator learns a week of normal behaviour, then replays a timeline of
randomly injected faults (each labelled with the devices it affects). The
streaming detector runs over the whole timeline and is scored on:

* recall    - share of fault episodes detected on an affected device
* precision - share of raised detections that fall inside a fault episode
* time to detect and false alarms per device-day

Scoring covers the streaming anomaly detector and, as in the live pipeline,
the trend forecaster (which is what catches slow leaks before thresholds).

Run:  python -m scripts.evaluate_detector [--episodes 40] [--iforest] [--no-forecast]
"""

import argparse
import random
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.collectors.simulator import NetworkSimulator, SimDevice
from app.intelligence import prediction
from app.intelligence.anomaly import Detector
from app.intelligence.baseline import BaselineModel
from app.services.seed import DEMO_NETWORK

# Metric-visible scenarios (security scenarios are log/flow rules, scored separately in tests).
SCENARIOS = ["cpu_spike", "memory_leak", "bad_cable", "link_saturation", "disk_full", "exfiltration"]
TARGETS = {
    "cpu_spike": ["srv-web-01", "srv-file-01", "fw-edge-01", "rtr-core-01"],
    "memory_leak": ["srv-mail-01", "srv-db-01", "cloud-app-01"],
    "bad_cable": ["sw-dist-01", "sw-dist-02", "sw-acc-03"],
    "link_saturation": ["rtr-wan-01"],
    "disk_full": ["srv-file-01", "srv-ehr-01"],
    "exfiltration": ["sw-acc-01", "sw-acc-02"],
}


@dataclass
class Episode:
    scenario: str
    device: str
    start: datetime
    end: datetime
    affected: set[str]
    detected_at: datetime | None = None


def build() -> NetworkSimulator:
    devices = [SimDevice(name, name, typ, ip, cap) for name, typ, ip, _c, cap, *_ in DEMO_NETWORK]
    links = [(up, name) for name, *_r, up, _b in DEMO_NETWORK if up]
    return NetworkSimulator(devices, links, seed=42)


def evaluate(
    episodes: int = 40,
    fault_minutes: int = 15,
    gap_minutes: int = 30,
    step_s: int = 60,
    use_iforest: bool = False,
    use_forecast: bool = True,
    seed: int = 7,
    verbose: bool = True,
) -> dict:
    sim = build()
    rng = random.Random(seed)
    capacity = {d.id: d.capacity_mbps for d in sim.devices.values()}

    # 1) learn a week of normal behaviour
    start = datetime(2026, 1, 5, tzinfo=UTC)  # a Monday
    baseline = BaselineModel(min_samples=12)
    history = []
    for samples in sim.backfill(start - timedelta(days=7), start, timedelta(minutes=5)):
        for s in samples:
            for m, v in s.values.items():
                baseline.update(s.device_id, m, s.time, v)
        history.append(samples)
    detector = Detector(baseline, z_threshold=3.5, consecutive=2, time_scale=1.0)
    if use_iforest:
        vectors = [detector.z_vector(s.device_id, s.time, s.values) for batch in history[-576:] for s in batch]
        detector.multivariate.fit(vectors)

    # 2) labelled fault timeline
    timeline: list[Episode] = []
    t = start + timedelta(minutes=gap_minutes)
    for _ in range(episodes):
        scenario = rng.choice(SCENARIOS)
        device = rng.choice(TARGETS[scenario])
        affected = {device}
        if scenario in ("bad_cable", "link_saturation"):
            affected |= set(sim.descendants(device))
        timeline.append(Episode(scenario, device, t, t + timedelta(minutes=fault_minutes), affected))
        t += timedelta(minutes=fault_minutes + gap_minutes)
    end = t

    # 3) stream the timeline through the detector
    grace = timedelta(minutes=5)  # detections just after a fault clears still count
    raised: list[tuple[datetime, str, str]] = []
    recent: dict[tuple[str, str], list[tuple[datetime, float]]] = defaultdict(list)
    forecasting: set[tuple[str, str]] = set()
    idx, active = 0, None
    now = start
    t0 = time.perf_counter()
    while now < end:
        if active and now >= active.end:
            sim.clear()
            active = None
        if active is None and idx < len(timeline) and now >= timeline[idx].start:
            active = timeline[idx]
            sim.inject(active.scenario, active.device, now=now)
            idx += 1

        def record(device_id: str, key: str, now: datetime = now) -> None:
            raised.append((now, device_id, key))
            for ep in timeline:
                if ep.start <= now <= ep.end + grace and device_id in ep.affected and ep.detected_at is None:
                    ep.detected_at = now

        run_forecast = use_forecast and now.minute % 5 == 0 and now.second < step_s
        for device_id in sim.devices:
            values = sim.sample(device_id, now)
            for kind, item in detector.observe(device_id, now, values, capacity[device_id]):
                if kind == "raise":
                    record(device_id, item.key)
            for metric, limit in (("memory", 98.0), ("disk", 95.0)):
                if metric not in values:
                    continue
                buf = recent[(device_id, metric)]
                buf.append((now, values[metric]))
                del buf[: max(0, len(buf) - 30)]  # 30-minute lookback
                if run_forecast:
                    f = prediction.time_to_threshold(
                        [p[0] for p in buf], [p[1] for p in buf], limit, timedelta(hours=24), metric
                    )
                    key = (device_id, metric)
                    if f and key not in forecasting:
                        forecasting.add(key)
                        record(device_id, f"{device_id}:prediction:{metric}")
                    elif not f:
                        forecasting.discard(key)
        now += timedelta(seconds=step_s)
    elapsed = time.perf_counter() - t0

    # 4) score
    def in_episode(ts: datetime, dev: str) -> bool:
        return any(ep.start <= ts <= ep.end + grace and dev in ep.affected for ep in timeline)

    tp = sum(1 for ts, dev, _ in raised if in_episode(ts, dev))
    fp = len(raised) - tp
    detected = [ep for ep in timeline if ep.detected_at]
    normal_device_days = (
        len(sim.devices)
        * ((end - start) - sum((ep.end + grace - ep.start for ep in timeline), timedelta())).total_seconds()
        / 86400
    )
    per_scenario = defaultdict(lambda: [0, 0])
    for ep in timeline:
        per_scenario[ep.scenario][1] += 1
        per_scenario[ep.scenario][0] += bool(ep.detected_at)
    result = {
        "episodes": len(timeline),
        "recall": len(detected) / len(timeline),
        "precision": tp / len(raised) if raised else 0.0,
        "detections": len(raised),
        "false_alarms_per_device_day": fp / normal_device_days if normal_device_days else 0.0,
        "mean_time_to_detect_min": statistics.mean((ep.detected_at - ep.start).total_seconds() / 60 for ep in detected)
        if detected
        else None,
        "per_scenario": {k: f"{d}/{n}" for k, (d, n) in sorted(per_scenario.items())},
        "samples_per_second": len(sim.devices) * ((end - start).total_seconds() / step_s) / elapsed,
    }
    if verbose:
        print(f"Episodes:            {result['episodes']}")
        print(f"Recall (detected):   {result['recall']:.1%}")
        print(f"Precision:           {result['precision']:.1%}  ({result['detections']} detections)")
        f1 = 2 * result["recall"] * result["precision"] / ((result["recall"] + result["precision"]) or 1)
        print(f"F1 score:            {f1:.1%}")
        print(f"False alarms:        {result['false_alarms_per_device_day']:.2f} per device-day")
        print(f"Mean time to detect: {result['mean_time_to_detect_min']:.1f} min")
        print(f"Per scenario:        {result['per_scenario']}")
        print(f"Throughput:          {result['samples_per_second']:.0f} device-samples/s")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episodes", type=int, default=40)
    ap.add_argument("--iforest", action="store_true", help="include the Isolation Forest detector")
    ap.add_argument("--no-forecast", action="store_true", help="score the anomaly detector alone")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    evaluate(args.episodes, use_iforest=args.iforest, use_forecast=not args.no_forecast, seed=args.seed)
