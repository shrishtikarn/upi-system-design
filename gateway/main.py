"""API Gateway - the single public entry point of the UPI system.

Request pipeline (in this order):

  1. assign / propagate X-Request-ID        (common.asgi middleware)
  2. route lookup (longest prefix)           -> 404 ROUTE_NOT_FOUND
  3. rate limiting: per-client + global      -> 429 Too Many Requests (+ Retry-After)
     token buckets (gateway/ratelimit.py)
  4. request inspection                       -> 405 / 413 / 415 / 400
     (method, body size, content type,
      Idempotency-Key on payments)
  5. pick a healthy node (round robin,        -> 503 when no node is available
     circuit breaker per node)
  6. reverse-proxy the request, retry once    -> 502 / 504 on upstream failure
     on another node when it is safe
  7. structured log line + metrics

Ops endpoints: GET /health, GET /metrics, GET /metrics/prometheus, GET /stats
"""
from __future__ import annotations

import asyncio
import math
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from common.clients import create_redis
from common.logs import get_logger, node_id, request_id_var, setup_logging
from common.metrics import Metrics
from common.resilience import CircuitBreaker, CircuitOpenError, CircuitState
from common.web import instrument
from gateway.config import (
    API_PREFIX,
    ROUTES,
    SAFE_METHODS,
    WRITE_METHODS,
    GatewayConfig,
    Route,
    match_route,
    node_name,
    upstream_path,
)
from gateway.ratelimit import LocalTokenBucket, RateLimiter, RedisTokenBucket
from gateway.upstreams import HealthMonitor, UpstreamNode, UpstreamPool

setup_logging("gateway")
log = get_logger("gateway")
NODE = node_id()
CONFIG = GatewayConfig.from_env()
metrics = Metrics("gateway", NODE)

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding",
}
RESPONSE_DROP = HOP_BY_HOP | {"content-encoding", "x-request-id", "server", "date"}


def _on_breaker_change(name: str, old: CircuitState, new: CircuitState) -> None:
    log.warning("circuit.state_changed", circuit=name, previous=old.value, current=new.value)
    metrics.inc("gw_circuit_transitions_total", circuit=name, to=new.value)


def _build_nodes() -> list[UpstreamNode]:
    nodes: list[UpstreamNode] = []
    for routable, group in ((True, CONFIG.upstreams), (False, CONFIG.monitored)):
        for service, urls in group.items():
            for url in urls:
                name = node_name(url)
                breaker = CircuitBreaker(
                    name,
                    failure_threshold=CONFIG.breaker_failure_threshold,
                    reset_timeout_s=CONFIG.breaker_reset_timeout_s,
                    on_state_change=_on_breaker_change,
                )
                nodes.append(UpstreamNode(service, name, url.rstrip("/"), breaker, routable=routable))
    return nodes


NODES = _build_nodes()
POOLS = {service: UpstreamPool(service, [n for n in NODES if n.service == service]) for service in CONFIG.upstreams}


class _State:
    http: httpx.AsyncClient
    redis: Any
    limiter: RateLimiter
    monitor: HealthMonitor


state = _State()


@asynccontextmanager
async def lifespan(_: FastAPI):
    state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(CONFIG.upstream_timeout_s, connect=1.0),
        limits=httpx.Limits(max_connections=1000, max_keepalive_connections=200),
    )
    # Short socket timeout: if Redis is slow we would rather fall back to the
    # local limiter than add latency to every request.
    state.redis = create_redis(CONFIG.redis_url, socket_timeout_s=0.25)
    state.limiter = RateLimiter(
        RedisTokenBucket(state.redis),
        LocalTokenBucket(),
        CONFIG.global_policy,
        metrics,
        log,
        enabled=CONFIG.rate_limit_enabled,
    )
    state.monitor = HealthMonitor(state.http, NODES, CONFIG.health_interval_s, CONFIG.health_timeout_s, metrics, log)
    monitor_task = asyncio.create_task(state.monitor.run_forever())
    log.info("gateway.started", config=CONFIG.summary())
    try:
        yield
    finally:
        monitor_task.cancel()
        await state.http.aclose()
        await state.redis.aclose()


app = FastAPI(
    title="UPI API Gateway",
    version="1.0.0",
    lifespan=lifespan,
    description="Reverse proxy with token-bucket rate limiting, health-aware load balancing and circuit breakers.",
)
instrument(app, metrics, log, health=None, log_requests=False)


# --------------------------------------------------------------------------- helpers
class UpstreamServerError(Exception):
    """A 5xx from an upstream: returned to the client, but counted as a breaker failure."""

    def __init__(self, response: httpx.Response):
        super().__init__(f"upstream answered HTTP {response.status_code}")
        self.response = response


