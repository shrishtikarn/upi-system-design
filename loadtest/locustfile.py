"""Locust load test for the UPI system.

User types
----------
UpiAppUser      a normal person using the app: checks balance, verifies a
                payee, sends money, polls the status, looks at history.
                Each simulated phone has its own X-Client-ID, so a normal user
                stays far below the per-client rate limit.
AbusiveClient   2 misbehaving clients (fixed_count) hammering the balance API
                ~50 req/s each from ONE client id -> the per-client token
                bucket must reject most of their traffic with 429.

Load shapes
-----------
default         constant users given with -u / -r / -t
LOAD_SHAPE=spike
                BASE_USERS for 30 s -> jump to SPIKE_USERS for 60 s -> back
                to BASE_USERS for 30 s (total 2 minutes). This is the "traffic
                spike" the global token bucket protects the backend from.

429 responses are reported as failures named "429 RATE_LIMITED" so they are
counted separately from real errors in Locust's failure table.
"""
from __future__ import annotations

import os
import random
import uuid

from locust import FastHttpUser, LoadTestShape, between, constant, task

SEED_USERS = int(os.getenv("SEED_USERS", "1000"))
BASE_USERS = int(os.getenv("BASE_USERS", "60"))
SPIKE_USERS = int(os.getenv("SPIKE_USERS", "600"))


def random_vpa() -> str:
    return f"user{random.randint(1, SEED_USERS):04d}@mockbank"


def _check(resp, expected: tuple[int, ...] = (200,)) -> None:
    if resp.status_code == 429:
        resp.failure("429 RATE_LIMITED")
    elif resp.status_code in expected:
        resp.success()
    else:
        resp.failure(f"HTTP {resp.status_code}")


class UpiAppUser(FastHttpUser):
    weight = 10
    wait_time = between(0.5, 1.5)

    def on_start(self) -> None:
        self.client_id = f"phone-{uuid.uuid4().hex[:12]}"
        self.vpa = random_vpa()
        self.recent_txns: list[str] = []

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"X-Client-ID": self.client_id, **extra}

    @task(8)
    def check_balance(self) -> None:
        with self.client.get(f"/api/v1/accounts/{self.vpa}/balance", headers=self._headers(),
                             name="GET /accounts/[vpa]/balance", catch_response=True) as resp:
            _check(resp)

    @task(5)
    def verify_payee(self) -> None:
        with self.client.get(f"/api/v1/vpa/{random_vpa()}", headers=self._headers(),
                             name="GET /vpa/[vpa]", catch_response=True) as resp:
            _check(resp)

    @task(4)
    def send_money(self) -> None:
        payee = random_vpa()
        while payee == self.vpa:
            payee = random_vpa()
        body = {"payer_vpa": self.vpa, "payee_vpa": payee,
                "amount": f"{random.randint(1, 500)}.{random.choice(['00', '50', '99'])}", "note": "load test"}
        headers = self._headers(**{"Idempotency-Key": uuid.uuid4().hex, "Content-Type": "application/json"})
        with self.client.post("/api/v1/payments", json=body, headers=headers,
                              name="POST /payments", catch_response=True) as resp:
            _check(resp, expected=(202,))
            if resp.status_code == 202:
                try:
                    self.recent_txns = (self.recent_txns + [resp.json()["txn_id"]])[-5:]
                except Exception:  # noqa: BLE001
                    pass

    @task(3)
    def poll_status(self) -> None:
        if not self.recent_txns:
            return
        txn_id = random.choice(self.recent_txns)
        with self.client.get(f"/api/v1/payments/{txn_id}", headers=self._headers(),
                             name="GET /payments/[txn_id]", catch_response=True) as resp:
            _check(resp)

    @task(1)
    def history(self) -> None:
        with self.client.get(f"/api/v1/accounts/{self.vpa}/transactions", headers=self._headers(),
                             name="GET /accounts/[vpa]/transactions", catch_response=True) as resp:
            _check(resp)


class AbusiveClient(FastHttpUser):
    """A buggy app / script stuck in a retry loop, all from one device id."""

    fixed_count = 2
    wait_time = constant(0.02)

    def on_start(self) -> None:
        self.client_id = f"abusive-bot-{uuid.uuid4().hex[:4]}"
        self.vpa = random_vpa()

    @task
    def hammer_balance(self) -> None:
        with self.client.get(f"/api/v1/accounts/{self.vpa}/balance", headers={"X-Client-ID": self.client_id},
                             name="GET /accounts/[vpa]/balance (abusive client)", catch_response=True) as resp:
            _check(resp)


if os.getenv("LOAD_SHAPE", "").lower() == "spike":

    class SpikeShape(LoadTestShape):
        """(end_time_s, users, spawn_rate)"""

        stages = [
            (30, BASE_USERS, 20),
            (90, SPIKE_USERS, 100),
            (120, BASE_USERS, 100),
        ]

        def tick(self):
            run_time = self.get_run_time()
            for end, users, rate in self.stages:
                if run_time < end:
                    return users, rate
            return None
