"""Control-flow tests for worker/processor.py with in-memory fakes for
Postgres, Redis and the bank switch (no infrastructure needed)."""
import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("httpx")

from common.keys import PAYMENTS_DLQ, balance_key  # noqa: E402
from common.logs import get_logger  # noqa: E402
from common.metrics import Metrics  # noqa: E402
from common.models import INSERT_EVENT_SQL  # noqa: E402
from common.resilience import CircuitBreaker  # noqa: E402
from worker import sql  # noqa: E402
from worker.bank import BankDeclined, BankUnavailable  # noqa: E402
from worker.processor import PaymentProcessor, WorkerSettings  # noqa: E402

NODE = "worker-test"
PAYER, PAYEE = "alice@okaxis", "bob@oksbi"


class FakeDB:
    def __init__(self, status="PENDING", attempts=0, balance=1_000_000, amount=15_000, expired=False):
        self.txn_id = uuid.uuid4()
        self.status, self.attempts, self.balance, self.amount, self.expired = status, attempts, balance, amount, expired
        self.reason = None
        self.ledger, self.events = [], []

    def _row(self):
        return {"txn_id": self.txn_id, "payer_vpa": PAYER, "payee_vpa": PAYEE, "amount_paise": self.amount,
                "attempts": self.attempts, "request_id": "req-1", "created_at": datetime.now(timezone.utc),
                "expired": self.expired}

    async def fetchrow(self, query, *args):
        if query == sql.CLAIM:
            if self.status == "PENDING":
                self.status = "PROCESSING"
                return self._row()
            return None
        if query == sql.MARK_FAILED:
            if self.status in ("PENDING", "PROCESSING"):
                self.status, self.reason = "FAILED", args[1]
                self.attempts += args[2]
                return {"created_at": None, "completed_at": None}
            return None
        if query == sql.LOCK_TXN:
            return {"status": self.status, "locked_by": NODE}
        if query == sql.MARK_SUCCESS:
            self.status = "SUCCESS"
            self.attempts += 1
            now = datetime.now(timezone.utc)
            return {"created_at": now - timedelta(milliseconds=250), "completed_at": now}
        raise AssertionError(f"unexpected fetchrow: {query[:60]}")

    async def fetchval(self, query, *args):
        if query == sql.PAYER_BALANCE:
            return self.balance
        if query == sql.RELEASE:
            self.status, self.reason = "PENDING", args[1]
            self.attempts += args[2]
            return self.attempts
        if query == sql.DEBIT:
            self.balance -= args[1]
            return self.balance
        if query == sql.CREDIT:
            return args[1]
        raise AssertionError(f"unexpected fetchval: {query[:60]}")

    async def fetch(self, query, *args):
        assert query == sql.LOCK_ACCOUNTS
        return [{"vpa": PAYER, "account_id": 1, "balance_paise": self.balance},
                {"vpa": PAYEE, "account_id": 2, "balance_paise": 0}]

    async def execute(self, query, *args):
        if query == sql.INSERT_LEDGER:
            self.ledger.append(args[2])
        elif query == INSERT_EVENT_SQL:
            self.events.append((args[1], args[2]))
        else:
            raise AssertionError(f"unexpected execute: {query[:60]}")

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield


class FakeRedis:
    def __init__(self):
        self.acked, self.added, self.deleted = [], [], []

    async def xack(self, stream, group, msg_id):
        self.acked.append(msg_id)

    async def xadd(self, stream, fields, **_):
        self.added.append((stream, fields))

    async def delete(self, *keys):
        self.deleted.extend(keys)


class FakeBank:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    async def authorize(self, txn):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if outcome == "unavailable":
            raise BankUnavailable("503")
        if outcome == "declined":
            raise BankDeclined("DECLINED_BY_REMITTER_BANK")
        return {"result": "APPROVED", "bank_ref": "NPCI123"}


def make(db, bank, breaker=None, retries=2, max_attempts=3):
    breaker = breaker or CircuitBreaker("npci", failure_threshold=50,
                                        is_failure=lambda e: isinstance(e, BankUnavailable))
    settings = WorkerSettings(node=NODE, bank_retries=retries, backoff_base_s=0.001, backoff_max_s=0.002,
                              max_delivery_attempts=max_attempts)
    redis = FakeRedis()
    return PaymentProcessor(db, redis, bank, breaker, settings, Metrics("worker", NODE), get_logger("t")), redis


def handle(proc, db, msg_id="1-0"):
    asyncio.run(proc.handle(msg_id, {"txn_id": str(db.txn_id), "request_id": "req-1"}, "c0"))