async def _send(method: str, url: str, body: bytes, headers: dict[str, str]) -> httpx.Response:
    response = await state.http.request(method, url, content=body or None, headers=headers)
    if response.status_code >= 500:
        raise UpstreamServerError(response)
    return response


def _client_id(request: Request) -> str:
    """Who is this request "from" for rate-limiting purposes.

    The mobile app sends a stable device id in X-Client-ID. In production this
    would be the authenticated user id taken from a verified JWT.
    """
    client_id = request.headers.get("x-client-id") or (request.client.host if request.client else "")
    return client_id.strip()[:64] or "anonymous"


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": code, "message": message, "request_id": request_id_var.get(), **extra}


def _forward_headers(request: Request, client_id: str) -> dict[str, str]:
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    headers["x-request-id"] = request_id_var.get() or ""
    headers["x-client-id"] = client_id
    client_ip = request.client.host if request.client else ""
    previous = request.headers.get("x-forwarded-for")
    headers["x-forwarded-for"] = f"{previous}, {client_ip}" if previous else client_ip
    headers["x-forwarded-proto"] = request.url.scheme
    return headers


def _response_headers(upstream: httpx.Response, extra: dict[str, str], node: UpstreamNode) -> dict[str, str]:
    headers = {k: v for k, v in upstream.headers.items() if k.lower() not in RESPONSE_DROP}
    headers.update(extra)
    headers["X-Upstream-Node"] = node.name
    return headers


def _finish(response: Response, *, route: Route | None, outcome: str, started: float, fields: dict[str, Any]) -> Response:
    elapsed_ms = (time.perf_counter() - started) * 1000
    route_name = route.name if route else "none"
    metrics.inc("gw_requests_total", route=route_name, outcome=outcome, status=response.status_code)
    metrics.observe("gw_latency_ms", elapsed_ms, route=route_name, outcome=outcome)
    response.headers["X-Gateway-Latency-Ms"] = f"{elapsed_ms:.2f}"
    log.info("gateway.request", status=response.status_code, outcome=outcome,
             latency_ms=round(elapsed_ms, 2), **fields)
    return response


# --------------------------------------------------------------------------- ops endpoints
@app.get("/", tags=["ops"])
async def index() -> dict[str, Any]:
    return {
        "service": "UPI API Gateway",
        "health": "/health",
        "metrics": "/metrics",
        "stats": "/stats",
        "dashboard": "/dashboard",
        "routes": [{"prefix": r.prefix, "service": r.service, "methods": sorted(r.methods)} for r in ROUTES],
    }


@app.get("/health", tags=["ops"], summary="Health of the gateway and of every service node")
async def health(live: bool = False) -> JSONResponse:
    if live:  # force a fresh probe instead of the cached result of the background monitor
        await state.monitor.check_all()
    try:
        await asyncio.wait_for(state.redis.ping(), timeout=0.5)
        redis_status = "UP"
    except Exception:  # noqa: BLE001
        redis_status = "DOWN"

    services: dict[str, dict[str, Any]] = {}
    for service in list(CONFIG.upstreams) + list(CONFIG.monitored):
        members = [n for n in NODES if n.service == service]
        services[service] = {
            "role": "routed" if service in CONFIG.upstreams else "monitored",
            "healthy_nodes": sum(1 for n in members if n.status in ("UP", "DEGRADED")),
            "total_nodes": len(members),
        }
    routed_down = any(s["healthy_nodes"] == 0 for s in services.values() if s["role"] == "routed")
    degraded = redis_status != "UP" or any(n.status != "UP" for n in NODES)
    status = "DOWN" if routed_down else ("DEGRADED" if degraded else "UP")

    body = {
        "service": "gateway",
        "node": NODE,
        "status": status,
        "uptime_s": round(time.time() - metrics.started_at, 1),
        "rate_limiter": {
            "enabled": CONFIG.rate_limit_enabled,
            "backend": state.limiter.active_backend,
            "redis": redis_status,
            "policies": CONFIG.summary()["policies"],
        },
        "services": services,
        "nodes": [n.describe() for n in NODES],
    }
    return JSONResponse(body, status_code=503 if status == "DOWN" else 200)


@app.get("/stats", tags=["ops"], summary="Metrics of the gateway and every node in one document")
async def stats() -> dict[str, Any]:
    async def fetch(node: UpstreamNode) -> tuple[str, Any]:
        try:
            resp = await state.http.get(f"{node.url}/metrics", timeout=2.0)
            return node.name, resp.json()
        except Exception as exc:  # noqa: BLE001
            return node.name, {"error": repr(exc)}

    results = await asyncio.gather(*(fetch(n) for n in NODES))
    return {
        "generated_at": time.time(),
        "config": CONFIG.summary(),
        "rate_limiter_backend": state.limiter.active_backend,
        "gateway": metrics.snapshot(),
        "nodes": dict(results),
    }


