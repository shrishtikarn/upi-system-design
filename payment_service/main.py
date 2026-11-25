"""Payment service - accepts payment requests and hands them to the worker pool.

POST /payments           validate -> idempotent INSERT (status PENDING) -> XADD to
                         Redis Stream -> 202 Accepted. The slow part (talking to
                         the bank switch, moving money) happens asynchronously
                         in worker/, so this endpoint answers in milliseconds
                         even when the bank is slow.
GET  /payments/{txn_id}  status polling, served cache-aside from Redis.

Two identical replicas (payment-service-1/-2) run behind the gateway. The
service is stateless: all state lives in Postgres and Redis.
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from common.cache import Cache
from common.clients import create_pg_pool, create_redis
from common.env import env_bool, env_float, env_int, env_str
from common.health import HealthRegistry
from common.keys import PAYMENTS_STREAM, txn_key, vpa_key
from common.logs import get_logger, node_id, request_id_var, setup_logging
from common.metrics import Metrics
from common.models import (
    FINAL_STATUSES,
    INSERT_EVENT_SQL,
    VPA_LOOKUP_SQL,
    is_valid_vpa,
    normalize_vpa,
    serialize_txn,
    serialize_vpa,
)
from common.web import instrument
from payment_service import sql

SERVICE = "payment-service"
setup_logging(SERVICE)
log = get_logger("payment")
NODE = node_id()
metrics = Metrics(SERVICE, NODE)
health = HealthRegistry(SERVICE, NODE)

DATABASE_URL = env_str("DATABASE_URL", "postgresql://upi:upi_dev_password@postgres:5432/upi")
REDIS_URL = env_str("REDIS_URL", "redis://redis:6379/0")
CACHE_ENABLED = env_bool("CACHE_ENABLED", True)
TTL_TXN_FINAL_S = env_float("CACHE_TTL_TXN_FINAL_S", 300)
TTL_TXN_PENDING_S = env_float("CACHE_TTL_TXN_PENDING_S", 1)
TTL_VPA_S = env_float("CACHE_TTL_VPA_S", 300)
STREAM_MAXLEN = env_int("STREAM_MAXLEN", 100_000)
MAX_AMOUNT_RUPEES = Decimal(env_str("MAX_AMOUNT_RUPEES", "100000"))  # NPCI's per-transaction P2P limit


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


app = FastAPI(title="UPI Payment Service", version="1.0.0", lifespan=lifespan)


async def _pg_probe() -> dict[str, Any]:
    await state.pool.fetchval("SELECT 1")
    return {"pool_size": state.pool.get_size(), "pool_idle": state.pool.get_idle_size()}


async def _redis_probe() -> None:
    await state.redis.ping()


health.add_check("postgres", _pg_probe, critical=True)
# Redis is not critical here: if XADD fails the payment is still persisted and
# the worker's sweeper re-enqueues it. The node reports DEGRADED, not DOWN.
health.add_check("redis", _redis_probe, critical=False)
health.add_info("config", lambda: {"cache_enabled": CACHE_ENABLED, "stream": PAYMENTS_STREAM})
instrument(app, metrics, log, health)


# --------------------------------------------------------------------------- models
class PaymentRequest(BaseModel):
    payer_vpa: str = Field(..., examples=["alice@okaxis"])
    payee_vpa: str = Field(..., examples=["bob@oksbi"])
    amount: Decimal = Field(..., gt=0, max_digits=9, decimal_places=2, examples=["150.00"],
                            description="Amount in rupees, up to 2 decimal places")
    note: str | None = Field(None, max_length=100)

    @field_validator("payer_vpa", "payee_vpa")
    @classmethod
    def _check_vpa(cls, value: str) -> str:
        value = normalize_vpa(value)
        if not is_valid_vpa(value):
            raise ValueError("invalid VPA, expected something like name@bank")
        return value


def _problem(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message,
                                                     "request_id": request_id_var.get()})


async def _lookup_vpa(vpa: str) -> dict[str, Any] | None:
    async def load() -> dict[str, Any] | None:
        metrics.inc("db_queries_total", query="vpa_lookup")
        row = await state.pool.fetchrow(VPA_LOOKUP_SQL, vpa)
        return serialize_vpa(row) if row else None

    value, _ = await state.cache.get_or_load("vpa", vpa_key(vpa), TTL_VPA_S, load)
    return value


# --------------------------------------------------------------------------- endpoints
@app.post("/payments", status_code=202, summary="Initiate a payment (asynchronous)")
async def create_payment(
    req: PaymentRequest,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    if not idempotency_key or len(idempotency_key) > 64:
        raise _problem(400, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key header (max 64 chars) is required")
    if req.amount > MAX_AMOUNT_RUPEES:
        raise _problem(422, "AMOUNT_LIMIT_EXCEEDED", f"Maximum per transaction is Rs {MAX_AMOUNT_RUPEES}")
    if req.payer_vpa == req.payee_vpa:
        raise _problem(422, "SAME_PAYER_PAYEE", "Payer and payee must be different")

    payer = await _lookup_vpa(req.payer_vpa)
    if not payer or not payer["active"]:
        raise _problem(404, "PAYER_VPA_NOT_FOUND", f"{req.payer_vpa} is not registered")
    payee = await _lookup_vpa(req.payee_vpa)
    if not payee or not payee["active"]:
        raise _problem(404, "PAYEE_VPA_NOT_FOUND", f"{req.payee_vpa} is not registered")

    amount_paise = int(req.amount * 100)
    request_id = request_id_var.get() or ""
    txn_id = uuid.uuid4()

    async with state.pool.acquire() as conn:
        async with conn.transaction():
            metrics.inc("db_queries_total", query="insert_txn")
            row = await conn.fetchrow(sql.INSERT_TXN, txn_id, idempotency_key, req.payer_vpa, req.payee_vpa,
                                      amount_paise, req.note, request_id)
            if row is not None:
                await conn.execute(INSERT_EVENT_SQL, txn_id, None, "PENDING", NODE, request_id,
                                   json.dumps({"source": "api", "amount_paise": amount_paise}))
        if row is None:
            existing = await conn.fetchrow(sql.SELECT_TXN_BY_IDEMPOTENCY_KEY, idempotency_key)

    if row is None:
        # Same Idempotency-Key seen before: return the original transaction instead
        # of creating (and charging) a second one.
        if (existing["payer_vpa"], existing["payee_vpa"], existing["amount_paise"]) != (
            req.payer_vpa, req.payee_vpa, amount_paise
        ):
            raise _problem(409, "IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was used for a different payment")
        metrics.inc("payments_idempotent_replay_total")
        log.info("payment.idempotent_replay", txn_id=str(existing["txn_id"]), idempotency_key=idempotency_key)
        response.status_code = 200
        response.headers["Idempotent-Replay"] = "true"
        return serialize_txn(existing)

    # Hand off to the worker pool. If Redis is unavailable the payment is still
    # safely stored as PENDING and the worker's sweeper will enqueue it later.
    try:
        stream_id = await state.redis.xadd(
            PAYMENTS_STREAM,
            {"txn_id": str(txn_id), "request_id": request_id, "enqueued_at_ms": str(int(time.time() * 1000))},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
        metrics.inc("queue_publish_total", outcome="ok")
        log.info("queue.published", stream=PAYMENTS_STREAM, stream_id=stream_id, txn_id=str(txn_id))
    except Exception as exc:  # noqa: BLE001
        metrics.inc("queue_publish_total", outcome="error")
        log.error("queue.publish_failed", txn_id=str(txn_id), error=repr(exc), recovery="sweeper will re-enqueue")

    metrics.inc("payments_created_total")
    log.info("payment.created", txn_id=str(txn_id), payer_vpa=req.payer_vpa, payee_vpa=req.payee_vpa,
             amount_paise=amount_paise)
    body = serialize_txn(row)
    body["payee_name"] = payee["name"]
    body["status_url"] = f"/api/v1/payments/{txn_id}"
    return body


@app.get("/payments/{txn_id}", summary="Payment status (poll until SUCCESS or FAILED)")
async def get_payment(txn_id: uuid.UUID) -> JSONResponse:
    key = txn_key(str(txn_id))
    cached = await state.cache.get_json("txn_status", key)
    if cached is not None:
        return JSONResponse(cached, headers={"X-Cache": "HIT"})

    metrics.inc("db_queries_total", query="txn_by_id")
    row = await state.pool.fetchrow(sql.SELECT_TXN, txn_id)
    if row is None:
        raise _problem(404, "TXN_NOT_FOUND", f"No transaction {txn_id}")
    body = serialize_txn(row)
    # Final states never change -> cache for minutes. PENDING changes soon ->
    # cache for ~1 s only (absorbs aggressive polling). The worker also deletes
    # this key the moment the payment completes.
    ttl = TTL_TXN_FINAL_S if body["status"] in FINAL_STATUSES else TTL_TXN_PENDING_S
    await state.cache.set_json(key, body, ttl)
    return JSONResponse(body, headers={"X-Cache": "MISS" if CACHE_ENABLED else "BYPASS"})


@app.get("/", include_in_schema=False)
async def index() -> dict[str, Any]:
    return {"service": SERVICE, "node": NODE, "max_amount_rupees": str(MAX_AMOUNT_RUPEES), "docs": "/docs"}
