import asyncio

import pytest

from common.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    backoff_delay,
    retry_with_backoff,
)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Transient(Exception):
    pass


class Business(Exception):
    pass


async def fail():
    raise Transient("boom")


async def ok():
    return "ok"


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- circuit breaker
def test_opens_after_threshold_consecutive_failures():
    breaker = CircuitBreaker("t", failure_threshold=3, reset_timeout_s=10, clock=FakeClock())
    for _ in range(3):
        with pytest.raises(Transient):
            run(breaker.call(fail))
    assert breaker.state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        run(breaker.call(ok))  # fails fast, the dependency is not called
    assert breaker.total_rejections == 1


def test_success_resets_the_failure_count():
    breaker = CircuitBreaker("t", failure_threshold=3, clock=FakeClock())
    for _ in range(2):
        with pytest.raises(Transient):
            run(breaker.call(fail))
    run(breaker.call(ok))
    for _ in range(2):
        with pytest.raises(Transient):
            run(breaker.call(fail))
    assert breaker.state is CircuitState.CLOSED


def test_half_open_success_closes_failure_reopens():
    clock = FakeClock()
    transitions = []
    breaker = CircuitBreaker("t", failure_threshold=1, reset_timeout_s=10, clock=clock,
                             on_state_change=lambda n, a, b: transitions.append((a.value, b.value)))
    with pytest.raises(Transient):
        run(breaker.call(fail))
    clock.t = 10.0
    assert breaker.state is CircuitState.HALF_OPEN
    with pytest.raises(Transient):
        run(breaker.call(fail))           # trial failed -> OPEN again, timer restarted
    assert breaker.state is CircuitState.OPEN
    clock.t = 15.0
    assert breaker.state is CircuitState.OPEN
    clock.t = 20.0
    assert run(breaker.call(ok)) == "ok"  # trial succeeded -> CLOSED
    assert breaker.state is CircuitState.CLOSED
    assert transitions == [("CLOSED", "OPEN"), ("OPEN", "HALF_OPEN"), ("HALF_OPEN", "OPEN"),
                           ("OPEN", "HALF_OPEN"), ("HALF_OPEN", "CLOSED")]


def test_half_open_allows_only_limited_trial_calls():
    clock = FakeClock()
    breaker = CircuitBreaker("t", failure_threshold=1, reset_timeout_s=5, half_open_max_calls=1, clock=clock)
    with pytest.raises(Transient):
        run(breaker.call(fail))
    clock.t = 5.0

    async def scenario():
        gate = asyncio.Event()

        async def slow_ok():
            await gate.wait()
            return "ok"

        trial = asyncio.create_task(breaker.call(slow_ok))
        await asyncio.sleep(0)
        with pytest.raises(CircuitOpenError):
            await breaker.call(ok)        # a second concurrent call is rejected
        gate.set()
        return await trial

    assert run(scenario()) == "ok"
    assert breaker.state is CircuitState.CLOSED


def test_business_errors_do_not_trip_the_breaker():
    breaker = CircuitBreaker("t", failure_threshold=2, is_failure=lambda e: isinstance(e, Transient), clock=FakeClock())

    async def declined():
        raise Business("declined")

    for _ in range(5):
        with pytest.raises(Business):
            run(breaker.call(declined))
    assert breaker.state is CircuitState.CLOSED


# ---------------------------------------------------------------- retry
def test_backoff_grows_exponentially_and_is_capped():
    delays = [backoff_delay(i, base_s=0.2, factor=2, max_s=2.0, jitter=False) for i in range(6)]
    assert delays == [0.2, 0.4, 0.8, 1.6, 2.0, 2.0]


def test_equal_jitter_stays_within_half_and_full_delay():
    for _ in range(200):
        d = backoff_delay(2, base_s=0.2, factor=2, max_s=2.0, jitter=True)
        assert 0.4 <= d <= 0.8


def test_retry_succeeds_after_transient_failures():
    calls, sleeps = [], []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise Transient("try again")
        return "done"

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    result = run(retry_with_backoff(flaky, retries=3, base_delay_s=0.1, jitter=False,
                                    retry_on=(Transient,), sleep=fake_sleep))
    assert result == "done" and len(calls) == 3 and sleeps == [0.1, 0.2]


def test_retry_gives_up_after_max_retries():
    calls = []

    async def always_fail():
        calls.append(1)
        raise Transient("nope")

    async def no_sleep(_):
        return None

    with pytest.raises(Transient):
        run(retry_with_backoff(always_fail, retries=3, retry_on=(Transient,), sleep=no_sleep))
    assert len(calls) == 4  # 1 attempt + 3 retries


def test_retry_stops_immediately_on_open_circuit():
    calls = []

    async def open_circuit():
        calls.append(1)
        raise CircuitOpenError("t", 5)

    with pytest.raises(CircuitOpenError):
        run(retry_with_backoff(open_circuit, retries=5, retry_on=(Exception,), give_up_on=(CircuitOpenError,)))
    assert len(calls) == 1