def test_success_moves_money_acks_and_invalidates_cache():
    db, bank = FakeDB(), FakeBank("ok")
    proc, redis = make(db, bank)
    handle(proc, db)
    assert db.status == "SUCCESS" and db.ledger == ["D", "C"] and db.balance == 1_000_000 - 15_000
    assert redis.acked == ["1-0"]
    assert balance_key(PAYER) in redis.deleted and balance_key(PAYEE) in redis.deleted
    assert ("PROCESSING", "SUCCESS") in db.events


def test_transient_bank_errors_are_retried_then_succeed():
    db, bank = FakeDB(), FakeBank("unavailable", "unavailable", "ok")
    proc, redis = make(db, bank, retries=2)
    handle(proc, db)
    assert bank.calls == 3 and db.status == "SUCCESS" and redis.acked == ["1-0"]


def test_bank_decline_fails_without_retry():
    db, bank = FakeDB(), FakeBank("declined")
    proc, redis = make(db, bank)
    handle(proc, db)
    assert bank.calls == 1 and db.status == "FAILED" and db.reason == "DECLINED_BY_REMITTER_BANK"
    assert db.ledger == [] and redis.acked == ["1-0"]


def test_bank_down_defers_without_ack_for_redelivery():
    db, bank = FakeDB(), FakeBank(*["unavailable"] * 10)
    proc, redis = make(db, bank, retries=2)
    handle(proc, db)
    assert bank.calls == 3                      # 1 try + 2 retries
    assert db.status == "PENDING" and db.attempts == 1 and db.reason == "BANK_UNAVAILABLE"
    assert redis.acked == []                    # stays in the PEL -> XAUTOCLAIM re-delivers it


def test_last_attempt_fails_and_dead_letters():
    db, bank = FakeDB(attempts=2), FakeBank(*["unavailable"] * 10)
    proc, redis = make(db, bank, retries=1, max_attempts=3)
    handle(proc, db)
    assert db.status == "FAILED" and db.reason == "BANK_UNAVAILABLE"
    assert redis.acked == ["1-0"] and redis.added[0][0] == PAYMENTS_DLQ


def test_open_circuit_skips_the_bank_and_defers():
    db, bank = FakeDB(), FakeBank("ok")
    breaker = CircuitBreaker("npci", failure_threshold=1, reset_timeout_s=60)
    breaker.record_failure()  # -> OPEN
    proc, redis = make(db, bank, breaker=breaker)
    handle(proc, db)
    assert bank.calls == 0 and db.status == "PENDING" and db.reason == "CIRCUIT_OPEN"
    assert db.attempts == 0 and redis.acked == []


def test_breaker_opens_during_retries_and_stops_them():
    db, bank = FakeDB(), FakeBank(*["unavailable"] * 10)
    breaker = CircuitBreaker("npci", failure_threshold=2, reset_timeout_s=60,
                             is_failure=lambda e: isinstance(e, BankUnavailable))
    proc, redis = make(db, bank, breaker=breaker, retries=5)
    handle(proc, db)
    assert bank.calls == 2                      # third attempt hits the open circuit instead of the bank
    assert db.status == "PENDING" and db.reason == "CIRCUIT_OPEN"


def test_duplicate_delivery_is_acked_and_ignored():
    db, bank = FakeDB(status="SUCCESS"), FakeBank("ok")
    proc, redis = make(db, bank)
    handle(proc, db)
    assert bank.calls == 0 and redis.acked == ["1-0"] and db.ledger == []


def test_insufficient_funds_fails_before_calling_bank():
    db, bank = FakeDB(balance=100), FakeBank("ok")
    proc, redis = make(db, bank)
    handle(proc, db)
    assert bank.calls == 0 and db.status == "FAILED" and db.reason == "INSUFFICIENT_FUNDS"


def test_expired_transaction_times_out():
    db, bank = FakeDB(expired=True), FakeBank("ok")
    proc, redis = make(db, bank)
    handle(proc, db)
    assert bank.calls == 0 and db.status == "FAILED" and db.reason == "TIMEOUT"
    assert redis.added and redis.added[0][1]["reason"] == "TIMEOUT"


def test_malformed_message_is_dead_lettered():
    proc, redis = make(FakeDB(), FakeBank())
    asyncio.run(proc.handle("9-0", {"oops": "x"}, "c0"))
    assert redis.acked == ["9-0"] and redis.added[0][1]["reason"] == "MALFORMED_MESSAGE"
