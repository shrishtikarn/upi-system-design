"""Health-check registry used by every service's GET /health endpoint.

Each service registers small async probes (Postgres `SELECT 1`, Redis PING,
worker-loop liveness, ...). The endpoint runs them concurrently with a timeout
and reports:

  UP        all probes passed                          -> HTTP 200
  DEGRADED  only non-critical probes failed            -> HTTP 200
  DOWN      at least one critical probe failed         -> HTTP 503

The gateway polls these endpoints to decide which nodes receive traffic.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

Probe = Callable[[], Awaitable[dict[str, Any] | None]]


@dataclass
class _Check:
    probe: Probe
    critical: bool


class HealthRegistry:
    def __init__(self, service: str, node: str, timeout_s: float = 1.5):
        self.service = service
        self.node = node
        self.timeout_s = timeout_s
        self.started_at = time.time()
        self._checks: dict[str, _Check] = {}
        self._info: dict[str, Callable[[], Any]] = {}

    def add_check(self, name: str, probe: Probe, critical: bool = True) -> None:
        self._checks[name] = _Check(probe, critical)

    def add_info(self, name: str, supplier: Callable[[], Any]) -> None:
        """Extra non-check information to include in the response (config, queue stats...)."""
        self._info[name] = supplier

    async def _run_one(self, name: str, check: _Check) -> tuple[str, dict[str, Any]]:
        start = time.perf_counter()
        try:
            details = await asyncio.wait_for(check.probe(), timeout=self.timeout_s)
            result: dict[str, Any] = {"status": "UP"}
            if details:
                result.update(details)
        except Exception as exc:  # noqa: BLE001
            result = {"status": "DOWN", "error": f"{type(exc).__name__}: {exc}"[:200]}
        result["latency_ms"] = round((time.perf_counter() - start) * 1000, 2)
        result["critical"] = check.critical
        return name, result

    async def run(self) -> tuple[int, dict[str, Any]]:
        results = await asyncio.gather(*(self._run_one(n, c) for n, c in self._checks.items()))
        checks = dict(results)
        critical_down = any(r["status"] != "UP" and r["critical"] for r in checks.values())
        any_down = any(r["status"] != "UP" for r in checks.values())
        status = "DOWN" if critical_down else ("DEGRADED" if any_down else "UP")
        body: dict[str, Any] = {
            "service": self.service,
            "node": self.node,
            "status": status,
            "uptime_s": round(time.time() - self.started_at, 1),
            "checks": checks,
        }
        for name, supplier in self._info.items():
            try:
                body[name] = supplier()
            except Exception as exc:  # noqa: BLE001
                body[name] = {"error": repr(exc)}
        return (503 if status == "DOWN" else 200), body
