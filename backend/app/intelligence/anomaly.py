"""Anomaly detection: an ensemble of three detectors.

* **Seasonal z-score** against the learned behavioural baseline (catches
  "85% CPU at 9 AM Monday when it is usually 25%").
* **Static safety thresholds with duration** (catches "CPU > 90% for 5 min"
  even on a device that has never been seen before).
* **Isolation Forest** over each device's vector of z-scores (catches
  unusual *combinations* of metrics and supplies a confidence score).

A detector raises only after ``consecutive`` abnormal samples, and clears
after ``clear_after`` normal ones, which suppresses single-sample noise.
"""

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from app.intelligence.baseline import BaselineModel

FEATURES = ["cpu", "memory", "disk", "traffic_in", "traffic_out", "if_errors", "latency", "packet_loss", "temperature"]

SIGNATURES = {
    "cpu": "cpu_high",
    "memory": "memory_high",
    "disk": "disk_full",
    "if_errors": "interface_errors",
    "latency": "degraded_path",
    "packet_loss": "degraded_path",
    "traffic_in": "traffic_spike",
    "traffic_out": "traffic_spike",
    "temperature": "overheating",
}


@dataclass(frozen=True)
class Rule:
    metric: str
    threshold: float
    duration_s: float
    severity: str
    signature: str
    title: str


RULES = [
    Rule("cpu", 90, 300, "critical", "cpu_high", "CPU above 90%"),
    Rule("memory", 92, 120, "critical", "memory_high", "Memory above 92%"),
    Rule("disk", 90, 60, "warning", "disk_full", "Disk above 90%"),
    Rule("packet_loss", 5, 60, "critical", "degraded_path", "Packet loss above 5%"),
    Rule("temperature", 75, 120, "critical", "overheating", "Temperature above 75°C"),
    Rule("utilization_in", 0.9, 120, "warning", "link_saturation", "Inbound link utilisation above 90%"),
]


@dataclass
class Detection:
    device_id: str
    metric: str | None
    kind: str  # anomaly|threshold
    severity: str
    signature: str
    title: str
    value: float | None
    expected: float | None = None
    score: float = 0.0
    detail: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.device_id}:{self.kind}:{self.metric or self.signature}"


@dataclass
class _Track:
    bad: int = 0
    good: int = 0
    over_since: datetime | None = None


class MultivariateModel:
    """Isolation Forest trained on per-sample z-score vectors."""

    def __init__(self) -> None:
        self.model = None
        self.threshold = None

    def fit(self, vectors: list[list[float]], seed: int = 7) -> None:
        from sklearn.ensemble import IsolationForest

        if len(vectors) < 200:
            return
        x = np.asarray(vectors, dtype=float)
        self.model = IsolationForest(n_estimators=150, contamination=0.002, random_state=seed).fit(x)
        self.threshold = float(self.model.offset_)

    def score(self, vector: list[float]) -> float | None:
        """Return an anomaly confidence in [0, 1] (higher is more anomalous)."""
        if self.model is None:
            return None
        raw = float(self.model.score_samples(np.asarray([vector], dtype=float))[0])
        # score_samples is ~-0.35 for inliers and approaches -1 for outliers.
        return float(np.clip((-raw - 0.35) / 0.4, 0.0, 1.0))


