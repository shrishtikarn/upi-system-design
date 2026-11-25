"""Payment worker node - consumes the Redis Stream and processes payments.

Each worker container runs:
  * WORKER_CONCURRENCY consumer tasks  XREADGROUP ... STREAMS upi:payments:stream >
  * 1 reclaimer task                   XAUTOCLAIM messages idle > RECLAIM_IDLE_MS
                                       (crashed consumers, deferred retries)
  * 1 sweeper task                     re-enqueues PENDING payments whose queue
                                       message was never published (Redis was down)
  * 1 stats task                       queue length / pending / lag -> metrics
  * a small HTTP server                GET /health, GET /metrics

All worker containers join the same consumer group, so Redis load-balances
messages across them: add capacity with more containers, no code changes.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI
from redis.exceptions import ResponseError

from common.clients import create_pg_pool, create_redis
from common.env import env_float, env_int, env_str
from common.health import HealthRegistry
from common.keys import PAYMENTS_DLQ, PAYMENTS_GROUP, PAYMENTS_STREAM, SWEEPER_LOCK
from common.logs import get_logger, node_id, setup_logging
from common.metrics import Metrics
from common.resilience import CircuitBreaker, CircuitState
from common.web import instrument
from worker import sql
from worker.bank import BankClient, BankUnavailable
from worker.processor import PaymentProcessor, WorkerSettings

SERVICE = "worker"
setup_logging(SERVICE)
log = get_logger("worker")
NODE = node_id()
metrics = Metrics(SERVICE, NODE)
health = HealthRegistry(SERVICE, NODE)

DATABASE_URL = env_str("DATABASE_URL", "postgresql://upi:upi_dev_password@postgres:5432/upi")
REDIS_URL = env_str("REDIS_URL", "redis://redis:6379/0")
BANK_URL = env_str("BANK_URL", "http://bank-mock:8000")
CONCURRENCY = env_int("WORKER_CONCURRENCY", 8)
BLOCK_MS = env_int("STREAM_BLOCK_MS", 1000)
BANK_TIMEOUT_S = env_float("BANK_TIMEOUT_S", 1.0)
RECLAIM_INTERVAL_S = env_float("RECLAIM_INTERVAL_S", 5)
SWEEP_INTERVAL_S = env_float("SWEEP_INTERVAL_S", 30)
STALE_AFTER_S = env_float("STALE_AFTER_S", 60)
STREAM_MAXLEN = env_int("STREAM_MAXLEN", 100_000)

SETTINGS = WorkerSettings(
    node=NODE,
    bank_retries=env_int("BANK_RETRIES", 3),
    backoff_base_s=env_float("BACKOFF_BASE_S", 0.2),
    backoff_max_s=env_float("BACKOFF_MAX_S", 2.0),
    max_delivery_attempts=env_int("MAX_DELIVERY_ATTEMPTS", 3),
    lease_timeout_s=env_float("LEASE_TIMEOUT_S", 30),
    txn_timeout_s=env_float("TXN_TIMEOUT_S", 120),
    reclaim_idle_ms=env_int("RECLAIM_IDLE_MS", 15_000),
)
_STATE_GAUGE = {CircuitState.CLOSED: 0, CircuitState.HALF_OPEN: 1, CircuitState.OPEN: 2}


class _State:
    pool: Any
    redis: Any
    http: httpx.AsyncClient
    breaker: CircuitBreaker
    processor: PaymentProcessor
    tasks: list[asyncio.Task]
    heartbeats: dict[str, float]
    queue: dict[str, Any]


state = _State()
state.heartbeats = {}
state.queue = {}


def _on_breaker_change(name: str, old: CircuitState, new: CircuitState) -> None:
    log.warning("circuit.state_changed", circuit=name, previous=old.value, current=new.value)
    metrics.inc("circuit_transitions_total", circuit=name, to=new.value)
    metrics.set_gauge("circuit_state", _STATE_GAUGE[new], circuit=name)


async def ensure_group() -> None:
    while True:
        try:
            await state.redis.xgroup_create(PAYMENTS_STREAM, PAYMENTS_GROUP, id="0", mkstream=True)
            log.info("queue.group_created", stream=PAYMENTS_STREAM, group=PAYMENTS_GROUP)
            return
        except ResponseError as exc:
            if "BUSYGROUP" in str(exc):  # already exists - fine
                return
            raise
        except Exception as exc:  # noqa: BLE001 - Redis not up yet
            log.warning("queue.redis_not_ready", error=repr(exc))
            await asyncio.sleep(1)


async def consumer_loop(index: int) -> None:
    consumer = f"{NODE}-c{index}"
    while True:
        try:
            response = await state.redis.xreadgroup(
                PAYMENTS_GROUP, consumer, {PAYMENTS_STREAM: ">"}, count=1, block=BLOCK_MS
            )
            state.heartbeats[consumer] = time.time()
            for _stream, messages in response or []:
                for msg_id, fields in messages:
                    await state.processor.handle(msg_id, fields, consumer)
        except asyncio.CancelledError:
            raise
        except ResponseError as exc:
            if "NOGROUP" in str(exc):  # stream/group deleted (e.g. FLUSHALL) -> recreate
                await ensure_group()
            else:
                log.error("worker.consumer_error", consumer=consumer, error=repr(exc))
                await asyncio.sleep(1)
        except Exception as exc:  # noqa: BLE001
            log.error("worker.consumer_error", consumer=consumer, error=repr(exc))
            await asyncio.sleep(1)


async def reclaimer_loop() -> None:
    consumer = f"{NODE}-reclaimer"
    while True:
        await asyncio.sleep(RECLAIM_INTERVAL_S)
        try:
            result = await state.redis.xautoclaim(
                PAYMENTS_STREAM, PAYMENTS_GROUP, consumer,
                min_idle_time=SETTINGS.reclaim_idle_ms, start_id="0-0", count=50,
            )
            messages = [(mid, fields) for mid, fields in result[1] if mid and fields]
            if messages:
                metrics.inc("messages_reclaimed_total", value=len(messages))
                log.info("queue.reclaimed", count=len(messages), min_idle_ms=SETTINGS.reclaim_idle_ms)
                await asyncio.gather(*(state.processor.handle(mid, f, consumer, reclaimed=True) for mid, f in messages))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("worker.reclaimer_error", error=repr(exc))


async def sweeper_loop() -> None:
    """Safety net for the dual-write problem (DB insert succeeded, XADD failed)."""
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_S)
        try:
            if not await state.redis.set(SWEEPER_LOCK, NODE, nx=True, ex=max(1, int(SWEEP_INTERVAL_S))):
                continue  # another worker node is sweeping this round
            rows = await state.pool.fetch(sql.STALE_UNFINISHED, float(STALE_AFTER_S), 100)
            for row in rows:
                await state.redis.xadd(
                    PAYMENTS_STREAM,
                    {"txn_id": str(row["txn_id"]), "request_id": row["request_id"] or "",
                     "enqueued_at_ms": str(int(time.time() * 1000)), "source": "sweeper"},
                    maxlen=STREAM_MAXLEN, approximate=True,
                )
            if rows:
                metrics.inc("sweeper_requeued_total", value=len(rows))
                log.warning("sweeper.requeued", count=len(rows), stale_after_s=STALE_AFTER_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("worker.sweeper_error", error=repr(exc))


async def queue_stats_loop() -> None:
    while True:
        try:
            length = await state.redis.xlen(PAYMENTS_STREAM)
            groups = await state.redis.xinfo_groups(PAYMENTS_STREAM)
            group = next((g for g in groups if g.get("name") == PAYMENTS_GROUP), {})
            dlq = await state.redis.xlen(PAYMENTS_DLQ)
            state.queue = {
                "stream": PAYMENTS_STREAM,
                "length": length,
                "pending": group.get("pending", 0),     # delivered, not yet ACKed
                "lag": group.get("lag") or 0,           # not yet delivered to any consumer
                "consumers": group.get("consumers", 0),
                "dead_letters": dlq,
                "sampled_at": time.time(),
            }
            metrics.set_gauge("queue_length", length)
            metrics.set_gauge("queue_pending", state.queue["pending"])
            metrics.set_gauge("queue_lag", state.queue["lag"])
            metrics.set_gauge("queue_dead_letters", dlq)
            metrics.set_gauge("circuit_state", _STATE_GAUGE[state.breaker.state], circuit=state.breaker.name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("worker.stats_error", error=repr(exc))
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(_: FastAPI):
    state.pool = await create_pg_pool(DATABASE_URL, log, min_size=2, max_size=CONCURRENCY + 5)
    # socket timeout must be longer than the XREADGROUP BLOCK time
    state.redis = create_redis(REDIS_URL, socket_timeout_s=BLOCK_MS / 1000 + 4)
    await ensure_group()
    state.http = httpx.AsyncClient(timeout=httpx.Timeout(BANK_TIMEOUT_S),
                                   limits=httpx.Limits(max_connections=CONCURRENCY * 4))
    state.breaker = CircuitBreaker(
        "npci-switch",
        failure_threshold=env_int("CB_FAILURE_THRESHOLD", 5),
        reset_timeout_s=env_float("CB_RESET_TIMEOUT_S", 10),
        is_failure=lambda exc: isinstance(exc, BankUnavailable),
        on_state_change=_on_breaker_change,
    )
    bank = BankClient(state.http, BANK_URL, metrics, log)
    state.processor = PaymentProcessor(state.pool, state.redis, bank, state.breaker, SETTINGS, metrics, log)
    state.tasks = [asyncio.create_task(consumer_loop(i)) for i in range(CONCURRENCY)]
    state.tasks += [asyncio.create_task(t()) for t in (reclaimer_loop, sweeper_loop, queue_stats_loop)]
    log.info("worker.started", concurrency=CONCURRENCY, settings=SETTINGS.__dict__)
    try:
        yield
    finally:
        for task in state.tasks:
            task.cancel()
        await asyncio.gather(*state.tasks, return_exceptions=True)  # un-ACKed messages get re-delivered
        await state.http.aclose()
        await state.pool.close()
        await state.redis.aclose()


app = FastAPI(title="UPI Payment Worker", version="1.0.0", lifespan=lifespan)


async def _pg_probe() -> None:
    await state.pool.fetchval("SELECT 1")


async def _redis_probe() -> None:
    await state.redis.ping()


async def _consumers_probe() -> dict[str, Any]:
    now = time.time()
    alive = sum(1 for ts in state.heartbeats.values() if now - ts < 30)
    if alive == 0 and state.heartbeats:
        raise RuntimeError("no consumer loop has polled the stream in the last 30 s")
    return {"alive": alive, "configured": CONCURRENCY}


health.add_check("postgres", _pg_probe, critical=True)
health.add_check("redis", _redis_probe, critical=True)
health.add_check("consumers", _consumers_probe, critical=False)
health.add_info("circuit_breaker", lambda: state.breaker.describe())
health.add_info("queue", lambda: state.queue)
instrument(app, metrics, log, health)
