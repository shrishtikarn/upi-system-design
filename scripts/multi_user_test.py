"""Multi-user scenario test for the UPI system (stdlib only).

Drives the running stack through the public gateway with many distinct users (each user
is its own "phone": its own X-Client-ID, so per-client rate limits apply per person) and
checks every outcome against what a real UPI app would expect.

    python scripts/multi_user_test.py                    # all phases, ~2-3 minutes
    python scripts/multi_user_test.py --users 100 --skip-chaos
    python scripts/multi_user_test.py --out reports/multi_user_test

Phases (each prints "=== PHASE <name> ===" so other tools can follow along):

  p2p           N users (default 100) each pay another user           -> 202, then SUCCESS
  p2m           30 users pay the merchant chaipoint@okicici            -> SUCCESS
  hot-payee     20 users pay alice@okaxis at the same moment           -> all SUCCESS, exact total credited
  overspend     bob sends 3 payments at once that together exceed his balance
                                                                       -> 2 SUCCESS + 1 INSUFFICIENT_FUNDS
  idempotency   10 users double-tap (same Idempotency-Key twice)       -> 202 + 200 replay, ONE debit
                5 users reuse a key for a different payment            -> 409 IDEMPOTENCY_KEY_REUSED
  negative      bank decline (.13), insufficient funds, amount > Rs 1 lakh, pay yourself,
                unknown payee, malformed VPA, missing Idempotency-Key  -> the documented error
  rate-limit    one user floods 30 payment requests                    -> 429 + Retry-After, other users unaffected
  chaos-flaky   20 payments while the bank fails 50% of calls          -> retries with backoff, still settle
  chaos-down    10 payments while the bank is down                     -> circuit breaker opens, payments wait
  chaos-recover bank back to normal                                    -> waiting payments finish (SUCCESS or
                                                                          FAILED BANK_UNAVAILABLE), none stuck
  verify        money in the system unchanged; every touched account's balance moved by exactly
                the sum of its SUCCESS payments; no negative balances

Writes <out>/results.json, <out>/transactions.csv and <out>/console.log.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

GATEWAY = os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").rstrip("/")
ROOT = Path(__file__).resolve().parent.parent
FINAL = ("SUCCESS", "FAILED")

if os.name == "nt":
    os.system("")
G, R, Y, C, D, B, X = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[1m", "\033[0m"

_print_lock = threading.Lock()
_console: list[str] = []


def out(line: str = "") -> None:
    with _print_lock:
        print(line, flush=True)
        _console.append(line)


# --------------------------------------------------------------------------- http
def call(method: str, path: str, body: Any = None, headers: dict[str, str] | None = None,
         timeout: float = 15) -> tuple[int, dict[str, str], Any, float]:
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"} if body is not None else {}
    hdrs.update(headers or {})
    req = urllib.request.Request(GATEWAY + path, data=data, method=method, headers=hdrs)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, status, rh = resp.read(), resp.status, resp.headers
    except urllib.error.HTTPError as exc:
        raw, status, rh = exc.read(), exc.code, exc.headers
    ms = (time.perf_counter() - t0) * 1000
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw.decode(errors="replace")
    return status, {k.lower(): v for k, v in rh.items()}, parsed, ms


def get(path: str, client: str) -> tuple[int, dict[str, str], Any, float]:
    """GET that waits politely when this client's read bucket is empty."""
    for _ in range(6):
        s, h, b, ms = call("GET", path, headers={"X-Client-ID": client})
        if s != 429:
            return s, h, b, ms
        time.sleep(float(h.get("retry-after", "1")))
    return s, h, b, ms


def error_code(body: Any) -> str | None:
    if isinstance(body, dict):
        if "error" in body:
            return body["error"]
        detail = body.get("detail")
        if isinstance(detail, dict):
            return detail.get("error")
        if isinstance(detail, list):  # FastAPI request validation
            return "VALIDATION_ERROR"
    return None


