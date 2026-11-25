import asyncio

import pytest

from common.logs import get_logger
from common.metrics import Metrics
from gateway.ratelimit import BucketPolicy, LocalTokenBucket, RateLimiter


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


CLIENT = BucketPolicy("read", capacity=5, refill_per_s=1)
GLOBAL = BucketPolicy("global", capacity=100, refill_per_s=50)


def test_burst_up_to_capacity_then_reject():
    clock = FakeClock()
    bucket = LocalTokenBucket(clock=clock)
    results = [bucket.acquire([("c", CLIENT), ("g", GLOBAL)])[0] for _ in range(6)]
    assert results == [True] * 5 + [False]


def test_retry_after_is_time_until_next_token():
    clock = FakeClock()
    bucket = LocalTokenBucket(clock=clock)
    for _ in range(5):
        bucket.acquire([("c", CLIENT)])
    allowed, blocked, _, retry_after = bucket.acquire([("c", CLIENT)])
    assert not allowed and blocked == 1
    assert retry_after == pytest.approx(1.0)
    clock.advance(0.5)
    assert bucket.acquire([("c", CLIENT)])[3] == pytest.approx(0.5)


def test_refill_is_continuous_and_capped():
    clock = FakeClock()
    bucket = LocalTokenBucket(clock=clock)
    for _ in range(5):
        bucket.acquire([("c", CLIENT)])
    clock.advance(2.0)  # two tokens back
    assert [bucket.acquire([("c", CLIENT)])[0] for _ in range(3)] == [True, True, False]
    clock.advance(3600)  # long idle: refill is capped at capacity
    assert sum(bucket.acquire([("c", CLIENT)])[0] for _ in range(10)) == 5


def test_global_bucket_blocks_and_nothing_is_consumed_on_reject():
    clock = FakeClock()
    bucket = LocalTokenBucket(clock=clock)
    tiny_global = BucketPolicy("global", capacity=2, refill_per_s=1)
    assert bucket.acquire([("a", CLIENT), ("g", tiny_global)])[0]
    assert bucket.acquire([("b", CLIENT), ("g", tiny_global)])[0]
    allowed, blocked, tokens_left, _ = bucket.acquire([("c", CLIENT), ("g", tiny_global)])
    assert not allowed and blocked == 2          # the *global* bucket said no
    assert tokens_left == pytest.approx(5)       # client "c" was not charged for a rejected request


def test_clients_are_isolated():
    clock = FakeClock()
    bucket = LocalTokenBucket(clock=clock)
    for _ in range(10):
        bucket.acquire([("greedy", CLIENT), ("g", GLOBAL)])
    assert bucket.acquire([("polite", CLIENT), ("g", GLOBAL)])[0]


class BrokenRedisBucket:
    calls = 0

    async def acquire(self, buckets, cost=1.0):
        BrokenRedisBucket.calls += 1
        raise ConnectionError("redis down")


def test_rate_limiter_falls_back_to_local_when_redis_fails():
    limiter = RateLimiter(BrokenRedisBucket(), LocalTokenBucket(), GLOBAL, Metrics("t", "t"), get_logger("t"))

    async def scenario():
        first = await limiter.check("phone-1", CLIENT)
        second = await limiter.check("phone-1", CLIENT)
        return first, second

    first, second = asyncio.run(scenario())
    assert first.allowed and first.backend == "local"
    assert second.backend == "local"
    assert BrokenRedisBucket.calls == 1            # Redis is not hammered while it is down
    assert limiter.active_backend == "local"


def test_rate_limiter_scope_and_remaining():
    limiter = RateLimiter(None, LocalTokenBucket(), GLOBAL, Metrics("t", "t"), get_logger("t"))

    async def scenario():
        return [await limiter.check("phone-2", CLIENT) for _ in range(6)]

    decisions = asyncio.run(scenario())
    assert [d.remaining for d in decisions[:5]] == [4, 3, 2, 1, 0]
    assert decisions[5].allowed is False and decisions[5].scope == "client" and decisions[5].retry_after_s > 0


def test_disabled_limiter_allows_everything():
    limiter = RateLimiter(None, LocalTokenBucket(), GLOBAL, Metrics("t", "t"), get_logger("t"), enabled=False)
    async def scenario():
        return await asyncio.gather(*(limiter.check("x", CLIENT) for _ in range(50)))

    decisions = asyncio.run(scenario())
    assert all(d.allowed for d in decisions) and decisions[0].backend == "disabled"
