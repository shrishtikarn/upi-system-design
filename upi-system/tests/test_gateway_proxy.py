"""Behaviour of the gateway's proxy handler: routing, inspection, rate limiting,
retries on another node, circuit breaking. Upstreams are simulated with
httpx.MockTransport, so no services need to run."""
import asyncio

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from starlette.requests import Request  # noqa: E402

import gateway.main as gw  # noqa: E402
from common.resilience import CircuitBreaker, CircuitState  # noqa: E402
from gateway.ratelimit import LocalTokenBucket, RateLimiter  # noqa: E402

behaviour: dict[str, str] = {}  # node name -> "ok" | "503" | "refuse" | "timeout"
seen: list[tuple[str, str, str]] = []


def upstream(request: httpx.Request) -> httpx.Response:
    node = request.url.host
    seen.append((node, request.method, request.url.path))
    mode = behaviour.get(node, "ok")
    if mode == "refuse":
        raise httpx.ConnectError("connection refused", request=request)
    if mode == "timeout":
        raise httpx.ReadTimeout("timed out", request=request)
    if mode == "503":
        return httpx.Response(503, json={"error": "down"})
    status = 202 if request.method == "POST" else 200
    return httpx.Response(status, json={"node": node, "path": request.url.path}, headers={"X-Cache": "MISS"})


@pytest.fixture(autouse=True)
def fresh_gateway():
    behaviour.clear()
    seen.clear()
    gw.state.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    gw.state.limiter = RateLimiter(None, LocalTokenBucket(), gw.CONFIG.global_policy, gw.metrics, gw.log)
    for node in gw.NODES:
        node.status = "UP"
        node.breaker = CircuitBreaker(node.name, failure_threshold=3, reset_timeout_s=60)
    yield


def call(method, path, headers=None, body=b"", client_id="tester"):
    hdrs = {"x-client-id": client_id, **(headers or {})}
    scope = {
        "type": "http", "method": method, "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in hdrs.items()],
        "client": ("10.0.0.7", 5555), "server": ("gateway", 8080), "scheme": "http", "root_path": "",
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def run():
        return await gw.proxy(Request(scope, receive), path.removeprefix("/api/v1/"))

    return asyncio.run(run())


PAY_HEADERS = {"content-type": "application/json", "idempotency-key": "k-1"}


def test_routes_read_to_account_service_and_rewrites_path():
    resp = call("GET", "/api/v1/accounts/alice@okaxis/balance")
    assert resp.status_code == 200
    assert seen == [("account-service", "GET", "/accounts/alice@okaxis/balance")]
    assert resp.headers["x-upstream-node"] == "account-service"
    assert resp.headers["x-ratelimit-remaining"] == "39" and resp.headers["x-cache"] == "MISS"


def test_round_robin_between_payment_nodes():
    nodes = {call("POST", "/api/v1/payments", PAY_HEADERS, b"{}").headers["x-upstream-node"] for _ in range(4)}
    assert nodes == {"payment-service-1", "payment-service-2"}


def test_inspection_rejects_bad_requests():
    assert call("GET", "/api/v1/unknown").status_code == 404
    assert call("DELETE", "/api/v1/accounts/x@y").status_code == 405
    assert call("POST", "/api/v1/payments", {"content-type": "application/json"}, b"{}").status_code == 400
    assert call("POST", "/api/v1/payments", {"content-type": "text/plain", "idempotency-key": "k"}, b"hi").status_code == 415
    assert call("POST", "/api/v1/payments", PAY_HEADERS, b"x" * 20000).status_code == 413
    assert seen == []  # nothing reached a service


def test_rate_limit_returns_429_with_retry_after():
    statuses = [call("GET", "/api/v1/vpa/bob@oksbi", client_id="greedy").status_code for _ in range(45)]
    assert statuses.count(200) == 40 and statuses.count(429) == 5
    resp = call("GET", "/api/v1/vpa/bob@oksbi", client_id="greedy")
    assert resp.status_code == 429 and int(resp.headers["retry-after"]) >= 1
    assert call("GET", "/api/v1/vpa/bob@oksbi", client_id="someone-else").status_code == 200


def test_failed_node_is_retried_on_the_other_node():
    behaviour["payment-service-1"] = "503"
    for _ in range(4):
        assert call("POST", "/api/v1/payments", PAY_HEADERS, b"{}").status_code == 202


def test_breaker_opens_and_node_is_skipped():
    behaviour["payment-service-1"] = "refuse"
    for _ in range(8):
        call("GET", "/api/v1/payments/abc")
    node1 = next(n for n in gw.NODES if n.name == "payment-service-1")
    assert node1.breaker.state is CircuitState.OPEN
    seen.clear()
    for _ in range(4):
        assert call("GET", "/api/v1/payments/abc").status_code == 200
    assert {n for n, _, _ in seen} == {"payment-service-2"}


def test_upstream_errors_map_to_502_504_503():
    behaviour["account-service"] = "refuse"
    assert call("GET", "/api/v1/vpa/a1@ok").status_code == 502
    behaviour["account-service"] = "timeout"
    assert call("GET", "/api/v1/vpa/a1@ok").status_code == 504
    for node in gw.NODES:
        node.status = "DOWN"
    assert call("GET", "/api/v1/vpa/a1@ok").status_code == 503