# --------------------------------------------------------------------------- cases
@dataclass
class Case:
    scenario: str
    payer: str
    payee: str
    amount: str
    note: str = ""
    expect_http: tuple[int, ...] = (202,)
    expect_final: tuple[str, ...] = ("SUCCESS",)      # empty -> rejected synchronously, no transaction
    expect_reason: tuple[str | None, ...] | None = None
    expect_error: str | None = None
    key: str | None = field(default_factory=lambda: uuid.uuid4().hex)
    send_key: bool = True
    twice: bool = False                                # double tap with the same key
    reuse_key_with_amount: str | None = None           # second request: same key, different amount
    # results
    http: int | None = None
    error: str | None = None
    txn_id: str | None = None
    request_id: str | None = None
    node: str | None = None
    post_ms: float | None = None
    final: str | None = None
    reason: str | None = None
    bank_ref: str | None = None
    attempts: int | None = None
    settle_s: float | None = None
    second_http: int | None = None
    second_txn: str | None = None
    second_error: str | None = None
    ok: bool | None = None
    why: str = ""

    def client(self) -> str:
        return f"phone-{self.payer}"


def run_case(c: Case, poll_timeout_s: float = 120) -> Case:
    body = {"payer_vpa": c.payer, "payee_vpa": c.payee, "amount": c.amount, "note": c.note or None}
    headers = {"X-Client-ID": c.client()}
    if c.send_key:
        headers["Idempotency-Key"] = c.key
    started = time.perf_counter()
    s, h, b, ms = call("POST", "/api/v1/payments", body, headers)
    c.http, c.post_ms, c.request_id, c.node = s, round(ms, 1), h.get("x-request-id"), h.get("x-upstream-node")
    c.error = error_code(b)
    if s in (200, 202) and isinstance(b, dict):
        c.txn_id = b.get("txn_id")
    if c.twice and c.txn_id:
        time.sleep(0.2)
        s2, h2, b2, _ = call("POST", "/api/v1/payments", body, headers)
        c.second_http, c.second_error = s2, error_code(b2)
        c.second_txn = b2.get("txn_id") if isinstance(b2, dict) else None
    if c.reuse_key_with_amount and c.txn_id:
        s2, _, b2, _ = call("POST", "/api/v1/payments", dict(body, amount=c.reuse_key_with_amount), headers)
        c.second_http, c.second_error = s2, error_code(b2)
    if c.txn_id:
        deadline = time.time() + poll_timeout_s
        delay = 0.3
        while time.time() < deadline:
            st, _, t, _ = get(f"/api/v1/payments/{c.txn_id}", c.client())
            if st == 200 and t.get("status") in FINAL:
                c.final, c.reason, c.bank_ref, c.attempts = t["status"], t.get("failure_reason"), t.get("bank_ref"), t.get("attempts")
                break
            if st == 200:
                c.final, c.attempts = t.get("status"), t.get("attempts")
            time.sleep(delay)
            delay = min(delay * 1.3, 2.0)
        c.settle_s = round(time.perf_counter() - started, 2)
    judge(c)
    return c


def judge(c: Case) -> None:
    problems = []
    if c.http not in c.expect_http:
        problems.append(f"HTTP {c.http} (expected {'/'.join(map(str, c.expect_http))})")
    if c.expect_error and c.error != c.expect_error:
        problems.append(f"error {c.error} (expected {c.expect_error})")
    random_decline = (c.expect_final == ("SUCCESS",) and c.final == "FAILED"
                      and c.reason == "DECLINED_BY_REMITTER_BANK" and c.attempts == 1)
    if c.expect_final:
        if random_decline:
            c.why = "random bank decline (the mock bank declines 0.5% of payments in normal mode, like a real issuer)"
        elif c.final not in c.expect_final:
            problems.append(f"final {c.final} (expected {'/'.join(c.expect_final)})")
        if c.expect_reason is not None and c.final == "FAILED" and c.reason not in c.expect_reason:
            problems.append(f"reason {c.reason}")
    elif c.txn_id:
        problems.append("a transaction was created but should not have been")
    if c.twice and not (c.second_http == 200 and c.second_txn == c.txn_id):
        problems.append(f"double tap: second HTTP {c.second_http}, same txn={c.second_txn == c.txn_id}")
    if c.reuse_key_with_amount and c.second_http != 409:
        problems.append(f"key reuse: HTTP {c.second_http} (expected 409)")
    c.ok = not problems
    if problems:
        c.why = "; ".join(problems)


def run_batch(cases: list[Case], concurrency: int) -> list[Case]:
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return list(pool.map(run_case, cases))


