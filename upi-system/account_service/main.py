"""Account service - read-heavy endpoints, all served cache-aside from Redis.

GET /vpa/{vpa}                    verify payee name before paying (hot, rarely changes -> 5 min TTL)
GET /accounts/{vpa}/balance       balance check (30 s TTL, invalidated by the worker on every payment)
GET /accounts/{vpa}/transactions  recent history (15 s TTL, invalidated by the worker)

Every response carries an X-Cache: HIT | MISS | BYPASS header so the effect of
the cache is visible in a demo, and db_queries_total / cache_requests_total
metrics quantify it in the load-test report.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from account_service import sql
from common.cache import Cache
from common.clients import create_pg_pool, create_redis
from common.env import env_bool, env_float, env_int, env_str
from common.health import HealthRegistry
from common.keys import balance_key, history_key, vpa_key
from common.logs import get_logger, node_id, request_id_var, setup_logging
from common.metrics import Metrics
from common.models import VPA_LOOKUP_SQL, is_valid_vpa, normalize_vpa, rupees, serialize_txn, serialize_vpa
from common.web import instrument

SERVICE = "account-service"
setup_logging(SERVICE)
log = get_logger("account")
NODE = node_id()
metrics = Metrics(SERVICE, NODE)
health = HealthRegistry(SERVICE, NODE)

DATABASE_URL = env_str("DATABASE_URL", "postgresql://upi:upi_dev_password@postgres:5432/upi")
REDIS_URL = env_str("REDIS_URL", "redis://redis:6379/0")
CACHE_ENABLED = env_bool("CACHE_ENABLED", True)
TTL_VPA_S = env_float("CACHE_TTL_VPA_S", 300)
TTL_BALANCE_S = env_float("CACHE_TTL_BALANCE_S", 30)
TTL_HISTORY_S = env_float("CACHE_TTL_HISTORY_S", 15)
HISTORY_DEFAULT_LIMIT = 10


class _State:
    pool: Any
    redis: Any
    cache: Cache


state = _State()


@asynccontextmanager
async def lifespan(_: FastAPI):
    state.pool = await create_pg_pool(DATABASE_URL, log, min_size=2, max_size=env_int("DB_POOL_MAX", 10))
    state.redis = create_redis(REDIS_URL)
    state.cache = Cache(state.redis, metrics, log, enabled=CACHE_ENABLED)
    log.info("service.started", cache_enabled=CACHE_ENABLED)
    try:
        yield
    finally:
        await state.pool.close()
        await state.redis.aclose()


app = FastAPI(title="UPI Account Service", version="1.0.0", lifespan=lifespan)


async def _pg_probe() -> dict[str, Any]:
    await state.pool.fetchval("SELECT 1")
    return {"pool_size": state.pool.get_size(), "pool_idle": state.pool.get_idle_size()}


async def _redis_probe() -> None:
    await state.redis.ping()


health.add_check("postgres", _pg_probe, critical=True)
health.add_check("redis", _redis_probe, critical=False)  # cache only: degrade to DB reads
health.add_info("config", lambda: {"cache_enabled": CACHE_ENABLED, "ttl_s": {
    "vpa": TTL_VPA_S, "balance": TTL_BALANCE_S, "history": TTL_HISTORY_S}})
instrument(app, metrics, log, health)


def _validated(vpa: str) -> str:
    vpa = normalize_vpa(vpa)
    if not is_valid_vpa(vpa):
        raise HTTPException(400, {"error": "INVALID_VPA", "message": f"'{vpa}' is not a valid VPA",
                                  "request_id": request_id_var.get()})
    return vpa


def _not_found(vpa: str) -> HTTPException:
    return HTTPException(404, {"error": "VPA_NOT_FOUND", "message": f"{vpa} is not registered",
                               "request_id": request_id_var.get()})


async def _db(query_name: str, fn: Callable[[], Awaitable[Any]]) -> Any:
    start = time.perf_counter()
    try:
        return await fn()
    finally:
        metrics.inc("db_queries_total", query=query_name)
        metrics.observe("db_query_ms", (time.perf_counter() - start) * 1000, query=query_name)


def _cached_response(body: Any, cache_status: str) -> JSONResponse:
    return JSONResponse(body, headers={"X-Cache": cache_status})


@app.get("/vpa/{vpa}", summary="Resolve a VPA to the registered name (payee verification)")
async def resolve_vpa(vpa: str) -> JSONResponse:
    vpa = _validated(vpa)

    async def load() -> dict[str, Any] | None:
        row = await _db("vpa_lookup", lambda: state.pool.fetchrow(VPA_LOOKUP_SQL, vpa))
        return serialize_vpa(row) if row else None

    body, cache_status = await state.cache.get_or_load("vpa", vpa_key(vpa), TTL_VPA_S, load)
    if body is None:
        raise _not_found(vpa)
    return _cached_response(body, cache_status)


@app.get("/accounts/{vpa}/balance", summary="Account balance")
async def balance(vpa: str) -> JSONResponse:
    vpa = _validated(vpa)

    async def load() -> dict[str, Any] | None:
        row = await _db("balance", lambda: state.pool.fetchrow(sql.BALANCE, vpa))
        if row is None:
            return None
        return {
            "vpa": row["vpa"],
            "bank": row["bank_name"],
            "account": f"XXXX{row['account_last4']}",
            "balance": rupees(row["balance_paise"]),
            "balance_paise": row["balance_paise"],
            "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    body, cache_status = await state.cache.get_or_load("balance", balance_key(vpa), TTL_BALANCE_S, load)
    if body is None:
        raise _not_found(vpa)
    return _cached_response(body, cache_status)


@app.get("/accounts/{vpa}/transactions", summary="Recent transactions (latest first)")
async def history(vpa: str, limit: int = Query(HISTORY_DEFAULT_LIMIT, ge=1, le=50)) -> JSONResponse:
    vpa = _validated(vpa)

    async def load() -> dict[str, Any]:
        rows = await _db("history", lambda: state.pool.fetch(sql.HISTORY, vpa, limit))
        items = []
        for row in rows:
            item = serialize_txn(row)
            item["direction"] = "DEBIT" if row["payer_vpa"] == vpa else "CREDIT"
            items.append(item)
        return {"vpa": vpa, "count": len(items), "transactions": items}

    if limit != HISTORY_DEFAULT_LIMIT:  # only the default first page is cached
        return _cached_response(await load(), "BYPASS")
    body, cache_status = await state.cache.get_or_load("history", history_key(vpa), TTL_HISTORY_S, load)
    return _cached_response(body, cache_status)


@app.get("/", include_in_schema=False)
async def index() -> dict[str, Any]:
    return {"service": SERVICE, "node": NODE, "docs": "/docs"}
