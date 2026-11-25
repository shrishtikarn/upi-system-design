"""FastAPI glue shared by all HTTP services: middleware, error format, /health, /metrics."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from common.asgi import RequestContextMiddleware
from common.health import HealthRegistry
from common.logs import EventLogger, request_id_var
from common.metrics import Metrics


def instrument(
    app: FastAPI,
    metrics: Metrics,
    log: EventLogger,
    health: HealthRegistry | None = None,
    log_requests: bool = True,
) -> None:
    app.add_middleware(RequestContextMiddleware, metrics=metrics, log=log, log_requests=log_requests)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Uniform error body: {"error": CODE, "message": ..., "request_id": ...}
        if isinstance(exc.detail, dict):
            body = exc.detail
        else:
            body = {"error": "HTTP_ERROR", "message": str(exc.detail), "request_id": request_id_var.get()}
        return JSONResponse(body, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.get("/health/live", tags=["ops"], summary="Liveness: the process is up (used by Docker)")
    async def liveness() -> dict:
        return {"status": "UP"}

    if health is not None:

        @app.get("/health", tags=["ops"], summary="Health of this node and its dependencies")
        async def health_endpoint() -> JSONResponse:
            status_code, body = await health.run()
            return JSONResponse(body, status_code=status_code)

    @app.get("/metrics", tags=["ops"], summary="Counters, gauges and latency histograms (JSON)")
    async def metrics_endpoint() -> JSONResponse:
        return JSONResponse(metrics.snapshot())

    @app.get("/metrics/prometheus", tags=["ops"], summary="Same metrics in Prometheus text format")
    async def metrics_prometheus() -> PlainTextResponse:
        return PlainTextResponse(metrics.prometheus())
