import asyncio

from common.health import HealthRegistry
from common.metrics import Histogram, Metrics, quantile_from_buckets
from common.models import is_valid_vpa, normalize_vpa, rupees
from common.resilience import CircuitBreaker
from gateway.config import match_route, upstream_path
from gateway.upstreams import UpstreamNode, UpstreamPool


# ---------------------------------------------------------------- metrics
def test_histogram_quantiles_interpolate_inside_buckets():
    h = Histogram(bounds=(10, 20, 50, 100))
    for v in [5] * 50 + [15] * 40 + [80] * 10:
        h.observe(v)
    assert h.count == 100
    assert 0 < quantile_from_buckets(0.5, h.bounds, h.counts) <= 10
    assert 10 < quantile_from_buckets(0.9, h.bounds, h.counts) <= 20
    assert 50 < quantile_from_buckets(0.99, h.bounds, h.counts) <= 100


def test_values_above_last_bucket_go_to_inf():
    h = Histogram(bounds=(1, 2))
    h.observe(999)
    assert h.counts == [0, 0, 1]
    assert quantile_from_buckets(0.5, h.bounds, h.counts) == 2


def test_metrics_snapshot_and_prometheus_format():
    m = Metrics("svc", "node-1")
    m.inc("requests_total", route="/x", status=200)
    m.inc("requests_total", route="/x", status=200)
    m.set_gauge("queue_lag", 7)
    m.observe("latency_ms", 12.5, route="/x")
    snap = m.snapshot()
    assert snap["counters"]['requests_total{route="/x",status="200"}'] == 2
    assert snap["gauges"]["queue_lag"] == 7
    assert snap["histograms"]['latency_ms{route="/x"}']["count"] == 1
    text = m.prometheus()
    assert 'requests_total{route="/x",status="200",node="node-1",service="svc"} 2' in text
    assert 'latency_ms_bucket{route="/x",node="node-1",service="svc",le="+Inf"} 1' in text


# ---------------------------------------------------------------- health
def test_health_registry_up_degraded_down():
    async def ok():
        return {"detail": 1}

    async def broken():
        raise RuntimeError("down")

    reg = HealthRegistry("svc", "n1")
    reg.add_check("db", ok, critical=True)
    status, body = asyncio.run(reg.run())
    assert status == 200 and body["status"] == "UP" and body["checks"]["db"]["detail"] == 1

    reg.add_check("cache", broken, critical=False)
    status, body = asyncio.run(reg.run())
    assert status == 200 and body["status"] == "DEGRADED"

    reg.add_check("queue", broken, critical=True)
    status, body = asyncio.run(reg.run())
    assert status == 503 and body["status"] == "DOWN" and "RuntimeError" in body["checks"]["queue"]["error"]


# ---------------------------------------------------------------- gateway routing / load balancing
def test_route_matching_and_path_rewrite():
    assert match_route("/api/v1/payments").name == "payments"
    assert match_route("/api/v1/payments/123").name == "payments"
    assert match_route("/api/v1/accounts/alice@okaxis/balance").service == "account-service"
    assert match_route("/api/v1/paymentsX") is None
    assert match_route("/api/v2/payments") is None
    assert upstream_path("/api/v1/payments/abc") == "/payments/abc"


def _node(name, status="UP"):
    node = UpstreamNode("payment-service", name, f"http://{name}:8000",
                        CircuitBreaker(name, failure_threshold=1, reset_timeout_s=60))
    node.status = status
    return node


def test_round_robin_skips_down_nodes_and_open_circuits():
    a, b, c = _node("a"), _node("b"), _node("c")
    pool = UpstreamPool("payment-service", [a, b, c])
    firsts = [pool.candidates()[0].name for _ in range(6)]
    assert firsts == ["a", "b", "c", "a", "b", "c"]

    b.status = "DOWN"
    c.breaker.record_failure()  # threshold 1 -> OPEN
    assert {n.name for n in pool.candidates()} == {"a"}
    a.status = "DOWN"
    assert pool.candidates() == []


# ---------------------------------------------------------------- domain helpers
def test_vpa_validation_and_money_formatting():
    assert is_valid_vpa(normalize_vpa("  Alice@OKAXIS "))
    assert is_valid_vpa("user0001@mockbank")
    assert not is_valid_vpa("no-at-sign")
    assert not is_valid_vpa("a@b")           # handle too short
    assert rupees(15000) == "150.00" and rupees(5) == "0.05" and rupees(-1234) == "-12.34"
