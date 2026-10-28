"""Predictive analytics: resource exhaustion, link saturation, failure risk."""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np


@dataclass
class Forecast:
    kind: str  # exhaustion|saturation|failure
    metric: str | None
    probability: float
    eta: datetime | None
    summary: str
    detail: dict = field(default_factory=dict)


def linear_trend(times: list[datetime], values: list[float]) -> tuple[float, float, float]:
    """Least-squares fit. Returns (slope per hour, intercept at last point, r^2)."""
    slope, intercept, r2, _ = _fit(times, values)
    return slope, intercept, r2


def _fit(times: list[datetime], values: list[float]) -> tuple[float, float, float, float]:
    """Returns (slope per hour, value at last point, r^2, residual std)."""
    t0 = times[-1]
    x = np.array([(t - t0).total_seconds() / 3600 for t in times])
    y = np.asarray(values, dtype=float)
    if len(x) < 3 or np.ptp(x) == 0:
        return 0.0, float(y[-1]) if len(y) else 0.0, 0.0, 0.0
    slope, intercept = np.polyfit(x, y, 1)
    resid = y - (slope * x + intercept)
    ss_res = float(np.sum(resid**2))
    ss_tot = float(np.sum((y - y.mean()) ** 2)) or 1e-9
    return float(slope), float(intercept), max(0.0, 1 - ss_res / ss_tot), float(resid.std())


def time_to_threshold(
    times: list[datetime],
    values: list[float],
    threshold: float,
    horizon: timedelta,
    metric: str,
    kind: str = "exhaustion",
    min_points: int = 8,
    min_r2: float = 0.6,
    min_rise: float = 3.0,
) -> Forecast | None:
    """Forecast when a steadily rising metric will cross ``threshold``.

    Fits the full window and then the most recent half and quarter, so a
    trend that only started recently (flat history, then a leak) is still
    caught. The longest window that passes the quality checks wins.
    """
    for n in (len(values), len(values) // 2, len(values) // 4):
        if n < min_points:
            break
        f = _forecast(times[-n:], values[-n:], threshold, horizon, metric, kind, min_r2, min_rise)
        if f:
            return f
    return None


def _forecast(times, values, threshold, horizon, metric, kind, min_r2, min_rise) -> Forecast | None:
    slope, current, r2, noise = _fit(times, values)
    if slope <= 0 or r2 < min_r2 or current >= threshold:
        return None
    # The trend must be a real rise, not a wobble: over the observed span it
    # has to clearly exceed both the residual noise and a small absolute floor.
    span_h = (times[-1] - times[0]).total_seconds() / 3600
    if slope * span_h < max(min_rise, 4 * noise):
        return None
    # The latest readings must still sit on the trend line; a sudden drop
    # (service restarted, logs archived) means the trend is broken.
    recent = float(np.mean(values[-3:]))
    if recent < current - max(min_rise, 3 * noise):
        return None
    hours = (threshold - current) / slope
    if hours > horizon.total_seconds() / 3600:
        return None
    eta = times[-1] + timedelta(hours=hours)
    # Confidence grows with fit quality and with how imminent the crossing is.
    urgency = 1 - min(1.0, hours / (horizon.total_seconds() / 3600))
    probability = round(min(0.99, 0.5 * r2 + 0.5 * (0.4 + 0.6 * urgency)), 2)
    label = {
        "memory": "Memory exhaustion",
        "disk": "Disk full",
        "traffic_in": "Bandwidth saturation",
        "traffic_out": "Bandwidth saturation",
    }.get(metric, f"{metric} limit")
    return Forecast(
        kind,
        metric,
        probability,
        eta,
        f"{label} in ~{_human(hours)}",
        {
            "slope_per_hour": round(slope, 3),
            "current": round(current, 2),
            "threshold": threshold,
            "r2": round(r2, 3),
            "hours_remaining": round(hours, 2),
        },
    )


# Logistic failure-risk model. Weights were tuned against simulated incident
# histories (see scripts/evaluate_detector.py); features are normalised to
# roughly 0..1 so each weight reads as "how much this symptom matters".
FAILURE_WEIGHTS = {
    "cpu_spike_ratio": 2.2,
    "memory_slope": 1.6,
    "error_trend": 2.0,
    "packet_loss": 1.4,
    "temperature": 1.2,
}
FAILURE_BIAS = -4.0


def failure_risk(series: dict[str, tuple[list[datetime], list[float]]], horizon_hours: int = 24) -> Forecast | None:
    feats: dict[str, float] = {}
    if cpu := series.get("cpu"):
        feats["cpu_spike_ratio"] = float(np.mean(np.asarray(cpu[1]) > 85)) * 2
    if mem := series.get("memory"):
        slope, _, r2 = linear_trend(*mem) if len(mem[1]) >= 3 else (0.0, 0.0, 0.0)
        feats["memory_slope"] = min(2.0, max(0.0, slope / 2) * r2)
    if err := series.get("if_errors"):
        vals = np.asarray(err[1])
        feats["error_trend"] = min(2.0, float(vals[-max(1, len(vals) // 4) :].mean()) / 20)
    if loss := series.get("packet_loss"):
        feats["packet_loss"] = min(2.0, float(np.mean(loss[1][-5:])) / 3)
    if temp := series.get("temperature"):
        feats["temperature"] = min(2.0, max(0.0, (float(np.mean(temp[1][-5:])) - 60) / 15))
    if not feats:
        return None
    logit = FAILURE_BIAS + sum(FAILURE_WEIGHTS[k] * v for k, v in feats.items())
    p = 1 / (1 + math.exp(-logit))
    if p < 0.6:
        return None
    symptoms = [k for k, v in feats.items() if FAILURE_WEIGHTS[k] * v >= 1.0]
    last = max(s[0][-1] for s in series.values() if s[0])
    return Forecast(
        "failure",
        None,
        round(p, 2),
        last + timedelta(hours=horizon_hours),
        f"{round(p * 100)}% chance of failure within {horizon_hours}h",
        {
            "features": {k: round(v, 3) for k, v in feats.items()},
            "symptoms": symptoms,
            "recommendation": "Schedule a maintenance window",
        },
    )


def _human(hours: float) -> str:
    if hours < 1:
        return f"{max(1, round(hours * 60))} min"
    if hours < 48:
        return f"{hours:.0f} h"
    return f"{hours / 24:.0f} days"
