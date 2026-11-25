"""Token Bucket rate limiter.

Algorithm
---------
Each bucket holds at most `capacity` tokens and is refilled continuously at
`refill_per_s` tokens/second. A request costs 1 token. If the bucket has a
token the request passes, otherwise it is rejected with 429 and a Retry-After
equal to the time until the next token arrives.

    capacity     -> how big a burst we tolerate   (e.g. a user tapping "Pay" 10x)
    refill_per_s -> the sustained rate we allow   (e.g. 2 payments / second)

We keep the bucket state as (tokens, last_refill_ts) and compute the refill
lazily on each request, so no background timer is needed:

    tokens = min(capacity, tokens + (now - last_ts) * refill_per_s)

Two buckets are checked for every request, atomically:
  1. per-client bucket  rl:client:<policy>:<client_id>  -> fairness / abuse
  2. global bucket      rl:global                       -> protects the backend
A token is taken from both only if *both* have one.

Where the state lives
---------------------
* Primary: Redis, updated by a Lua script. Redis runs a script atomically, so
  concurrent requests (from several gateway replicas) can never both take the
  last token. The script reads the clock with Redis TIME, so gateway replicas
  with slightly different clocks still agree.
* Fallback: an in-process LocalTokenBucket with the same semantics. If Redis is
  unreachable the gateway keeps limiting (per replica) instead of failing open
  or failing every request.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Callable

from common.keys import RL_GLOBAL_KEY, rl_client_key
from common.logs import EventLogger
from common.metrics import Metrics


@dataclass(frozen=True)
class BucketPolicy:
    name: str
    capacity: float
    refill_per_s: float

    def describe(self) -> dict[str, Any]:
        return {"capacity": self.capacity, "refill_per_s": self.refill_per_s}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    scope: str | None        # "client" | "global" when rejected
    remaining: int           # whole tokens left in the client's bucket
    limit: int               # client bucket capacity
    retry_after_s: float
    backend: str             # "redis" | "local" | "disabled"


# KEYS = bucket keys (client first, then global)
# ARGV = [cost, cap_1, rate_1, cap_2, rate_2, ...]
# returns {allowed(0|1), blocked_index(0 = none), tokens_left_in_bucket_1, retry_after_s}
TOKEN_BUCKET_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local cost = tonumber(ARGV[1])
local n = #KEYS
local tokens = {}
local allowed = 1
local blocked = 0
local retry_after = 0

for i = 1, n do
  local cap = tonumber(ARGV[2 * i])
  local rate = tonumber(ARGV[2 * i + 1])
  local state = redis.call('HMGET', KEYS[i], 'tokens', 'ts')
  local tk = tonumber(state[1])
  local ts = tonumber(state[2])
  if tk == nil or ts == nil then
    tk = cap
    ts = now
  end
  tk = math.min(cap, tk + math.max(0, now - ts) * rate)
  tokens[i] = tk
  if tk < cost then
    allowed = 0
    local wait = (cost - tk) / rate
    if wait > retry_after then
      retry_after = wait
      blocked = i
    end
  end
end

for i = 1, n do
  local cap = tonumber(ARGV[2 * i])
  local rate = tonumber(ARGV[2 * i + 1])
  if allowed == 1 then
    tokens[i] = tokens[i] - cost
  end
  redis.call('HSET', KEYS[i], 'tokens', tostring(tokens[i]), 'ts', tostring(now))
  -- an idle bucket refills completely after cap/rate seconds; after that the
  -- key carries no information, so let Redis expire it
  redis.call('EXPIRE', KEYS[i], math.ceil(cap / rate) + 5)
end

return {allowed, blocked, tostring(tokens[1]), tostring(retry_after)}
"""

AcquireResult = tuple[bool, int, float, float]  # allowed, blocked_index (1-based, 0 = none), tokens_left_b1, retry_after