# --------------------------------------------------------------------------- live dashboard
# GET /dashboard serves one static page; the page polls /health, /stats and the
# /dashboard/api/* routes below every 2 s. These are ops endpoints (like /health):
# not rate limited and not counted as API traffic. Demo only - no authentication.
DASHBOARD_HTML = Path(__file__).parent / "static" / "dashboard.html"
PAY_HTML = Path(__file__).parent / "static" / "pay.html"
BANK_MODES = {"normal", "flaky", "slow", "down"}


@app.get("/dashboard", include_in_schema=False)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_HTML.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@app.get("/pay", include_in_schema=False)
async def pay_page() -> HTMLResponse:
    """Person-to-person payment console: pick a payer, a payee and an amount, then
    watch the payment move PENDING -> PROCESSING -> SUCCESS/FAILED. It sends real
    requests through /api/v1 (so they are rate limited like any app)."""
    return HTMLResponse(PAY_HTML.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@app.get("/dashboard/api/users", include_in_schema=False)
async def dashboard_users(q: str = "", limit: int = 200) -> JSONResponse:
    return await _internal("account-service", "GET",
                           f"/admin/users?q={quote(q[:64])}&limit={max(1, min(limit, 1100))}")


async def _internal(service: str, method: str, path: str, body: Any = None) -> JSONResponse:
    """Call one node of an internal service and pass its JSON answer through."""
    nodes = [n for n in NODES if n.service == service]
    candidates = [n for n in nodes if n.status != "DOWN"] or nodes
    last_error = "no node configured"
    for node in candidates:
        try:
            resp = await state.http.request(method, node.url + path, json=body, timeout=3.0)
        except httpx.HTTPError as exc:
            last_error = type(exc).__name__
            continue
        try:
            content = resp.json()
        except ValueError:
            content = {"error": "BAD_UPSTREAM_RESPONSE", "status": resp.status_code}
        return JSONResponse(content, status_code=resp.status_code)
    return JSONResponse({"error": "SERVICE_UNAVAILABLE", "message": f"{service}: {last_error}"}, status_code=503)


@app.get("/dashboard/api/summary", include_in_schema=False)
async def dashboard_summary() -> JSONResponse:
    return await _internal("account-service", "GET", "/admin/summary")


@app.get("/dashboard/api/transactions", include_in_schema=False)
async def dashboard_transactions(limit: int = 20) -> JSONResponse:
    return await _internal("account-service", "GET", f"/admin/transactions/recent?limit={max(1, min(limit, 100))}")


@app.get("/dashboard/api/transactions/{txn_id}/events", include_in_schema=False)
async def dashboard_transaction_events(txn_id: str) -> JSONResponse:
    try:
        txn = uuid.UUID(txn_id)
    except ValueError:
        return JSONResponse({"error": "INVALID_TXN_ID", "message": "not a transaction id"}, status_code=400)
    return await _internal("account-service", "GET", f"/admin/transactions/{txn}/events")


@app.post("/dashboard/api/chaos", include_in_schema=False)
async def dashboard_set_chaos(request: Request) -> JSONResponse:
    try:
        mode = (await request.json()).get("mode")
    except (ValueError, AttributeError):
        mode = None
    if mode not in BANK_MODES:
        return JSONResponse({"error": "UNKNOWN_MODE", "available": sorted(BANK_MODES)}, status_code=400)
    log.warning("dashboard.chaos", mode=mode)
    return await _internal("bank-mock", "POST", "/admin/chaos", {"mode": mode})


# --------------------------------------------------------------------------- the proxy
@app.api_route(f"{API_PREFIX}/{{full_path:path}}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
               include_in_schema=False)
async def proxy(request: Request, full_path: str) -> Response:
    started = time.perf_counter()
    method = request.method
    path = request.url.path
    client_id = _client_id(request)
    route = match_route(path)
    fields: dict[str, Any] = {"method": method, "path": path, "client_id": client_id,
                              "route": route.name if route else None}

    def done(response: Response, outcome: str) -> Response:
        return _finish(response, route=route, outcome=outcome, started=started, fields=fields)

    if route is None:
        return done(JSONResponse(_error("ROUTE_NOT_FOUND", f"No route for {path}"), status_code=404), "rejected")

    # 1) Rate limiting - done first so that floods are shed before any other work.
    policy = CONFIG.write_policy if method in WRITE_METHODS else CONFIG.read_policy
    decision = await state.limiter.check(client_id, policy)
    rl_headers = {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Policy": policy.name,
    }
    fields.update(rl_policy=policy.name, rl_backend=decision.backend, rl_remaining=decision.remaining)
    if not decision.allowed:
        fields["rl_scope"] = decision.scope
        metrics.inc("gw_rate_limited_total", scope=decision.scope, policy=policy.name)
        retry_after = max(1, math.ceil(decision.retry_after_s))
        body = _error("RATE_LIMITED", "Too many requests - please slow down.",
                      scope=decision.scope, retry_after_s=decision.retry_after_s)
        return done(JSONResponse(body, status_code=429, headers={**rl_headers, "Retry-After": str(retry_after)}),
                    "rate_limited")

    # 2) Request inspection
    if method not in route.methods:
        return done(JSONResponse(_error("METHOD_NOT_ALLOWED", f"{method} not allowed on {route.prefix}"),
                                 status_code=405, headers={"Allow": ", ".join(sorted(route.methods))}), "rejected")
    body_bytes = await request.body()
    if len(body_bytes) > CONFIG.max_body_bytes:
        return done(JSONResponse(_error("PAYLOAD_TOO_LARGE", f"Body exceeds {CONFIG.max_body_bytes} bytes"),
                                 status_code=413), "rejected")
    if method in WRITE_METHODS and body_bytes and "application/json" not in request.headers.get("content-type", "").lower():
        return done(JSONResponse(_error("UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json"),
                                 status_code=415), "rejected")
    idempotency_key = request.headers.get("idempotency-key")
    if method in route.idempotency_required_for and not idempotency_key:
        return done(JSONResponse(_error("MISSING_IDEMPOTENCY_KEY",
                                        "Send a unique Idempotency-Key header with every payment"),
                                 status_code=400), "rejected")

    # 3) Load-balance across healthy nodes, 4) proxy
    candidates = POOLS[route.service].candidates()
    if not candidates:
        fields["error"] = "NO_HEALTHY_UPSTREAM"
        return done(JSONResponse(_error("SERVICE_UNAVAILABLE", f"No healthy {route.service} node"),
                                 status_code=503, headers={"Retry-After": "2"}), "upstream_error")

    # Retrying on another node is only safe when repeating the request cannot
    # double-charge: reads, or writes carrying an idempotency key.
    retry_allowed = method in SAFE_METHODS or bool(idempotency_key)
    max_attempts = min(len(candidates), 2 if retry_allowed else 1)
    target = upstream_path(path) + (f"?{request.url.query}" if request.url.query else "")
    headers = _forward_headers(request, client_id)
    last_status, last_error = 503, "CIRCUIT_OPEN"

    for attempt, node in enumerate(candidates[:max_attempts], start=1):
        if attempt > 1:
            metrics.inc("gw_retries_total", route=route.name)
            log.warning("gateway.retry_other_node", node=node.name, attempt=attempt, previous_error=last_error)
        t0 = time.perf_counter()
        try:
            upstream = await node.breaker.call(_send, method, node.url + target, body_bytes, headers)
        except UpstreamServerError as exc:
            metrics.inc("gw_upstream_errors_total", node=node.name, kind=f"http_{exc.response.status_code}")
            last_status, last_error = exc.response.status_code, f"HTTP_{exc.response.status_code}"
            if attempt < max_attempts:
                continue
            upstream = exc.response  # nothing else to try: pass the upstream's error through
        except CircuitOpenError:
            last_status, last_error = 503, "CIRCUIT_OPEN"
            continue
        except httpx.TimeoutException:
            metrics.inc("gw_upstream_errors_total", node=node.name, kind="timeout")
            last_status, last_error = 504, "UPSTREAM_TIMEOUT"
            continue
        except httpx.HTTPError as exc:
            metrics.inc("gw_upstream_errors_total", node=node.name, kind="unreachable")
            last_status, last_error = 502, "UPSTREAM_UNREACHABLE"
            fields["upstream_exception"] = type(exc).__name__
            continue

        upstream_ms = (time.perf_counter() - t0) * 1000
        metrics.observe("gw_upstream_ms", upstream_ms, service=route.service, node=node.name)
        fields.update(upstream=node.name, attempts=attempt, upstream_ms=round(upstream_ms, 2))
        response = Response(content=upstream.content, status_code=upstream.status_code,
                            headers=_response_headers(upstream, rl_headers, node))
        return done(response, "proxied")

    fields.update(attempts=max_attempts, error=last_error)
    error_headers = {**rl_headers, "Retry-After": "1"} if last_status == 503 else rl_headers
    return done(JSONResponse(_error(last_error, f"{route.service} did not answer successfully"),
                             status_code=last_status, headers=error_headers), "upstream_error")
