"""Behavioural baseline learning.

Keeps running statistics (Welford's algorithm) per device + metric at three
granularities and uses the most specific one with enough samples:

1. hour-of-week (``0..167``) - captures "Monday 9 AM" vs "Saturday 9 AM"
2. hour-of-day (``168..191``) - fallback while a week of data accumulates
3. global (``-1``)
"""

import math
from dataclasses import dataclass
from datetime import datetime

GLOBAL = -1
HOUR_OF_DAY_OFFSET = 168

# Noise floors stop near-constant metrics (e.g. 0 errors) from producing
# huge z-scores on tiny absolute changes.
MIN_STD: dict[str, float] = {
    "cpu": 2.0,
    "memory": 1.0,
    "disk": 0.5,
    "if_errors": 1.0,
    "latency": 1.0,
    "packet_loss": 0.3,
    "temperature": 1.0,
}


def hour_of_week(t: datetime) -> int:
    return t.weekday() * 24 + t.hour


@dataclass(slots=True)
class Stats:
    count: int = 0
    mean: float = 0.0
    m2: float = 0.0

    def update(self, x: float) -> None:
        self.count += 1
        delta = x - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (x - self.mean)

    @property
    def std(self) -> float:
        return math.sqrt(self.m2 / (self.count - 1)) if self.count > 1 else 0.0


@dataclass(slots=True)
class Expectation:
    mean: float
    std: float
    samples: int
    granularity: str


class BaselineModel:
    def __init__(self, min_samples: int = 12) -> None:
        self.min_samples = min_samples
        self.stats: dict[tuple[str, str, int], Stats] = {}
        self.dirty: set[tuple[str, str, int]] = set()

    def _buckets(self, t: datetime) -> tuple[int, int, int]:
        return hour_of_week(t), HOUR_OF_DAY_OFFSET + t.hour, GLOBAL

    def update(self, device_id: str, metric: str, t: datetime, value: float) -> None:
        for b in self._buckets(t):
            key = (device_id, metric, b)
            self.stats.setdefault(key, Stats()).update(value)
            self.dirty.add(key)

    def expect(self, device_id: str, metric: str, t: datetime) -> Expectation | None:
        for b, label in zip(self._buckets(t), ("hour_of_week", "hour_of_day", "global"), strict=True):
            s = self.stats.get((device_id, metric, b))
            if s and s.count >= self.min_samples:
                floor = MIN_STD.get(metric, 0.0)
                if metric.startswith("traffic"):
                    floor = max(1.0, abs(s.mean) * 0.05)
                return Expectation(s.mean, max(s.std, floor), s.count, label)
        return None

    def zscore(self, device_id: str, metric: str, t: datetime, value: float) -> tuple[float, Expectation] | None:
        exp = self.expect(device_id, metric, t)
        if exp is None:
            return None
        return (value - exp.mean) / exp.std, exp

    def is_learning(self, device_id: str, metric: str) -> bool:
        s = self.stats.get((device_id, metric, GLOBAL))
        return s is None or s.count < self.min_samples

    # Persistence helpers -------------------------------------------------
    def load_row(self, device_id: str, metric: str, bucket: int, count: int, mean: float, m2: float) -> None:
        self.stats[(device_id, metric, bucket)] = Stats(count, mean, m2)

    def drain_dirty(self) -> list[tuple[str, str, int, Stats]]:
        rows = [(*k, self.stats[k]) for k in self.dirty]
        self.dirty.clear()
        return rows
