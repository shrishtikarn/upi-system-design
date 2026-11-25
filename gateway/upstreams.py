"""Upstream node registry, round-robin load balancing and active health checks.

Each node carries:
  * status   - last result of polling GET <node>/health  (UP / DEGRADED / DOWN / UNKNOWN)
  * breaker  - a CircuitBreaker fed by *real* proxied traffic

A node receives traffic only if its health status is not DOWN *and* its
breaker allows a request. Health checks catch a node that is dead; the breaker
catches a node that is "up" but failing/timing out on real requests, and
reacts within a handful of requests instead of waiting for the next poll.
"""
from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Any

from common.logs import EventLogger
from common.metrics import Metrics
from common.resilience import CircuitBreaker, CircuitState

_STATE_GAUGE = {CircuitState.CLOSED: 0, CircuitState.HALF_OPEN: 1, CircuitState.OPEN: 2}


@dataclass
class UpstreamNode:
    service: str
    name: str
    url: str
    breaker: CircuitBreaker
    routable: bool = True
    status: str = "UNKNOWN"
    last_checked: float | None = None
    check_latency_ms: float | None = None
    last_error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def available(self) -> bool:
        return self.status != "DOWN" and self.breaker.allow_request()

    def describe(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "node": self.name,
            "url": self.url,
            "role": "routed" if self.routable else "monitored",
            "status": self.status,
            "receiving_traffic": self.routable and self.available(),
            "last_checked_s_ago": None if self.last_checked is None else round(time.time() - self.last_checked, 1),
            "health_check_latency_ms": self.check_latency_ms,
            "last_error": self.last_error,
            "circuit": self.breaker.describe(),
            "checks": self.details.get("checks"),
            **{k: v for k, v in self.details.items() if k not in ("checks", "service", "node", "status")},
        }


class UpstreamPool:
    """Round-robin over the nodes of one service, skipping unavailable ones."""

    def __init__(self, service: str, nodes: list[UpstreamNode]):
        self.service = service
        self.nodes = nodes
        self._counter = itertools.count()

    def candidates(self) -> list[UpstreamNode]:
        if not self.nodes:
            return []
        start = next(self._counter) % len(self.nodes)
        rotated = self.nodes[start:] + self.nodes[:start]
        return [node for node in rotated if node.available()]

    def healthy_count(self) -> int:
        return sum(1 for n in self.nodes if n.status in ("UP", "DEGRADED"))


class HealthMonitor:
    def __init__(self, http: Any, nodes: list[UpstreamNode], interval_s: float, timeout_s: float,
                 metrics: Metrics, log: EventLogger):
        self.http = http
        self.nodes = nodes
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self.metrics = metrics
        self.log = log

    async def check(self, node: UpstreamNode) -> None:
        start = time.perf_counter()
        previous = node.status
        try:
            resp = await self.http.get(f"{node.url}/health", timeout=self.timeout_s)
            body = resp.json()
            node.details = body if isinstance(body, dict) else {}
            node.status = str(node.details.get("status", "UP" if resp.status_code == 200 else "DOWN"))
            node.last_error = None if resp.status_code == 200 else f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001 - unreachable, timeout, bad JSON...
            node.status = "DOWN"
            node.details = {}
            node.last_error = f"{type(exc).__name__}: {exc}"[:200]
        node.check_latency_ms = round((time.perf_counter() - start) * 1000, 2)
        node.last_checked = time.time()
        self.metrics.set_gauge("upstream_up", 1 if node.status in ("UP", "DEGRADED") else 0, node=node.name)
        self.metrics.set_gauge("upstream_circuit_state", _STATE_GAUGE[node.breaker.state], node=node.name)
        if previous != node.status:
            level = self.log.warning if node.status == "DOWN" else self.log.info
            level("node.status_changed", node=node.name, service=node.service, previous=previous,
                  current=node.status, error=node.last_error)

    async def check_all(self) -> None:
        await asyncio.gather(*(self.check(node) for node in self.nodes))

    async def run_forever(self) -> None:
        while True:
            try:
                await self.check_all()
            except Exception as exc:  # noqa: BLE001 - never let the monitor die
                self.log.error("health_monitor.error", error=repr(exc))
            await asyncio.sleep(self.interval_s)
