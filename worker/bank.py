"""HTTP client for the (mock) NPCI switch, translating responses into exceptions
the retry / circuit-breaker logic understands:

  BankUnavailable -> transient (timeout, connection error, 5xx): retry, counts as breaker failure
  BankDeclined    -> business outcome (declined, 4xx): never retried, NOT a breaker failure
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from common.logs import EventLogger, request_id_var
from common.metrics import Metrics


class BankUnavailable(Exception):
    pass


class BankDeclined(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class BankClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str, metrics: Metrics, log: EventLogger):
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.metrics = metrics
        self.log = log

    async def authorize(self, txn: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "txn_id": str(txn["txn_id"]),
            "payer_vpa": txn["payer_vpa"],
            "payee_vpa": txn["payee_vpa"],
            "amount_paise": txn["amount_paise"],
        }
        start = time.perf_counter()
        outcome = "error"
        try:
            try:
                resp = await self.http.post(f"{self.base_url}/npci/authorize", json=payload,
                                            headers={"x-request-id": request_id_var.get() or ""})
            except httpx.TimeoutException as exc:
                outcome = "timeout"
                raise BankUnavailable("bank switch timed out") from exc
            except httpx.HTTPError as exc:
                outcome = "unreachable"
                raise BankUnavailable(f"bank switch unreachable: {type(exc).__name__}") from exc

            if resp.status_code >= 500:
                outcome = f"http_{resp.status_code}"
                raise BankUnavailable(f"bank switch answered HTTP {resp.status_code}")
            if resp.status_code >= 400:
                outcome = f"http_{resp.status_code}"
                raise BankDeclined(f"BANK_REJECTED_{resp.status_code}")
            body = resp.json()
            if body.get("result") != "APPROVED":
                outcome = "declined"
                raise BankDeclined(str(body.get("reason", "DECLINED")))
            outcome = "approved"
            return body
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            self.metrics.inc("bank_calls_total", outcome=outcome)
            self.metrics.observe("bank_call_ms", elapsed_ms)
            self.log.info("bank.call", txn_id=payload["txn_id"], outcome=outcome, latency_ms=round(elapsed_ms, 2))