class Detector:
    def __init__(
        self,
        baseline: BaselineModel,
        z_threshold: float = 3.5,
        consecutive: int = 2,
        clear_after: int = 3,
        time_scale: float = 1.0,
        learn_interval_s: float = 300.0,
    ) -> None:
        self.baseline = baseline
        self.z_threshold = z_threshold
        self.consecutive = consecutive
        self.clear_after = clear_after
        self.time_scale = time_scale
        # Online learning is rate-limited to the baseline's training resolution
        # (5-minute samples) so high-frequency live data cannot swamp a bucket
        # and slowly "learn" an emerging fault as normal.
        self.learn_interval_s = learn_interval_s
        self._last_learned: dict[tuple[str, str], datetime] = {}
        self.multivariate = MultivariateModel()
        self.tracks: dict[str, _Track] = {}
        self.active: dict[str, Detection] = {}

    def z_vector(self, device_id: str, t: datetime, values: dict[str, float]) -> list[float]:
        vec = []
        for m in FEATURES:
            z = self.baseline.zscore(device_id, m, t, values[m]) if m in values else None
            vec.append(float(np.clip(z[0], -10, 10)) if z else 0.0)
        return vec

    def _learn(self, device_id: str, metric: str, t: datetime, value: float) -> None:
        last = self._last_learned.get((device_id, metric))
        if last is None or (t - last).total_seconds() >= self.learn_interval_s:
            self._last_learned[(device_id, metric)] = t
            self.baseline.update(device_id, metric, t, value)

    def _step(self, key: str, abnormal: bool, make: callable, needed: int) -> list[tuple[str, Detection | str]]:
        tr = self.tracks.setdefault(key, _Track())
        events: list[tuple[str, Detection | str]] = []
        if abnormal:
            tr.bad += 1
            tr.good = 0
            if key not in self.active and tr.bad >= needed:
                det = make()
                self.active[key] = det
                events.append(("raise", det))
            elif key in self.active:
                self.active[key] = make()  # refresh value/score
        else:
            tr.good += 1
            tr.bad = 0
            if key in self.active and tr.good >= self.clear_after:
                self.active.pop(key)
                events.append(("clear", key))
        return events

    def observe(
        self, device_id: str, t: datetime, values: dict[str, float], capacity_mbps: float = 1000.0, learn: bool = True
    ) -> list[tuple[str, Detection | str]]:
        events: list[tuple[str, Detection | str]] = []
        derived = dict(values)
        if "traffic_in" in values and capacity_mbps:
            derived["utilization_in"] = values["traffic_in"] / capacity_mbps

        vector = self.z_vector(device_id, t, values)
        confidence = self.multivariate.score(vector)

        # 1) seasonal z-score
        for metric, value in values.items():
            zs = self.baseline.zscore(device_id, metric, t, value)
            if zs is None:
                if learn:
                    self._learn(device_id, metric, t, value)
                continue
            z, exp = zs
            abnormal = z >= self.z_threshold
            # Values that are drifting away (|z| > 2) are not learned either.
            if learn and z < 2.0:
                self._learn(device_id, metric, t, value)
            key = f"{device_id}:anomaly:{metric}"

            def make(metric=metric, value=value, z=z, exp=exp) -> Detection:
                return Detection(
                    device_id,
                    metric,
                    "anomaly",
                    "critical" if z >= 2 * self.z_threshold else "warning",
                    SIGNATURES.get(metric, "anomaly"),
                    f"Unusual {metric.replace('_', ' ')}: {value:.1f} (normally ~{exp.mean:.1f})",
                    value,
                    round(exp.mean, 2),
                    round(float(z), 2),
                    {
                        "z": round(float(z), 2),
                        "baseline_std": round(exp.std, 2),
                        "granularity": exp.granularity,
                        "ai_confidence": None if confidence is None else round(confidence, 2),
                    },
                )

            events += self._step(key, abnormal, make, self.consecutive)

        # 2) static thresholds held for a duration
        for rule in RULES:
            if rule.metric not in derived:
                continue
            key = f"{device_id}:threshold:{rule.metric}"
            tr = self.tracks.setdefault(key, _Track())
            value = derived[rule.metric]
            over = value > rule.threshold
            if over and tr.over_since is None:
                tr.over_since = t
            elif not over:
                tr.over_since = None
            held = over and (t - tr.over_since).total_seconds() >= rule.duration_s / self.time_scale

            def make(rule=rule, value=value) -> Detection:
                return Detection(
                    device_id,
                    rule.metric,
                    "threshold",
                    rule.severity,
                    rule.signature,
                    rule.title,
                    round(value, 3),
                    rule.threshold,
                    1.0,
                    {"duration_s": rule.duration_s},
                )

            events += self._step(key, held, make, 1)

        # 3) multivariate (only when no univariate detector already fired)
        if confidence is not None:
            key = f"{device_id}:anomaly:multivariate"
            others = any(k.startswith(f"{device_id}:") and k != key for k in self.active)
            top = [FEATURES[i] for i in np.argsort(np.abs(vector))[::-1][:3] if abs(vector[i]) > 1.5]

            def make(confidence=confidence, top=top) -> Detection:
                return Detection(
                    device_id,
                    None,
                    "anomaly",
                    "warning",
                    "multivariate",
                    "Unusual combination of metrics",
                    None,
                    None,
                    round(confidence, 2),
                    {"contributing_metrics": top, "ai_confidence": round(confidence, 2)},
                )

            events += self._step(key, confidence >= 0.8 and not others, make, self.consecutive + 1)
        return events