class LocalTokenBucket:
    """Same algorithm as the Lua script, kept in process memory."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, max_keys: int = 100_000):
        self._clock = clock
        self._max_keys = max_keys
        self._state: dict[str, list[float]] = {}  # key -> [tokens, last_ts]

    def acquire(self, buckets: list[tuple[str, BucketPolicy]], cost: float = 1.0) -> AcquireResult:
        now = self._clock()
        refilled: list[float] = []
        allowed, blocked, retry_after = True, 0, 0.0
        for i, (key, policy) in enumerate(buckets, start=1):
            tokens, ts = self._state.get(key, (policy.capacity, now))
            tokens = min(policy.capacity, tokens + max(0.0, now - ts) * policy.refill_per_s)
            refilled.append(tokens)
            if tokens < cost:
                allowed = False
                wait = (cost - tokens) / policy.refill_per_s
                if wait > retry_after:
                    retry_after, blocked = wait, i
        for (key, _), tokens in zip(buckets, refilled):
            self._state[key] = [tokens - cost if allowed else tokens, now]
        if len(self._state) > self._max_keys:
            self._state.clear()  # crude memory guard; buckets simply restart full
        return allowed, blocked, refilled[0] - (cost if allowed else 0), retry_after


class RedisTokenBucket:
    def __init__(self, redis: Any):
        self._script = redis.register_script(TOKEN_BUCKET_LUA)

    async def acquire(self, buckets: list[tuple[str, BucketPolicy]], cost: float = 1.0) -> AcquireResult:
        keys = [key for key, _ in buckets]
        args: list[float] = [cost]
        for _, policy in buckets:
            args += [policy.capacity, policy.refill_per_s]
        allowed, blocked, tokens_left, retry_after = await self._script(keys=keys, args=args)
        return int(allowed) == 1, int(blocked), float(tokens_left), float(retry_after)


class RateLimiter:
    def __init__(
        self,
        redis_bucket: RedisTokenBucket | None,
        local_bucket: LocalTokenBucket,
        global_policy: BucketPolicy,
        metrics: Metrics,
        log: EventLogger,
        enabled: bool = True,
        redis_retry_after_s: float = 5.0,
    ):
        self._redis = redis_bucket
        self._local = local_bucket
        self.global_policy = global_policy
        self.metrics = metrics
        self.log = log
        self.enabled = enabled
        self._redis_retry_after_s = redis_retry_after_s
        self._redis_down_until = 0.0

    @property
    def active_backend(self) -> str:
        if not self.enabled:
            return "disabled"
        if self._redis is None or time.monotonic() < self._redis_down_until:
            return "local"
        return "redis"

    async def check(self, client_id: str, policy: BucketPolicy) -> Decision:
        if not self.enabled:
            return Decision(True, None, int(policy.capacity), int(policy.capacity), 0.0, "disabled")

        buckets = [(rl_client_key(client_id, policy.name), policy), (RL_GLOBAL_KEY, self.global_policy)]
        result: AcquireResult | None = None
        backend = "redis"
        if self._redis is not None and time.monotonic() >= self._redis_down_until:
            try:
                result = await self._redis.acquire(buckets)
            except Exception as exc:  # noqa: BLE001 - Redis down/slow -> local fallback
                self._redis_down_until = time.monotonic() + self._redis_retry_after_s
                self.metrics.inc("rl_backend_errors_total")
                self.log.warning("ratelimit.redis_unavailable", error=repr(exc), fallback="local",
                                 retry_redis_in_s=self._redis_retry_after_s)
        if result is None:
            backend = "local"
            result = self._local.acquire(buckets)

        allowed, blocked, tokens_left, retry_after = result
        scope = None if allowed else ("client" if blocked == 1 else "global")
        return Decision(
            allowed=allowed,
            scope=scope,
            remaining=max(0, math.floor(tokens_left)),
            limit=int(policy.capacity),
            retry_after_s=round(retry_after, 3),
            backend=backend,
        )
