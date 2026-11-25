"""Fault-tolerance building blocks: Circuit Breaker + Retry with exponential backoff.

Used in two places:
  * worker/      -> calls to the (mock) NPCI/bank switch
  * gateway/     -> one breaker per upstream service node

Circuit breaker state machine
-----------------------------

        failures >= threshold                 reset_timeout elapsed
  CLOSED ----------------------> OPEN ------------------------------> HALF_OPEN
    ^                              ^                                     |
    |        trial call ok         |          trial call failed          |
    +------------------------------+-------------------------------------+

  CLOSED     : calls flow normally; consecutive failures are counted.
  OPEN       : calls fail immediately with CircuitOpenError (no load on the
               broken dependency, no threads stuck waiting on timeouts).
  HALF_OPEN  : a limited number of trial calls are let through; one success
               closes the circuit, one failure re-opens it.
"""
from __future__ import annotations

import asyncio
import random
import time
from enum import Enum
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(Exception):
    """Raised instead of calling the dependency while the circuit is open."""

    def __init__(self, name: str, retry_in_s: float):
        super().__init__(f"circuit '{name}' is OPEN; retry in {retry_in_s:.1f}s")
        self.name = name
        self.retry_in_s = retry_in_s


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        reset_timeout_s: float = 10.0,
        half_open_max_calls: int = 1,
        is_failure: Callable[[BaseException], bool] = lambda exc: True,
        on_state_change: Callable[[str, CircuitState, CircuitState], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout_s = reset_timeout_s
        self.half_open_max_calls = half_open_max_calls
        self._is_failure = is_failure
        self._on_state_change = on_state_change
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._half_open_in_flight = 0
        self.total_opens = 0
        self.total_rejections = 0

    # -- state ---------------------------------------------------------------
    @property
    def state(self) -> CircuitState:
        if self._state is CircuitState.OPEN and self._clock() - self._opened_at >= self.reset_timeout_s:
            self._transition(CircuitState.HALF_OPEN)
        return self._state

    def _transition(self, new: CircuitState) -> None:
        old = self._state
        if old is new:
            return
        self._state = new
        if new is CircuitState.OPEN:
            self._opened_at = self._clock()
            self.total_opens += 1
        if new is CircuitState.HALF_OPEN:
            self._half_open_in_flight = 0
        if new is CircuitState.CLOSED:
            self._consecutive_failures = 0
        if self._on_state_change:
            self._on_state_change(self.name, old, new)

    def allow_request(self) -> bool:
        """Non-raising check, used by the gateway's load balancer to skip nodes."""
        state = self.state
        if state is CircuitState.CLOSED:
            return True
        if state is CircuitState.HALF_OPEN:
            return self._half_open_in_flight < self.half_open_max_calls
        return False

    def _before_call(self) -> None:
        state = self.state
        if state is CircuitState.OPEN or (
            state is CircuitState.HALF_OPEN and self._half_open_in_flight >= self.half_open_max_calls
        ):
            self.total_rejections += 1
            retry_in = max(0.0, self.reset_timeout_s - (self._clock() - self._opened_at))
            raise CircuitOpenError(self.name, retry_in)
        if state is CircuitState.HALF_OPEN:
            self._half_open_in_flight += 1

    def record_success(self) -> None:
        if self._state is CircuitState.HALF_OPEN:
            self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
            self._transition(CircuitState.CLOSED)
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        if self._state is CircuitState.HALF_OPEN:
            self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
            self._transition(CircuitState.OPEN)
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._transition(CircuitState.OPEN)

    # -- usage ---------------------------------------------------------------
    async def call(self, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        self._before_call()
        try:
            result = await fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - we re-raise
            if isinstance(exc, asyncio.CancelledError):
                if self._state is CircuitState.HALF_OPEN:
                    self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                raise
            if self._is_failure(exc):
                self.record_failure()
            else:
                self.record_success()  # e.g. a business "DECLINED" means the dependency is healthy
            raise
        self.record_success()
        return result

    def describe(self) -> dict[str, Any]:
        state = self.state
        info: dict[str, Any] = {
            "name": self.name,
            "state": state.value,
            "consecutive_failures": self._consecutive_failures,
            "failure_threshold": self.failure_threshold,
            "total_opens": self.total_opens,
            "total_rejections": self.total_rejections,
        }
        if state is CircuitState.OPEN:
            info["retry_in_s"] = round(max(0.0, self.reset_timeout_s - (self._clock() - self._opened_at)), 2)
        return info


def backoff_delay(attempt: int, base_s: float, factor: float, max_s: float, jitter: bool = True) -> float:
    """Exponential backoff with "equal jitter".

    attempt=0 -> ~base, attempt=1 -> ~base*factor, ... capped at max_s.
    Equal jitter keeps half of the delay fixed (so the exponential growth is
    still visible in the logs) and randomises the other half, so a fleet of
    workers that failed at the same moment does not retry in lock-step
    (the "thundering herd" problem).
    """
    delay = min(max_s, base_s * (factor ** attempt))
    if jitter:
        delay = delay / 2 + random.uniform(0, delay / 2)
    return delay


async def retry_with_backoff(
    fn: Callable[[], Awaitable[T]],
    *,
    retries: int = 3,
    base_delay_s: float = 0.2,
    factor: float = 2.0,
    max_delay_s: float = 2.0,
    jitter: bool = True,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    give_up_on: tuple[type[BaseException], ...] = (),
    on_retry: Callable[[int, float, BaseException], None] | None = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> T:
    """Call fn(); on a retryable error wait (exponentially longer) and try again.

    retries=3 means up to 4 calls in total (1 initial + 3 retries).
    Exceptions listed in give_up_on (e.g. CircuitOpenError) are never retried.
    """
    attempt = 0
    while True:
        try:
            return await fn()
        except give_up_on:
            raise
        except retry_on as exc:
            if attempt >= retries:
                raise
            delay = backoff_delay(attempt, base_delay_s, factor, max_delay_s, jitter)
            attempt += 1
            if on_retry:
                on_retry(attempt, delay, exc)
            await sleep(delay)
