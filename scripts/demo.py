"""Demo / operations CLI for the UPI system (stdlib only - works on Windows, macOS, Linux).

  python scripts/demo.py health                       per-node health table
  python scripts/demo.py balance alice@okaxis         shows cache MISS then HIT
  python scripts/demo.py pay alice@okaxis bob@oksbi 150 [--twice]
                                                      send money, follow it to SUCCESS
                                                      (--twice: replay the same Idempotency-Key)
  python scripts/demo.py burst --n 80                 fire 80 requests from ONE client -> 429s
  python scripts/demo.py chaos flaky|down|slow|normal switch the mock bank's failure mode
  python scripts/demo.py trace <request_id|txn_id>    follow one request through every service's logs
  python scripts/demo.py stats                        one-screen summary of /stats
  python scripts/demo.py smoke                        end-to-end self test (also used by CI)

Environment: GATEWAY_URL (default http://127.0.0.1:8080), BANK_URL (default http://127.0.0.1:8090)
(127.0.0.1 rather than "localhost": on Windows, "localhost" tries IPv6 first, which can add
 1-2 s to every request with Docker Desktop.)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

GATEWAY = os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").rstrip("/")
BANK = os.getenv("BANK_URL", "http://127.0.0.1:8090").rstrip("/")
ROOT = Path(__file__).resolve().parent.parent

if os.name == "nt":
    os.system("")  # enable ANSI colours in the classic Windows console

GREEN, RED, YELLOW, CYAN, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[1m", "\033[0m"
STATUS_COLOR = {"UP": GREEN, "SUCCESS": GREEN, "CLOSED": GREEN, "DEGRADED": YELLOW, "PENDING": YELLOW,
                "PROCESSING": CYAN, "HALF_OPEN": YELLOW, "DOWN": RED, "FAILED": RED, "OPEN": RED, "UNKNOWN": DIM}


def color(text: Any) -> str:
    text = str(text)
    return f"{STATUS_COLOR.get(text, '')}{text}{RESET}"


# --------------------------------------------------------------------------- http helpers
def call(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None,
         timeout: float = 10) -> tuple[int, dict[str, str], Any, float]:
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"} if body is not None else {}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, status, rh = resp.read(), resp.status, resp.headers
    except urllib.error.HTTPError as exc:
        raw, status, rh = exc.read(), exc.code, exc.headers
    elapsed = (time.perf_counter() - start) * 1000
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw.decode(errors="replace")
    return status, {k.lower(): v for k, v in rh.items()}, parsed, elapsed


def api(method: str, path: str, body: Any = None, client: str = "demo-cli", **headers: str):
    return call(method, f"{GATEWAY}{path}", body, {"X-Client-ID": client, **headers})


# --------------------------------------------------------------------------- commands
def cmd_health(_: argparse.Namespace) -> int:
    status, _, body, _ = call("GET", f"{GATEWAY}/health?live=true")
    if not isinstance(body, dict):
        print(f"{RED}gateway not reachable (HTTP {status}){RESET}")
        return 1
    rl = body["rate_limiter"]
    print(f"\n{BOLD}Gateway{RESET}: {color(body['status'])}   rate limiter backend={rl['backend']}  redis={color(rl['redis'])}")
    print(f"\n{'NODE':<20}{'SERVICE':<18}{'ROLE':<11}{'STATUS':<19}{'CIRCUIT':<20}{'TRAFFIC':<9}{'CHECK ms':>9}")
    for n in body["nodes"]:
        traffic = "yes" if n["receiving_traffic"] else ("-" if n["role"] == "monitored" else f"{RED}no{RESET}")
        print(f"{n['node']:<20}{n['service']:<18}{n['role']:<11}{color(n['status']):<28}"
              f"{color(n['circuit']['state']):<29}{traffic:<9}{n['health_check_latency_ms'] or 0:>9.1f}")
        extra = n.get("circuit_breaker") or {}
        if n["service"] == "worker" and extra:
            q = n.get("queue") or {}
            print(f"{'':<20}{DIM}npci breaker={extra.get('state')}  queue length={q.get('length')} "
                  f"pending={q.get('pending')} lag={q.get('lag')} dlq={q.get('dead_letters')}{RESET}")
        if n.get("last_error"):
            print(f"{'':<20}{RED}{n['last_error']}{RESET}")
    print()
    return 0 if body["status"] != "DOWN" else 1


def cmd_balance(args: argparse.Namespace) -> int:
    for i in range(2):
        status, headers, body, ms = api("GET", f"/api/v1/accounts/{args.vpa}/balance", client=args.client)
        print(f"[{i + 1}] HTTP {status}  X-Cache={headers.get('x-cache', '-'):<6} {ms:6.1f} ms  "
              f"node={headers.get('x-upstream-node')}  request_id={headers.get('x-request-id')}")
        if status == 200:
            print(f"    {body['vpa']}: Rs {body['balance']}  ({body['bank']} {body['account']})")
        else:
            print(f"    {body}")
    return 0


def cmd_pay(args: argparse.Namespace) -> int:
    key = args.key or uuid.uuid4().hex
    body = {"payer_vpa": args.payer, "payee_vpa": args.payee, "amount": args.amount, "note": args.note}
    rounds = 2 if args.twice else 1
    txn_id = None
    for i in range(rounds):
        status, headers, resp, ms = api("POST", "/api/v1/payments", body, client=args.client,
                                        **{"Idempotency-Key": key})
        replay = headers.get("idempotent-replay") == "true"
        print(f"{BOLD}POST /api/v1/payments{RESET} -> HTTP {status} in {ms:.1f} ms via {headers.get('x-upstream-node')}"
              f"{'  (Idempotent-Replay: same transaction returned, no second debit)' if replay else ''}")
        print(f"  request_id={CYAN}{headers.get('x-request-id')}{RESET}  idempotency_key={key}")
        if status not in (200, 202):
            print(f"  {RED}{resp}{RESET}")
            return 1
        txn_id = resp["txn_id"]
        print(f"  txn_id={txn_id}  status={color(resp['status'])}  payee_name={resp.get('payee_name', '-')}")
        if i == 0 and rounds == 2:
            time.sleep(0.5)

    print(f"\npolling GET /api/v1/payments/{txn_id} ...")
    last, start = None, time.time()
    while time.time() - start < 45:
        status, headers, resp, ms = api("GET", f"/api/v1/payments/{txn_id}", client=args.client)
        if status == 200 and resp["status"] != last:
            last = resp["status"]
            extra = resp.get("failure_reason") or resp.get("bank_ref") or ""
            print(f"  +{time.time() - start:5.2f}s  {color(last):<20}  X-Cache={headers.get('x-cache')}  {extra}")
        if last in ("SUCCESS", "FAILED"):
            break
        time.sleep(0.3)
    print(f"\ntrace it:  python scripts/demo.py trace {resp.get('request_id')}\n")
    return 0 if last == "SUCCESS" else 1


def cmd_burst(args: argparse.Namespace) -> int:
    path = args.path or "/api/v1/accounts/alice@okaxis/balance"
    print(f"firing {args.n} requests at {path} from ONE client id '{args.client}' ({args.concurrency} in parallel)")

    def one(_: int) -> tuple[int, dict[str, str]]:
        status, headers, _, _ = api("GET", path, client=args.client)
        return status, headers

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(one, range(args.n)))
    elapsed = time.perf_counter() - start
    counts = Counter(status for status, _ in results)
    print(f"done in {elapsed:.2f} s:  " + "  ".join(f"HTTP {k}: {v}" for k, v in sorted(counts.items())))
    limited = [h for s, h in results if s == 429]
    if limited:
        h = limited[0]
        print(f"first 429 -> Retry-After: {h.get('retry-after')} s, X-RateLimit-Limit: {h.get('x-ratelimit-limit')}, "
              f"X-RateLimit-Remaining: {h.get('x-ratelimit-remaining')}, policy: {h.get('x-ratelimit-policy')}")
    print("(another client id is not affected:", api("GET", path, client=f"other-{uuid.uuid4().hex[:6]}")[0], ")")
    return 0


def cmd_chaos(args: argparse.Namespace) -> int:
    status, _, body, _ = call("POST", f"{BANK}/admin/chaos", {"mode": args.mode})
    print(f"bank switch mode -> {body}")
    return 0 if status == 200 else 1


def _logs(since: str) -> list[dict[str, Any]]:
    out = subprocess.run(["docker", "compose", "logs", "--no-color", "--no-log-prefix", "--since", since],
                         cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    events = []
    for line in out.stdout.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            events.append(json.loads(line[start:]))
        except ValueError:
            continue
    return events


def cmd_trace(args: argparse.Namespace) -> int:
    ident = args.id
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", ident):
        status, _, body, _ = api("GET", f"/api/v1/payments/{ident}")
        if status == 200 and body.get("request_id"):
            print(f"txn {ident} was created by request_id {body['request_id']}")
            ident = body["request_id"]
    events = sorted((e for e in _logs(args.since) if e.get("request_id") == ident), key=lambda e: e.get("ts", ""))
    if not events:
        print(f"no log lines with request_id={ident} in the last {args.since}")
        return 1
    skip = {"ts", "level", "service", "node", "event", "request_id"}
    print(f"\n{BOLD}Trace of request {ident}{RESET}  ({len(events)} log lines)\n")
    for e in events:
        details = " ".join(f"{k}={v}" for k, v in e.items() if k not in skip and v not in (None, ""))
        level = e.get("level", "")
        lc = RED if level == "ERROR" else (YELLOW if level == "WARNING" else "")
        print(f"{DIM}{e.get('ts', '')[11:23]}{RESET} {CYAN}{e.get('node', ''):<18}{RESET} "
              f"{lc}{e.get('event', ''):<24}{RESET} {DIM}{details[:150]}{RESET}")
    print()
    return 0


def _sum(snap: dict[str, Any], name: str, **labels: str) -> float:
    total = 0.0
    for key, value in snap.get("counters", {}).items():
        if key == name or key.startswith(name + "{"):
            if all(f'{k}="{v}"' in key for k, v in labels.items()):
                total += value
    return total


def cmd_stats(_: argparse.Namespace) -> int:
    status, _, stats, _ = call("GET", f"{GATEWAY}/stats")
    if status != 200:
        print(f"{RED}/stats failed: HTTP {status}{RESET}")
        return 1
    gw = stats["gateway"]
    print(f"\n{BOLD}Gateway{RESET}  (limiter backend: {stats['rate_limiter_backend']})")
    for outcome in ("proxied", "rate_limited", "rejected", "upstream_error"):
        print(f"  {outcome:<15}{_sum(gw, 'gw_requests_total', outcome=outcome):>10,.0f}")
    print(f"  429 by scope:  global={_sum(gw, 'gw_rate_limited_total', scope='global'):,.0f}  "
          f"client={_sum(gw, 'gw_rate_limited_total', scope='client'):,.0f}")
    print(f"\n{BOLD}Nodes{RESET}")
    for name, snap in stats["nodes"].items():
        if "error" in snap:
            print(f"  {name:<20}{RED}unreachable{RESET}")
            continue
        hit, miss = _sum(snap, "cache_requests_total", result="hit"), _sum(snap, "cache_requests_total", result="miss")
        line = f"  {name:<20}http={_sum(snap, 'http_requests_total'):>8,.0f}"
        if hit + miss:
            line += f"  cache hit={100 * hit / (hit + miss):5.1f}%  db_queries={_sum(snap, 'db_queries_total'):,.0f}"
        if snap.get("service") == "worker":
            g = snap.get("gauges", {})
            line += (f"  ok={_sum(snap, 'payments_processed_total', status='SUCCESS'):,.0f}"
                     f" failed={_sum(snap, 'payments_processed_total', status='FAILED'):,.0f}"
                     f" retries={_sum(snap, 'bank_retries_total'):,.0f}"
                     f" breaker_opens={_sum(snap, 'circuit_transitions_total', to='OPEN'):,.0f}"
                     f"  queue pending={g.get('queue_pending', 0):.0f} lag={g.get('queue_lag', 0):.0f}")
        print(line)
    print()
    return 0


# --------------------------------------------------------------------------- smoke test
def cmd_smoke(args: argparse.Namespace) -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        results.append((name, ok, detail))
        print(f"  {GREEN + 'PASS' if ok else RED + 'FAIL'}{RESET}  {name}  {DIM}{detail}{RESET}")
        return ok

    print(f"\n{BOLD}Smoke test against {GATEWAY}{RESET}")
    deadline, health = time.time() + args.wait, None
    while time.time() < deadline:
        try:
            _, _, health, _ = call("GET", f"{GATEWAY}/health?live=true", timeout=5)
            if isinstance(health, dict) and health.get("status") == "UP":
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    ok_status = isinstance(health, dict) and health.get("status") in ("UP", "DEGRADED")
    check("gateway /health reports all nodes", ok_status, health.get("status") if isinstance(health, dict) else "unreachable")
    if not ok_status:
        return 1

    s, _, b, _ = api("GET", "/api/v1/vpa/alice@okaxis")
    check("VPA lookup returns the registered name", s == 200 and b.get("name") == "Alice Sharma", f"HTTP {s}")

    api("GET", "/api/v1/accounts/chaipoint@okicici/balance")
    s, h, _, _ = api("GET", "/api/v1/accounts/chaipoint@okicici/balance")
    check("second balance read is a cache HIT", s == 200 and h.get("x-cache") == "HIT", f"X-Cache={h.get('x-cache')}")

    key = uuid.uuid4().hex
    pay = {"payer_vpa": "alice@okaxis", "payee_vpa": "bob@oksbi", "amount": "1.00", "note": "smoke"}
    s, h, b, _ = api("POST", "/api/v1/payments", pay, **{"Idempotency-Key": key})
    accepted = check("payment accepted with 202", s == 202, f"HTTP {s}")
    if accepted:
        txn_id, final = b["txn_id"], None
        for _ in range(100):
            _, _, t, _ = api("GET", f"/api/v1/payments/{txn_id}")
            if isinstance(t, dict) and t.get("status") in ("SUCCESS", "FAILED"):
                final = t
                break
            time.sleep(0.3)
        check("worker completes the payment (SUCCESS)", bool(final) and final["status"] == "SUCCESS",
              f"{final and final['status']} {final and final.get('failure_reason') or ''}")
        s2, h2, b2, _ = api("POST", "/api/v1/payments", pay, **{"Idempotency-Key": key})
        check("same Idempotency-Key replays the original transaction", s2 == 200 and b2.get("txn_id") == txn_id
              and h2.get("idempotent-replay") == "true", f"HTTP {s2}")

    s, _, b, _ = api("POST", "/api/v1/payments", pay)
    check("payment without Idempotency-Key is rejected by the gateway (400)", s == 400, f"HTTP {s}")

    declined = dict(pay, amount="1.13")
    s, _, b, _ = api("POST", "/api/v1/payments", declined, **{"Idempotency-Key": uuid.uuid4().hex})
    if s == 202:
        final = None
        for _ in range(100):
            _, _, t, _ = api("GET", f"/api/v1/payments/{b['txn_id']}")
            if isinstance(t, dict) and t.get("status") in ("SUCCESS", "FAILED"):
                final = t
                break
            time.sleep(0.3)
        check("bank decline ends as FAILED", bool(final) and final["status"] == "FAILED",
              f"{final and final.get('failure_reason')}")

    s, _, _, _ = api("GET", "/api/v1/nope")
    check("unknown route -> 404 from the gateway", s == 404, f"HTTP {s}")

    # Burst against the *write* bucket (capacity 10, refill 2/s): 30 parallel payment POSTs without an
    # Idempotency-Key. The gateway rate-limits before it inspects, so the first ~10 get 400 (nothing
    # reaches a service) and the rest must get 429 - even on a machine where each request is slow.
    client = f"smoke-burst-{uuid.uuid4().hex[:6]}"
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=30) as pool:
        statuses = list(pool.map(lambda _: api("POST", "/api/v1/payments", {}, client=client)[:2], range(30)))
    took = time.perf_counter() - started
    limited = [h for st, h in statuses if st == 429]
    check("burst of 30 payments from one client gets 429 + Retry-After", bool(limited) and "retry-after" in limited[0],
          f"{len(limited)} x 429 in {took:.1f} s")

    s, _, st, _ = call("GET", f"{GATEWAY}/stats")
    bad = [n for n, snap in (st or {}).get("nodes", {}).items() if "error" in snap] if s == 200 else ["/stats"]
    check("/stats collects metrics from every node", s == 200 and not bad, f"unreachable: {bad}" if bad else "")

    failed = [r for r in results if not r[1]]
    print(f"\n{GREEN if not failed else RED}{len(results) - len(failed)}/{len(results)} checks passed{RESET}\n")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health").set_defaults(fn=cmd_health)
    p = sub.add_parser("balance")
    p.add_argument("vpa")
    p.add_argument("--client", default="demo-phone")
    p.set_defaults(fn=cmd_balance)
    p = sub.add_parser("pay")
    p.add_argument("payer")
    p.add_argument("payee")
    p.add_argument("amount")
    p.add_argument("--note", default="demo payment")
    p.add_argument("--key", help="Idempotency-Key (default: random)")
    p.add_argument("--twice", action="store_true", help="send the same request twice to show idempotency")
    p.add_argument("--client", default="demo-phone")
    p.set_defaults(fn=cmd_pay)
    p = sub.add_parser("burst")
    p.add_argument("--n", type=int, default=80)
    p.add_argument("--concurrency", type=int, default=20)
    p.add_argument("--client", default="greedy-phone")
    p.add_argument("--path")
    p.set_defaults(fn=cmd_burst)
    p = sub.add_parser("chaos")
    p.add_argument("mode", choices=["normal", "flaky", "slow", "down"])
    p.set_defaults(fn=cmd_chaos)
    p = sub.add_parser("trace")
    p.add_argument("id", help="request_id (from X-Request-ID) or txn_id")
    p.add_argument("--since", default="30m")
    p.set_defaults(fn=cmd_trace)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    p = sub.add_parser("smoke")
    p.add_argument("--wait", type=float, default=120, help="seconds to wait for the stack to become healthy")
    p.set_defaults(fn=cmd_smoke)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
