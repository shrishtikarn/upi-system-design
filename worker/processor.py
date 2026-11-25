"""Processes one payment message from the Redis Stream.

    queue message --> claim (lease) --> precheck balance --> call bank switch
                                                              (retry + backoff,
                                                               circuit breaker)
                  --> settle in ONE Postgres transaction:
                        lock rows -> debit -> credit -> 2 ledger entries
                        -> status SUCCESS -> audit event
                  --> XACK, invalidate caches

Delivery semantics: Redis Streams give *at-least-once* delivery. Combined with
the lease-based claim and the conditional status updates in worker/sql.py the
*effect* is exactly-once: money moves at most once per transaction, however
many times a message is delivered.

What happens when things fail:
  * bank returns DECLINED           -> FAILED (business outcome), ACK
  * bank 5xx / timeout              -> retried in-process with exponential backoff;
                                       if still failing -> back to PENDING, *no ACK*,
                                       XAUTOCLAIM re-delivers it after RECLAIM_IDLE_MS;
                                       after MAX_DELIVERY_ATTEMPTS -> FAILED + dead-letter
  * circuit breaker OPEN            -> do not even call the bank; back to PENDING, no ACK
  * worker crashes mid-way          -> message stays un-ACKed in the PEL, lease expires,
                                       another worker re-claims it
  * transaction older than timeout  -> FAILED "TIMEOUT" (UPI's "deemed failed")
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from common.keys import PAYMENTS_DLQ, PAYMENTS_GROUP, PAYMENTS_STREAM, balance_key, history_key, txn_key
from common.logs import EventLogger, new_request_id, request_id_var
from common.metrics import Metrics
from common.models import INSERT_EVENT_SQL
from common.resilience import CircuitBreaker, CircuitOpenError, retry_with_backoff
from worker import sql
from worker.bank import BankClient, BankDeclined, BankUnavailable


@dataclass(frozen=True)
class WorkerSettings:
    node: str
    bank_retries: int = 3
    backoff_base_s: float = 0.2
    backoff_max_s: float = 2.0
    max_delivery_attempts: int = 3
    lease_timeout_s: float = 30.0
    txn_timeout_s: float = 120.0
    reclaim_idle_ms: int = 15_000
    dlq_maxlen: int = 10_000


class PaymentProcessor:
    def __init__(self, pool: Any, redis: Any, bank: BankClient, breaker: CircuitBreaker,
                 settings: WorkerSettings, metrics: Metrics, log: EventLogger):
        self.pool = pool
        self.redis = redis
        self.bank = bank
        self.breaker = breaker
        self.s = settings
        self.metrics = metrics
        self.log = log

    # ------------------------------------------------------------------ entry
    async def handle(self, msg_id: str, fields: dict[str, str], consumer: str, reclaimed: bool = False) -> None:
        request_id = fields.get("request_id") or new_request_id()
        token = request_id_var.set(request_id)  # every log line below carries the original request id
        started = time.perf_counter()
        try:
            await self._handle(msg_id, fields, consumer, reclaimed)
        except Exception as exc:  # noqa: BLE001 - leave un-ACKed; it will be re-delivered
            self.metrics.inc("worker_errors_total")
            self.log.exception("worker.unexpected_error", stream_id=msg_id, error=repr(exc))
        finally:
            self.metrics.observe("processing_ms", (time.perf_counter() - started) * 1000)
            request_id_var.reset(token)

    async def _handle(self, msg_id: str, fields: dict[str, str], consumer: str, reclaimed: bool) -> None:
        try:
            txn_id = str(uuid.UUID(fields.get("txn_id", "")))
        except ValueError:
            await self._dead_letter(msg_id, fields, "MALFORMED_MESSAGE")
            await self._ack(msg_id)
            return

        enqueued_ms = fields.get("enqueued_at_ms")
        queue_wait_ms = time.time() * 1000 - int(enqueued_ms) if enqueued_ms and enqueued_ms.isdigit() else None
        if queue_wait_ms is not None and not reclaimed:
            self.metrics.observe("queue_wait_ms", queue_wait_ms)
        self.log.info("queue.consumed", stream_id=msg_id, txn_id=txn_id, consumer=consumer, reclaimed=reclaimed,
                      queue_wait_ms=round(queue_wait_ms, 1) if queue_wait_ms is not None else None)

        # 1) claim the transaction (lease)
        txn = await self.pool.fetchrow(sql.CLAIM, uuid.UUID(txn_id), self.s.node, float(self.s.lease_timeout_s),
                                       float(self.s.txn_timeout_s))
        if txn is None:
            self.metrics.inc("messages_skipped_total", reason="already_handled_or_locked")
            self.log.info("worker.skip", txn_id=txn_id, reason="already final or leased by another worker")
            await self._ack(msg_id)
            return
        txn = dict(txn)
        await self._event(txn_id, "PENDING", "PROCESSING", {"consumer": consumer, "reclaimed": reclaimed})

        if txn["expired"]:
            await self._fail(txn, "TIMEOUT", attempted=0)
            await self._dead_letter(msg_id, fields, "TIMEOUT")
            await self._ack(msg_id)
            return

        # 2) cheap pre-check before involving the bank
        balance = await self.pool.fetchval(sql.PAYER_BALANCE, txn["payer_vpa"])
        if balance is None or balance < txn["amount_paise"]:
            await self._fail(txn, "INSUFFICIENT_FUNDS", attempted=0)
            await self._ack(msg_id)
            return

        # 3) call the bank switch: retries with exponential backoff, guarded by the circuit breaker
        def on_retry(attempt: int, delay: float, exc: BaseException) -> None:
            self.metrics.inc("bank_retries_total")
            self.log.warning("bank.retry_scheduled", txn_id=txn_id, retry=attempt, of=self.s.bank_retries,
                             backoff_s=round(delay, 3), error=str(exc))

        try:
            result = await retry_with_backoff(
                lambda: self.breaker.call(self.bank.authorize, txn),
                retries=self.s.bank_retries,
                base_delay_s=self.s.backoff_base_s,
                max_delay_s=self.s.backoff_max_s,
                retry_on=(BankUnavailable,),
                give_up_on=(CircuitOpenError, BankDeclined),
                on_retry=on_retry,
            )
        except CircuitOpenError as exc:
            await self._defer(txn, msg_id, "CIRCUIT_OPEN", attempted=0, extra={"breaker_retry_in_s": round(exc.retry_in_s, 1)})
            return
        except BankDeclined as exc:
            await self._fail(txn, exc.reason, attempted=1)
            await self._ack(msg_id)
            return
        except BankUnavailable as exc:
            if txn["attempts"] + 1 >= self.s.max_delivery_attempts:
                await self._fail(txn, "BANK_UNAVAILABLE", attempted=1)
                await self._dead_letter(msg_id, fields, "BANK_UNAVAILABLE")
                await self._ack(msg_id)
            else:
                await self._defer(txn, msg_id, "BANK_UNAVAILABLE", attempted=1, extra={"error": str(exc)})
            return

        # 4) move the money
        outcome = await self._settle(txn, result.get("bank_ref", ""))
        await self._ack(msg_id)
        if outcome == "LOST_LEASE":
            self.log.warning("worker.lease_lost", txn_id=txn_id)

    # ------------------------------------------------------------------ steps
    async def _settle(self, txn: dict[str, Any], bank_ref: str) -> str:
        txn_id, amount = txn["txn_id"], txn["amount_paise"]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchrow(sql.LOCK_TXN, txn_id)
                if current is None or current["status"] != "PROCESSING" or current["locked_by"] != self.s.node:
                    return "LOST_LEASE"
                rows = await conn.fetch(sql.LOCK_ACCOUNTS, [txn["payer_vpa"], txn["payee_vpa"]])
                accounts = {r["vpa"]: r for r in rows}
                payer, payee = accounts[txn["payer_vpa"]], accounts[txn["payee_vpa"]]

                if payer["balance_paise"] < amount:  # re-check under the row lock
                    await conn.fetchrow(sql.MARK_FAILED, txn_id, "INSUFFICIENT_FUNDS", 1)
                    await conn.execute(INSERT_EVENT_SQL, txn_id, "PROCESSING", "FAILED", self.s.node,
                                       request_id_var.get(), json.dumps({"reason": "INSUFFICIENT_FUNDS"}))
                    result = "INSUFFICIENT_FUNDS"
                else:
                    payer_after = await conn.fetchval(sql.DEBIT, payer["account_id"], amount)
                    payee_after = await conn.fetchval(sql.CREDIT, payee["account_id"], amount)
                    await conn.execute(sql.INSERT_LEDGER, txn_id, payer["account_id"], "D", amount, payer_after)
                    await conn.execute(sql.INSERT_LEDGER, txn_id, payee["account_id"], "C", amount, payee_after)
                    times = await conn.fetchrow(sql.MARK_SUCCESS, txn_id, bank_ref)
                    await conn.execute(INSERT_EVENT_SQL, txn_id, "PROCESSING", "SUCCESS", self.s.node,
                                       request_id_var.get(), json.dumps({"bank_ref": bank_ref}))
                    result = "SUCCESS"

        self.log.info("db.committed", txn_id=str(txn_id), status=result, table_writes=(
            "bank_accounts x2, ledger_entries x2, transactions, transaction_events" if result == "SUCCESS"
            else "transactions, transaction_events"))
        await self._invalidate(txn, balance_changed=result == "SUCCESS")
        if result == "SUCCESS":
            e2e_ms = (times["completed_at"] - times["created_at"]).total_seconds() * 1000
            self.metrics.observe("txn_e2e_ms", e2e_ms)
            self.metrics.inc("payments_processed_total", status="SUCCESS", reason="OK")
            self.log.info("payment.completed", txn_id=str(txn_id), status="SUCCESS", bank_ref=bank_ref,
                          end_to_end_ms=round(e2e_ms, 1))
        else:
            self.metrics.inc("payments_processed_total", status="FAILED", reason=result)
            self.log.info("payment.completed", txn_id=str(txn_id), status="FAILED", reason=result)
        return result

    async def _fail(self, txn: dict[str, Any], reason: str, attempted: int) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(sql.MARK_FAILED, txn["txn_id"], reason, attempted)
                if row is not None:
                    await conn.execute(INSERT_EVENT_SQL, txn["txn_id"], "PROCESSING", "FAILED", self.s.node,
                                       request_id_var.get(), json.dumps({"reason": reason}))
        self.metrics.inc("payments_processed_total", status="FAILED", reason=reason)
        self.log.info("payment.completed", txn_id=str(txn["txn_id"]), status="FAILED", reason=reason)
        await self._invalidate(txn, balance_changed=False)

    async def _defer(self, txn: dict[str, Any], msg_id: str, reason: str, attempted: int,
                     extra: dict[str, Any] | None = None) -> None:
        """Hand the transaction back: status PENDING, message NOT acknowledged.

        It stays in the consumer group's Pending Entries List and XAUTOCLAIM
        re-delivers it after reclaim_idle_ms - a built-in delayed retry.
        """
        attempts = await self.pool.fetchval(sql.RELEASE, txn["txn_id"], reason, attempted, self.s.node)
        await self._event(txn["txn_id"], "PROCESSING", "PENDING", {"reason": reason, **(extra or {})})
        self.metrics.inc("payments_deferred_total", reason=reason)
        self.log.warning("payment.deferred", txn_id=str(txn["txn_id"]), stream_id=msg_id, reason=reason,
                         attempts=attempts, max_attempts=self.s.max_delivery_attempts,
                         redelivery_in_s=self.s.reclaim_idle_ms / 1000, **(extra or {}))

    async def _event(self, txn_id: Any, from_status: str, to_status: str, detail: dict[str, Any]) -> None:
        await self.pool.execute(INSERT_EVENT_SQL, txn_id, from_status, to_status, self.s.node,
                                request_id_var.get(), json.dumps(detail))

    async def _invalidate(self, txn: dict[str, Any], balance_changed: bool) -> None:
        keys = [txn_key(str(txn["txn_id"])), history_key(txn["payer_vpa"]), history_key(txn["payee_vpa"])]
        if balance_changed:
            keys += [balance_key(txn["payer_vpa"]), balance_key(txn["payee_vpa"])]
        try:
            await self.redis.delete(*keys)
            self.metrics.inc("cache_invalidations_total", value=len(keys))
            self.log.debug("cache.invalidated", keys=keys)
        except Exception as exc:  # noqa: BLE001 - stale cache expires via TTL anyway
            self.log.warning("cache.invalidate_failed", keys=keys, error=repr(exc))

    async def _ack(self, msg_id: str) -> None:
        await self.redis.xack(PAYMENTS_STREAM, PAYMENTS_GROUP, msg_id)

    async def _dead_letter(self, msg_id: str, fields: dict[str, str], reason: str) -> None:
        await self.redis.xadd(PAYMENTS_DLQ, {**fields, "reason": reason, "original_id": msg_id,
                                             "failed_at_ms": str(int(time.time() * 1000))},
                              maxlen=self.s.dlq_maxlen, approximate=True)
        self.metrics.inc("dlq_messages_total", reason=reason)
        self.log.warning("queue.dead_lettered", stream_id=msg_id, reason=reason, dlq=PAYMENTS_DLQ)
