"""In-process metrics: counters, gauges and fixed-bucket latency histograms.

Why fixed buckets (like Prometheus) instead of keeping raw samples?
  * Memory is constant no matter how much traffic flows through.
  * Histograms are *cumulative*, so the load-test tooling can take a snapshot
    before and after a run, subtract the bucket counts, and compute accurate
    p50/p95/p99 for exactly that window (see loadtest/make_report.py).

Exposed by every service at GET /metrics (JSON) and GET /metrics/prometheus
(Prometheus text exposition format, so you can point Prometheus at it later).
"""
from __future__ import annotations

import bisect
import time
from typing import Any, Iterable

DEFAULT_BUCKETS_MS: tuple[float, ...] = (
    0.5, 1, 2, 3, 5, 7.5, 10, 15, 20, 30, 50, 75, 100, 150, 200,
    300, 500, 750, 1000, 1500, 2000, 3000, 5000, 10000,
)

LabelKey = tuple[str, tuple[tuple[str, str], ...]]


def _label_tuple(labels: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((k, str(v)) for k, v in labels.items()))


def format_key(name: str, labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return name
    inner = ",".join(f'{k}="{v}"' for k, v in labels)
    return f"{name}{{{inner}}}"


def quantile_from_buckets(q: float, bounds: Iterable[float], counts: list[int]) -> float | None:
    """Estimate a quantile from bucket counts with linear interpolation
    inside the bucket (same approach as Prometheus' histogram_quantile)."""
    bounds = list(bounds)
    total = sum(counts)
    if total <= 0:
        return None
    rank = q * total
    cumulative = 0
    for i, c in enumerate(counts):
        if c and cumulative + c >= rank:
            if i == len(bounds):  # +Inf bucket: best we can say is "above the last bound"
                return float(bounds[-1])
            lower = bounds[i - 1] if i > 0 else 0.0
            upper = bounds[i]
            fraction = (rank - cumulative) / c
            return round(lower + (upper - lower) * fraction, 3)
        cumulative += c
    return float(bounds[-1])


class Histogram:
    __slots__ = ("bounds", "counts", "sum", "count")

    def __init__(self, bounds: Iterable[float] = DEFAULT_BUCKETS_MS):
        self.bounds = tuple(bounds)
        self.counts = [0] * (len(self.bounds) + 1)  # last slot is +Inf
        self.sum = 0.0
        self.count = 0

    def observe(self, value: float) -> None:
        self.counts[bisect.bisect_left(self.bounds, value)] += 1
        self.sum += value
        self.count += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "sum": round(self.sum, 3),
            "avg": round(self.sum / self.count, 3) if self.count else None,
            "p50": quantile_from_buckets(0.50, self.bounds, self.counts),
            "p90": quantile_from_buckets(0.90, self.bounds, self.counts),
            "p95": quantile_from_buckets(0.95, self.bounds, self.counts),
            "p99": quantile_from_buckets(0.99, self.bounds, self.counts),
            "bounds": list(self.bounds),
            "counts": list(self.counts),
        }


class Metrics:
    def __init__(self, service: str, node: str, buckets: Iterable[float] = DEFAULT_BUCKETS_MS):
        self.service = service
        self.node = node
        self.started_at = time.time()
        self._buckets = tuple(buckets)
        self._counters: dict[LabelKey, float] = {}
        self._gauges: dict[LabelKey, float] = {}
        self._histograms: dict[LabelKey, Histogram] = {}

    # -- recording -----------------------------------------------------------
    def inc(self, name: str, value: float = 1, **labels: Any) -> None:
        key = (name, _label_tuple(labels))
        self._counters[key] = self._counters.get(key, 0) + value

    def set_gauge(self, name: str, value: float, **labels: Any) -> None:
        self._gauges[(name, _label_tuple(labels))] = value

    def observe(self, name: str, value_ms: float, **labels: Any) -> None:
        key = (name, _label_tuple(labels))
        hist = self._histograms.get(key)
        if hist is None:
            hist = self._histograms[key] = Histogram(self._buckets)
        hist.observe(value_ms)

    # -- reading -------------------------------------------------------------
    def counter_value(self, name: str, **labels: Any) -> float:
        return self._counters.get((name, _label_tuple(labels)), 0)

    def snapshot(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "node": self.node,
            "uptime_s": round(time.time() - self.started_at, 1),
            "counters": {format_key(n, lbl): v for (n, lbl), v in sorted(self._counters.items())},
            "gauges": {format_key(n, lbl): v for (n, lbl), v in sorted(self._gauges.items())},
            "histograms": {format_key(n, lbl): h.to_dict() for (n, lbl), h in sorted(self._histograms.items())},
        }

    def prometheus(self) -> str:
        lines: list[str] = []
        base = (("node", self.node), ("service", self.service))
        for (name, labels), value in sorted(self._counters.items()):
            lines.append(f"{format_key(name, labels + base)} {value}")
        for (name, labels), value in sorted(self._gauges.items()):
            lines.append(f"{format_key(name, labels + base)} {value}")
        for (name, labels), hist in sorted(self._histograms.items()):
            cumulative = 0
            for bound, count in zip(list(hist.bounds) + ["+Inf"], hist.counts):
                cumulative += count
                lines.append(f"{format_key(name + '_bucket', labels + base + (('le', str(bound)),))} {cumulative}")
            lines.append(f"{format_key(name + '_sum', labels + base)} {round(hist.sum, 3)}")
            lines.append(f"{format_key(name + '_count', labels + base)} {hist.count}")
        return "\n".join(lines) + "\n"
