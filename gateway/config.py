"""Gateway configuration: route table, upstream nodes and rate-limit policies.

Everything is overridable through environment variables (see docker-compose.yml).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from common.env import env_bool, env_float, env_int, env_list, env_str
from gateway.ratelimit import BucketPolicy

API_PREFIX = "/api/v1"
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class Route:
    name: str                       # used as metric label and in logs
    prefix: str                     # public path prefix, e.g. /api/v1/payments
    service: str                    # upstream pool name
    methods: frozenset[str]
    idempotency_required_for: frozenset[str] = field(default_factory=frozenset)

    def matches(self, path: str) -> bool:
        return path == self.prefix or path.startswith(self.prefix + "/")


ROUTES: tuple[Route, ...] = (
    Route("payments", f"{API_PREFIX}/payments", "payment-service", frozenset({"GET", "POST"}),
          idempotency_required_for=frozenset({"POST"})),
    Route("accounts", f"{API_PREFIX}/accounts", "account-service", frozenset({"GET"})),
    Route("vpa", f"{API_PREFIX}/vpa", "account-service", frozenset({"GET"})),
)


def match_route(path: str, routes: tuple[Route, ...] = ROUTES) -> Route | None:
    """Longest-prefix match."""
    best: Route | None = None
    for route in routes:
        if route.matches(path) and (best is None or len(route.prefix) > len(best.prefix)):
            best = route
    return best


def upstream_path(path: str) -> str:
    """/api/v1/payments/abc -> /payments/abc (services don't know about the public prefix)."""
    if path.startswith(API_PREFIX):
        return path[len(API_PREFIX):] or "/"
    return path


def node_name(url: str) -> str:
    return urlparse(url).hostname or url


@dataclass(frozen=True)
class GatewayConfig:
    upstreams: dict[str, list[str]]           # routed services
    monitored: dict[str, list[str]]           # health-checked only (workers, bank switch)
    redis_url: str
    rate_limit_enabled: bool
    read_policy: BucketPolicy
    write_policy: BucketPolicy
    global_policy: BucketPolicy
    max_body_bytes: int
    upstream_timeout_s: float
    health_interval_s: float
    health_timeout_s: float
    breaker_failure_threshold: int
    breaker_reset_timeout_s: float

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        return cls(
            upstreams={
                "payment-service": env_list("UPSTREAM_PAYMENT", "http://payment-service-1:8000,http://payment-service-2:8000"),
                "account-service": env_list("UPSTREAM_ACCOUNT", "http://account-service:8000"),
            },
            monitored={
                "worker": env_list("MONITOR_WORKER", "http://worker-1:8000,http://worker-2:8000"),
                "bank-mock": env_list("MONITOR_BANK", "http://bank-mock:8000"),
            },
            redis_url=env_str("REDIS_URL", "redis://redis:6379/0"),
            rate_limit_enabled=env_bool("RATE_LIMIT_ENABLED", True),
            read_policy=BucketPolicy("read", env_float("RL_READ_CAPACITY", 40), env_float("RL_READ_REFILL_PER_S", 20)),
            write_policy=BucketPolicy("write", env_float("RL_WRITE_CAPACITY", 10), env_float("RL_WRITE_REFILL_PER_S", 2)),
            global_policy=BucketPolicy("global", env_float("RL_GLOBAL_CAPACITY", 500), env_float("RL_GLOBAL_REFILL_PER_S", 300)),
            max_body_bytes=env_int("MAX_BODY_BYTES", 16 * 1024),
            upstream_timeout_s=env_float("UPSTREAM_TIMEOUT_S", 5.0),
            health_interval_s=env_float("HEALTH_INTERVAL_S", 3.0),
            health_timeout_s=env_float("HEALTH_TIMEOUT_S", 1.5),
            breaker_failure_threshold=env_int("CB_FAILURE_THRESHOLD", 5),
            breaker_reset_timeout_s=env_float("CB_RESET_TIMEOUT_S", 10.0),
        )

    def summary(self) -> dict:
        return {
            "rate_limit_enabled": self.rate_limit_enabled,
            "policies": {
                "read (per client)": self.read_policy.describe(),
                "write (per client)": self.write_policy.describe(),
                "global": self.global_policy.describe(),
            },
            "upstreams": self.upstreams,
            "monitored": self.monitored,
            "max_body_bytes": self.max_body_bytes,
            "upstream_timeout_s": self.upstream_timeout_s,
            "circuit_breaker": {
                "failure_threshold": self.breaker_failure_threshold,
                "reset_timeout_s": self.breaker_reset_timeout_s,
            },
        }
