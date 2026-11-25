"""Mock NPCI switch / issuer bank.

In real UPI the PSP app's backend sends the payment to NPCI, which routes the
debit to the payer's bank and the credit to the payee's bank. This service
stands in for that whole external hop: it adds realistic latency, occasionally
fails, and can be switched into failure modes at runtime to demonstrate the
worker's retries and circuit breaker.

POST /npci/authorize   -> 200 {"result": "APPROVED", "bank_ref": ...}
                       -> 200 {"result": "DECLINED", "reason": ...}   (business decline, no retry)
                       -> 503                                          (switch unavailable -> retry)
GET  /admin/chaos      current failure mode
POST /admin/chaos      {"mode": "normal" | "flaky" | "slow" | "down"}
                       or fine-grained {"failure_rate": 0.3, "latency_ms": [100, 300]}

Demo trick: any amount ending in .13 (e.g. Rs 1.13) is always declined.
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from common.env import env_str
from common.health import HealthRegistry
from common.logs import get_logger, node_id, setup_logging
from common.metrics import Metrics
from common.web import instrument

SERVICE = "bank-mock"
setup_logging(SERVICE)
log = get_logger("bank")
NODE = node_id()
metrics = Metrics(SERVICE, NODE)
health = HealthRegistry(SERVICE, NODE)

MODES: dict[str, dict[str, Any]] = {
    "normal": {"failure_rate": 0.01, "decline_rate": 0.005, "latency_ms": [20, 60]},
    "flaky":  {"failure_rate": 0.50, "decline_rate": 0.005, "latency_ms": [20, 80]},
    "slow":   {"failure_rate": 0.00, "decline_rate": 0.0,   "latency_ms": [1500, 2500]},
    "down":   {"failure_rate": 1.00, "decline_rate": 0.0,   "latency_ms": [1, 5]},
}
chaos: dict[str, Any] = {"mode": env_str("BANK_MODE", "normal"), **MODES[env_str("BANK_MODE", "normal")]}

app = FastAPI(title="Mock NPCI Switch", version="1.0.0")
health.add_info("chaos", lambda: chaos)
instrument(app, metrics, log, health)


class AuthorizeRequest(BaseModel):
    txn_id: str
    payer_vpa: str
    payee_vpa: str
    amount_paise: int = Field(..., gt=0)


class ChaosRequest(BaseModel):
    mode: str | None = None
    failure_rate: float | None = Field(None, ge=0, le=1)
    decline_rate: float | None = Field(None, ge=0, le=1)
    latency_ms: tuple[float, float] | None = None


@app.post("/npci/authorize")
async def authorize(req: AuthorizeRequest) -> JSONResponse:
    latency = random.uniform(*chaos["latency_ms"])
    await asyncio.sleep(latency / 1000)

    if random.random() < chaos["failure_rate"]:
        metrics.inc("npci_requests_total", result="unavailable")
        log.warning("npci.unavailable", txn_id=req.txn_id, mode=chaos["mode"], latency_ms=round(latency, 1))
        return JSONResponse({"error": "SWITCH_UNAVAILABLE", "message": "NPCI switch temporarily unavailable"},
                            status_code=503)

    if req.amount_paise % 100 == 13 or random.random() < chaos["decline_rate"]:
        metrics.inc("npci_requests_total", result="declined")
        log.info("npci.declined", txn_id=req.txn_id, latency_ms=round(latency, 1))
        return JSONResponse({"result": "DECLINED", "reason": "DECLINED_BY_REMITTER_BANK"})

    bank_ref = f"NPCI{int(time.time() * 1000) % 10**10:010d}{random.randint(10, 99)}"
    metrics.inc("npci_requests_total", result="approved")
    metrics.observe("npci_latency_ms", latency)
    log.info("npci.approved", txn_id=req.txn_id, bank_ref=bank_ref, latency_ms=round(latency, 1))
    return JSONResponse({"result": "APPROVED", "bank_ref": bank_ref, "latency_ms": round(latency, 1)})


@app.get("/admin/chaos")
async def get_chaos() -> dict[str, Any]:
    return {**chaos, "available_modes": MODES}


@app.post("/admin/chaos")
async def set_chaos(req: ChaosRequest) -> JSONResponse:
    if req.mode:
        if req.mode not in MODES:
            return JSONResponse({"error": "UNKNOWN_MODE", "available": list(MODES)}, status_code=400)
        chaos.clear()
        chaos.update({"mode": req.mode, **MODES[req.mode]})
    for field in ("failure_rate", "decline_rate"):
        value = getattr(req, field)
        if value is not None:
            chaos[field] = value
            chaos["mode"] = "custom" if not req.mode else req.mode
    if req.latency_ms is not None:
        chaos["latency_ms"] = list(req.latency_ms)
    log.warning("chaos.changed", **chaos)
    return JSONResponse(chaos)
