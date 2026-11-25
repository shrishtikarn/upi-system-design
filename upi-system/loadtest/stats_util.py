"""Helpers to read the gateway's /stats document (stdlib only, runs on the host).

/stats = {"gateway": <metrics snapshot>, "nodes": {"worker-1": <snapshot>, ...}}
A snapshot has "counters", "gauges" and "histograms" keyed like
    gw_requests_total{outcome="proxied",route="payments",status="202"}
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Any, Iterable

_KEY = re.compile(r'^(?P<name>[^{]+)(?:\{(?P<labels>.*)\})?$')
_LABEL = re.compile(r'(\w+)="([^"]*)"')


def parse_key(key: str) -> tuple[str, dict[str, str]]:
    m = _KEY.match(key)
    if not m:
        return key, {}
    return m.group("name"), dict(_LABEL.findall(m.group("labels") or ""))


def fetch_json(url: str, timeout: float = 5.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def snapshots(stats: dict[str, Any], service: str | None = None) -> list[dict[str, Any]]:
    """All metric snapshots (gateway + nodes), optionally filtered by service name."""
    items = [stats.get("gateway", {})] + list(stats.get("nodes", {}).values())
    return [s for s in items if isinstance(s, dict) and "counters" in s and (service is None or s.get("service") == service)]


def counter_sum(snaps: Iterable[dict[str, Any]], name: str, **labels: str) -> float:
    total = 0.0
    for snap in snaps:
        for key, value in snap.get("counters", {}).items():
            n, lbl = parse_key(key)
            if n == name and all(lbl.get(k) == v for k, v in labels.items()):
                total += value
    return total


def counter_breakdown(snaps: Iterable[dict[str, Any]], name: str, by: str, **labels: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for snap in snaps:
        for key, value in snap.get("counters", {}).items():
            n, lbl = parse_key(key)
            if n == name and all(lbl.get(k) == v for k, v in labels.items()):
                out[lbl.get(by, "")] = out.get(lbl.get(by, ""), 0) + value
    return out


def gauge_max(snaps: Iterable[dict[str, Any]], name: str) -> float:
    values = [v for s in snaps for k, v in s.get("gauges", {}).items() if parse_key(k)[0] == name]
    return max(values) if values else 0.0


def histogram_merge(snaps: Iterable[dict[str, Any]], name: str, **labels: str) -> dict[str, Any] | None:
    """Sum bucket counts of every histogram series matching name+labels."""
    merged: dict[str, Any] | None = None
    for snap in snaps:
        for key, hist in snap.get("histograms", {}).items():
            n, lbl = parse_key(key)
            if n != name or not all(lbl.get(k) == v for k, v in labels.items()):
                continue
            if merged is None:
                merged = {"bounds": list(hist["bounds"]), "counts": [0] * len(hist["counts"]), "count": 0, "sum": 0.0}
            merged["counts"] = [a + b for a, b in zip(merged["counts"], hist["counts"])]
            merged["count"] += hist["count"]
            merged["sum"] += hist["sum"]
    return merged


def histogram_diff(after: dict[str, Any] | None, before: dict[str, Any] | None) -> dict[str, Any] | None:
    if after is None:
        return None
    if before is None:
        return after
    return {
        "bounds": after["bounds"],
        "counts": [max(0, a - b) for a, b in zip(after["counts"], before["counts"])],
        "count": max(0, after["count"] - before["count"]),
        "sum": max(0.0, after["sum"] - before["sum"]),
    }


def quantile(hist: dict[str, Any] | None, q: float) -> float | None:
    """Same interpolation as common/metrics.py quantile_from_buckets."""
    if not hist:
        return None
    bounds, counts = hist["bounds"], hist["counts"]
    total = sum(counts)
    if total <= 0:
        return None
    rank, cumulative = q * total, 0
    for i, c in enumerate(counts):
        if c and cumulative + c >= rank:
            if i == len(bounds):
                return float(bounds[-1])
            lower = bounds[i - 1] if i > 0 else 0.0
            return round(lower + (bounds[i] - lower) * (rank - cumulative) / c, 2)
        cumulative += c
    return float(bounds[-1])


def compact_sample(stats: dict[str, Any], t: float) -> dict[str, Any]:
    """One point of the time-series recorded during a load test."""
    gw = snapshots(stats, "gateway")
    workers = snapshots(stats, "worker")
    return {
        "t": round(t, 1),
        "gw_requests": counter_sum(gw, "gw_requests_total"),
        "gw_rate_limited": counter_sum(gw, "gw_rate_limited_total"),
        "gw_upstream_errors": counter_sum(gw, "gw_requests_total", outcome="upstream_error"),
        "queue_pending": gauge_max(workers, "queue_pending"),
        "queue_lag": gauge_max(workers, "queue_lag"),
        "payments_success": counter_sum(workers, "payments_processed_total", status="SUCCESS"),
        "payments_failed": counter_sum(workers, "payments_processed_total", status="FAILED"),
        "payments_deferred": counter_sum(workers, "payments_deferred_total"),
        "bank_retries": counter_sum(workers, "bank_retries_total"),
        "circuit_state_max": gauge_max(workers, "circuit_state"),  # 0 closed, 1 half-open, 2 open
    }
