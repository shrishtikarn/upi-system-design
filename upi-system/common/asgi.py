"""Pure-ASGI middleware that gives every request a request id, a structured
access log line and latency metrics.

Written as raw ASGI (instead of Starlette's BaseHTTPMiddleware) because it is
faster and keeps the request's contextvars visible inside the endpoint.
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from common.logs import EventLogger, new_request_id, request_id_var
from common.metrics import Metrics

ASGIApp = Callable[[dict, Callable, Callable], Awaitable[None]]

QUIET_PATHS = ("/health", "/health/live", "/metrics", "/metrics/prometheus")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, metrics: Metrics, log: EventLogger, log_requests: bool = True):
        self.app = app
        self.metrics = metrics
        self.log = log
        # The gateway turns this off and writes its own, richer "gateway.request" line.
        self.log_requests = log_requests

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = None
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                request_id = value.decode("latin-1")[:64]
                break
        request_id = request_id or new_request_id()
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            path = scope.get("path", "")
            # FastAPI stores the matched route in the scope -> use its template
            # ("/payments/{txn_id}") as the metric label to keep cardinality low.
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            if self.log_requests and path not in QUIET_PATHS:
                self.metrics.inc("http_requests_total", route=route, method=scope.get("method"), status=status["code"])
                self.metrics.observe("http_request_ms", elapsed_ms, route=route)
                self.log.info(
                    "http.request",
                    method=scope.get("method"),
                    path=path,
                    status=status["code"],
                    latency_ms=round(elapsed_ms, 2),
                )
            request_id_var.reset(token)