# --------------------------------------------------------------------------- helpers
def vpa(i: int) -> str:
    return f"user{i:04d}@mockbank"


def rand_amount(rng: random.Random, lo: int = 10, hi: int = 5000) -> str:
    while True:
        a = Decimal(rng.randint(lo * 100, hi * 100)) / 100
        if int(a * 100) % 100 != 13:          # .13 is the bank mock's "always decline" trick
            return f"{a:.2f}"


def balances(vpas: list[str]) -> dict[str, int]:
    def one(item: tuple[int, str]) -> tuple[str, int | None]:
        i, v = item
        s, _, b, _ = get(f"/api/v1/accounts/{v}/balance", f"verifier-{i // 25}")
        return v, (b["balance_paise"] if s == 200 else None)

    with ThreadPoolExecutor(max_workers=10) as pool:
        return dict(pool.map(one, enumerate(vpas)))


def summary() -> dict[str, Any]:
    return call("GET", "/dashboard/api/summary")[2]


def stats_counters() -> dict[str, float]:
    """A few worker / gateway counters summed over nodes."""
    _, _, st, _ = call("GET", "/stats")
    total: dict[str, float] = defaultdict(float)

    def add(snap: dict[str, Any]) -> None:
        for key, value in snap.get("counters", {}).items():
            name = key.split("{", 1)[0]
            if name == "payments_processed_total":
                name += "{" + key.split("{", 1)[1]
            if name.startswith("circuit_transitions_total") and 'to="OPEN"' in key:
                name = "circuit_opened_total"
            total[name] += value

    add(st.get("gateway", {}))
    for snap in st.get("nodes", {}).values():
        if isinstance(snap, dict) and "error" not in snap:
            add(snap)
    return dict(total)


def chaos(mode: str) -> None:
    s, _, b, _ = call("POST", "/dashboard/api/chaos", {"mode": mode})
    out(f"  bank switch -> {B}{mode}{X}  (HTTP {s}, failure_rate={b.get('failure_rate') if isinstance(b, dict) else '?'})")


def phase(name: str, text: str) -> None:
    out("")
    out(f"=== PHASE {name} ===")
    out(f"{B}{text}{X}")


def report(name: str, cases: list[Case], extra: str = "") -> dict[str, Any]:
    finals = Counter(c.final or f"HTTP {c.http} {c.error or ''}".strip() for c in cases)
    passed = sum(1 for c in cases if c.ok)
    settle = sorted(c.settle_s for c in cases if c.settle_s is not None and c.final == "SUCCESS")
    p50 = settle[len(settle) // 2] if settle else None
    p95 = settle[min(len(settle) - 1, int(len(settle) * 0.95))] if settle else None
    colour = G if passed == len(cases) else R
    out(f"  {colour}{passed}/{len(cases)} as expected{X}   outcomes: " + ", ".join(f"{k}={v}" for k, v in finals.most_common())
        + (f"   settle p50={p50}s p95={p95}s" if settle else "") + (f"   {extra}" if extra else ""))
    for c in cases:
        if not c.ok:
            out(f"    {R}x{X} {c.payer} -> {c.payee} Rs {c.amount}: {c.why}  {D}request_id={c.request_id}{X}")
        elif c.why:
            out(f"    {Y}i{X} {c.payer} -> {c.payee} Rs {c.amount}: {c.why}  {D}request_id={c.request_id}{X}")
    random_declines = sum(1 for c in cases if c.ok and c.why.startswith("random bank decline"))
    if random_declines:
        extra = (extra + "   " if extra else "") + f"{random_declines} random bank decline(s)"
    return {"name": name, "cases": len(cases), "passed": passed, "outcomes": dict(finals),
            "settle_p50_s": p50, "settle_p95_s": p95, "note": extra}


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--users", type=int, default=100, help="distinct users in the P2P phase (default 100)")
    ap.add_argument("--concurrency", type=int, default=25)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--skip-chaos", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "reports" / "multi_user_test"))
    args = ap.parse_args()
    rng = random.Random(args.seed)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    t_start = time.time()

    out(f"{B}UPI multi-user scenario test{X}  gateway={GATEWAY}  users={args.users}  seed={args.seed}")
    s, _, h, _ = call("GET", "/health?live=true")
    out(f"  gateway health: {h.get('status') if isinstance(h, dict) else s}")
    if not isinstance(h, dict) or h.get("status") == "DOWN":
        out(f"{R}stack is not healthy - start it with docker compose up -d{X}")
        return 1
    chaos("normal")

    n = args.users
    # user blocks so that every phase uses its own people
    p2p_users = list(range(1, n + 1))
    p2m_users = list(range(n + 1, n + 31))
    hot_users = list(range(n + 31, n + 51))
    dup_users = list(range(n + 51, n + 61))
    reuse_users = list(range(n + 61, n + 66))
    neg_users = list(range(n + 66, n + 96))
    rl_user = n + 96
    flaky_users = list(range(n + 97, n + 117))
    down_users = list(range(n + 117, n + 127))
    overspend_payees = list(range(n + 127, n + 130))
    last = n + 130
    if last > 1000:
        out(f"{R}too many users for the 1000 seeded accounts{X}")
        return 1
    tracked = [vpa(i) for i in range(1, last + 1)] + ["alice@okaxis", "bob@oksbi", "chaipoint@okicici"]

    money_before = summary()
    bal_before = balances(tracked)
    counters_before = stats_counters()
    out(f"  money in system before: Rs {money_before['money_in_system']} across {money_before['accounts']} accounts")
    all_cases: list[Case] = []
    phases: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- P2P
    phase("p2p", f"{n} different users each send money to another user (person to person)")
    cases = []
    for i in p2p_users:
        j = rng.choice([k for k in p2p_users if k != i])
        cases.append(Case("P2P transfer", vpa(i), vpa(j), rand_amount(rng), note=f"p2p {i}->{j}"))
    t0 = time.time()
    run_batch(cases, args.concurrency)
    tput = len(cases) / (time.time() - t0)
    phases.append(report("P2P transfer", cases, f"{tput:.1f} payments/s end-to-end"))
    all_cases += cases

    # ---------------------------------------------------------------- P2M
    phase("p2m", "30 users pay the merchant chaipoint@okicici (person to merchant)")
    cases = [Case("P2M merchant payment", vpa(i), "chaipoint@okicici", rand_amount(rng, 20, 400), note="chai") for i in p2m_users]
    run_batch(cases, args.concurrency)
    phases.append(report("P2M merchant payment", cases))
    all_cases += cases

    # ---------------------------------------------------------------- hot payee
    phase("hot-payee", "20 users pay alice@okaxis at the same moment (row-lock contention on one account)")
    cases = [Case("Concurrent payments to one payee", vpa(i), "alice@okaxis", rand_amount(rng, 50, 500), note="hot") for i in hot_users]
    run_batch(cases, 20)
    phases.append(report("Concurrent payments to one payee", cases))
    all_cases += cases

    # ---------------------------------------------------------------- overspend
    phase("overspend", "bob sends 3 payments at once that together exceed his balance")
    bob = balances(["bob@oksbi"])["bob@oksbi"]
    each = Decimal(bob) / 100 / Decimal("2.5")
    each = min(each, Decimal("99000")).quantize(Decimal("1"))
    out(f"  bob's balance Rs {Decimal(bob) / 100:.2f} -> 3 x Rs {each}: only 2 can fit")
    cases = [Case("Concurrent overspend", "bob@oksbi", vpa(i), f"{each:.2f}", note="overspend",
                  expect_final=FINAL, expect_reason=("INSUFFICIENT_FUNDS",)) for i in overspend_payees]
    run_batch(cases, 3)
    wins = sum(1 for c in cases if c.final == "SUCCESS")
    for c in cases:
        if wins != 2:
            c.ok, c.why = False, f"{wins} succeeded, expected exactly 2"
    phases.append(report("Concurrent overspend", cases, f"{wins} SUCCESS / {3 - wins} INSUFFICIENT_FUNDS"))
    all_cases += cases

    # ---------------------------------------------------------------- idempotency
    phase("idempotency", "10 users double-tap Pay (same Idempotency-Key twice); 5 reuse a key for a different amount")
    cases = [Case("Double tap (idempotent replay)", vpa(i), vpa(rng.choice(p2p_users)), rand_amount(rng), twice=True, note="double tap")
             for i in dup_users]
    cases += [Case("Idempotency key reused", vpa(i), vpa(rng.choice(p2p_users)), rand_amount(rng),
                   reuse_key_with_amount="1.00", note="key reuse") for i in reuse_users]
    run_batch(cases, args.concurrency)
    phases.append(report("Double tap (idempotent replay)", [c for c in cases if c.twice]))
    phases.append(report("Idempotency key reused", [c for c in cases if c.reuse_key_with_amount]))
    all_cases += cases

    # ---------------------------------------------------------------- negative
    phase("negative", "business declines and invalid requests (30 users)")
    chai = balances(["chaipoint@okicici"])["chaipoint@okicici"]
    insufficient_amount = f"{min(Decimal(chai) / 100 + 500, Decimal('99999')):.2f}"
    it = iter(neg_users)
    cases = []
    for _ in range(10):
        cases.append(Case("Bank decline", vpa(next(it)), vpa(rng.choice(p2p_users)), f"{rng.randint(1, 900)}.13",
                          expect_final=("FAILED",), expect_reason=("DECLINED_BY_REMITTER_BANK",), note="decline"))
    cases.append(Case("Insufficient funds", "chaipoint@okicici", "alice@okaxis", insufficient_amount,
                      expect_final=("FAILED",), expect_reason=("INSUFFICIENT_FUNDS",), note="more than balance"))
    for _ in range(4):
        cases.append(Case("Amount above Rs 1 lakh", vpa(next(it)), vpa(rng.choice(p2p_users)), "150000.00",
                          expect_http=(422,), expect_final=(), expect_error="AMOUNT_LIMIT_EXCEEDED"))
    for _ in range(4):
        u = vpa(next(it))
        cases.append(Case("Pay yourself", u, u, "10.00", expect_http=(422,), expect_final=(), expect_error="SAME_PAYER_PAYEE"))
    for _ in range(4):
        cases.append(Case("Unknown payee", vpa(next(it)), f"ghost{rng.randint(1000, 9999)}@okhdfc", "10.00",
                          expect_http=(404,), expect_final=(), expect_error="PAYEE_VPA_NOT_FOUND"))
    for _ in range(4):
        cases.append(Case("Malformed VPA", vpa(next(it)), "not-a-vpa", "10.00",
                          expect_http=(422,), expect_final=(), expect_error="VALIDATION_ERROR"))
    for _ in range(4):
        cases.append(Case("Missing Idempotency-Key", vpa(next(it)), vpa(rng.choice(p2p_users)), "10.00", send_key=False,
                          expect_http=(400,), expect_final=(), expect_error="MISSING_IDEMPOTENCY_KEY"))
    run_batch(cases, args.concurrency)
    for name in dict.fromkeys(c.scenario for c in cases):
        phases.append(report(name, [c for c in cases if c.scenario == name]))
    all_cases += cases

    # ---------------------------------------------------------------- rate limit
    phase("rate-limit", f"{vpa(rl_user)} floods 30 payment requests in parallel; another user pays at the same time")
    flood_client = f"phone-{vpa(rl_user)}"

    flood_txns: list[str] = []

    def flood(_: int) -> tuple[int, dict[str, str]]:
        body = {"payer_vpa": vpa(rl_user), "payee_vpa": vpa(1), "amount": "1.00", "note": "flood"}
        s, h, b, _ = call("POST", "/api/v1/payments", body, {"X-Client-ID": flood_client, "Idempotency-Key": uuid.uuid4().hex})
        if s == 202 and isinstance(b, dict):
            flood_txns.append(b["txn_id"])
        return s, h

    with ThreadPoolExecutor(max_workers=30) as pool:
        flood_results = list(pool.map(flood, range(30)))
    other = run_case(Case("Rate limit: bystander unaffected", vpa(2), vpa(3), "5.00", note="bystander"))
    codes = Counter(s for s, _ in flood_results)
    limited = [h for s, h in flood_results if s == 429]
    rl_ok = bool(limited) and "retry-after" in limited[0] and other.ok
    out(f"  flood result: " + ", ".join(f"HTTP {k} x{v}" for k, v in sorted(codes.items()))
        + (f"   first 429: Retry-After={limited[0].get('retry-after')}s limit={limited[0].get('x-ratelimit-limit')} "
           f"policy={limited[0].get('x-ratelimit-policy')}" if limited else ""))
    out(f"  bystander {vpa(2)} -> {other.final} ({'unaffected' if other.ok else 'AFFECTED'})")
    phases.append({"name": "Rate limit (one user floods 30 requests)", "cases": 30, "passed": 30 if rl_ok else 0,
                   "outcomes": {f"HTTP {k}": v for k, v in codes.items()}, "settle_p50_s": None, "settle_p95_s": None,
                   "note": f"bystander payment {other.final}"})
    all_cases.append(other)
    # the flood's accepted payments are real: wait for them so the balance check below includes them
    time.sleep(3)

    # ---------------------------------------------------------------- chaos
    chaos_notes: dict[str, Any] = {}
    if not args.skip_chaos:
        phase("chaos-flaky", "bank switch fails 50% of calls; 20 users pay (workers retry with exponential backoff)")
        c0 = stats_counters()
        chaos("flaky")
        cases = [Case("Bank flaky (50% errors)", vpa(i), vpa(rng.choice(p2p_users)), rand_amount(rng), expect_final=FINAL,
                      expect_reason=("BANK_UNAVAILABLE",), note="flaky") for i in flaky_users]
        run_batch(cases, 20)
        c1 = stats_counters()
        retries = c1.get("bank_retries_total", 0) - c0.get("bank_retries_total", 0)
        chaos_notes["flaky_retries"] = retries
        phases.append(report("Bank flaky (50% errors)", cases, f"{retries:.0f} bank retries"))
        all_cases += cases

        phase("chaos-down", "bank switch DOWN; 10 users pay (circuit breaker opens, payments wait in the queue)")
        chaos("down")
        down_cases = [Case("Bank down, then recovers", vpa(i), vpa(rng.choice(p2p_users)), rand_amount(rng), expect_final=FINAL,
                           expect_reason=("BANK_UNAVAILABLE",), note="bank down") for i in down_users]
        pool = ThreadPoolExecutor(max_workers=10)
        futures = [pool.submit(run_case, c, 180) for c in down_cases]
        time.sleep(10)
        c2 = stats_counters()
        opened = c2.get("circuit_opened_total", 0) - c1.get("circuit_opened_total", 0)
        _, _, health, _ = call("GET", "/health?live=true")
        breakers = {n["node"]: (n.get("circuit_breaker") or {}).get("state") for n in health.get("nodes", []) if n["service"] == "worker"}
        out(f"  after 10 s: circuit breaker opened {opened:.0f} time(s); worker npci breakers: {breakers}")
        chaos_notes.update(breaker_opened=opened, breakers_while_down=breakers)
        out("=== PHASE chaos-recover ===")
        out(f"{B}bank back to normal: waiting payments are re-delivered (XAUTOCLAIM) and finish{X}")
        chaos("normal")
        for f in futures:
            f.result()
        pool.shutdown()
        phases.append(report("Bank down, then recovers", down_cases,
                             f"breaker opened {opened:.0f}x"))
        all_cases += down_cases

    # ---------------------------------------------------------------- verify
    phase("verify", "consistency checks: money conserved, balances match SUCCESS payments, nothing stuck")
    time.sleep(2)
    money_after = summary()
    bal_after = balances(tracked)
    counters_after = stats_counters()

    expected = defaultdict(int)
    for c in all_cases:
        if c.final == "SUCCESS":
            p = int(Decimal(c.amount) * 100)
            expected[c.payer] -= p
            expected[c.payee] += p
    # the flood payments that got through (202) are real money movements too
    flood_payer = vpa(rl_user)
    for i, txn_id in enumerate(flood_txns):
        _, _, t, _ = get(f"/api/v1/payments/{txn_id}", f"verifier-flood-{i}")
        if isinstance(t, dict) and t.get("status") == "SUCCESS":
            expected[t["payer_vpa"]] -= t["amount_paise"]
            expected[t["payee_vpa"]] += t["amount_paise"]
        elif isinstance(t, dict) and t.get("status") not in FINAL:  # counted by the "nothing stuck" check
            all_cases.append(Case("Rate limit: accepted flood payment", flood_payer, vpa(1), "1.00", txn_id=txn_id,
                                  final=t.get("status"), ok=False))

    mismatches = []
    for v in tracked:
        b0, b1 = bal_before.get(v), bal_after.get(v)
        if b0 is None or b1 is None:
            mismatches.append((v, "unreadable"))
        elif b1 - b0 != expected.get(v, 0):
            mismatches.append((v, f"moved {b1 - b0} paise, expected {expected.get(v, 0)}"))
    negative = [v for v, b in bal_after.items() if b is not None and b < 0]
    stuck = [c for c in all_cases if c.txn_id and c.final not in FINAL]
    conserved = money_before["money_in_system_paise"] == money_after["money_in_system_paise"]
    checks = [
        ("Total money in the system unchanged", conserved,
         f"Rs {money_before['money_in_system']} -> Rs {money_after['money_in_system']}"),
        (f"Every one of {len(tracked)} tracked accounts moved by exactly its SUCCESS payments", not mismatches,
         f"{len(mismatches)} mismatches" if mismatches else "all match"),
        ("No negative balances", not negative, f"{len(negative)} negative"),
        ("No payment left PENDING/PROCESSING", not stuck, f"{len(stuck)} stuck"),
        ("Every double tap returned the original transaction (one debit)",
         all(c.second_http == 200 and c.second_txn == c.txn_id for c in all_cases if c.twice),
         f"{sum(1 for c in all_cases if c.twice)} double taps"),
    ]
    for name, ok, detail in checks:
        out(f"  {G + 'PASS' if ok else R + 'FAIL'}{X}  {name}  {D}{detail}{X}")
    for v, why in mismatches[:10]:
        out(f"    {R}{v}: {why}{X}")

    # ---------------------------------------------------------------- summary + files
    distinct_payers = {c.payer for c in all_cases} | {flood_payer}
    distinct_people = distinct_payers | {c.payee for c in all_cases if c.final}
    total_cases = sum(p["cases"] for p in phases)
    total_pass = sum(p["passed"] for p in phases)
    all_ok = total_pass == total_cases and all(ok for _, ok, _ in checks)
    duration = time.time() - t_start
    proc = {k: counters_after.get(k, 0) - counters_before.get(k, 0) for k in counters_after
            if k.startswith(("payments_processed_total", "bank_retries_total", "gw_rate_limited_total",
                             "payments_deferred_total", "circuit_opened_total", "payments_idempotent_replay_total",
                             "dlq_messages_total"))}
    out("")
    out(f"{B}Summary{X}: {total_pass}/{total_cases} scenario requests behaved as expected, "
        f"{sum(ok for _, ok, _ in checks)}/{len(checks)} consistency checks passed, "
        f"{len(distinct_payers)} distinct paying users, {len(distinct_people)} distinct people involved, {duration:.0f} s")
    out(f"{G + 'ALL PASSED' if all_ok else R + 'SOME CHECKS FAILED'}{X}")

    samples = {}
    for c in all_cases:
        if c.request_id and c.scenario not in samples and (c.txn_id or c.error):
            samples[c.scenario] = {"request_id": c.request_id, "txn_id": c.txn_id, "payer": c.payer, "payee": c.payee,
                                   "amount": c.amount, "final": c.final or c.error}
    results = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "gateway": GATEWAY, "seed": args.seed,
        "duration_s": round(duration, 1), "distinct_payers": len(distinct_payers), "distinct_people": len(distinct_people),
        "total_cases": total_cases, "total_passed": total_pass, "all_ok": all_ok,
        "phases": phases, "checks": [{"name": n_, "ok": ok, "detail": d} for n_, ok, d in checks],
        "money_before": money_before, "money_after": money_after, "counters_delta": proc, "chaos": chaos_notes,
        "samples": samples, "mismatches": mismatches[:50],
    }
    (outdir / "results.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    with (outdir / "transactions.csv").open("w", newline="", encoding="utf-8") as fh:
        cols = ["scenario", "payer", "payee", "amount", "http", "error", "txn_id", "final", "reason", "bank_ref",
                "attempts", "post_ms", "settle_s", "node", "request_id", "second_http", "ok", "why"]
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for c in all_cases:
            w.writerow(asdict(c))
    (outdir / "console.log").write_text("\n".join(_console) + "\n", encoding="utf-8")
    out(f"{D}wrote {outdir / 'results.json'}, transactions.csv, console.log{X}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
