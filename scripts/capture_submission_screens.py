"""Capture the remaining demonstration screens for the submission PDF.

    python scripts/capture_submission_screens.py

Complements scripts/capture_report_assets.py (dashboard, Pay Console, request traces) with:
CLI demos (health, smoke test, rate-limit burst, cache MISS->HIT, idempotent payment, trace, stats),
the gateway's /health and route table, the PostgreSQL schema and data, Redis (stream, cache keys,
rate-limit buckets), the unit tests and the Locust HTML reports of the 100/200/500-user runs.
Writes reports/submission_screens/*.png and manifest.json. Needs the stack running and Playwright.
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capture_report_assets import ansi_to_html, terminal_page  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports" / "submission_screens"
GATEWAY = os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").rstrip("/")
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
manifest: list[dict[str, str]] = []


def run(cmd: list[str], timeout: int = 180) -> str:
    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         env=ENV, timeout=timeout)
    return (res.stdout + res.stderr).rstrip()


def shot_html(page: Page, key: str, title: str, command: str, output: str, caption: str, section: str) -> None:
    f = OUT / "_render" / f"{key}.html"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(terminal_page(title, command, f"<pre>{ansi_to_html(output)}</pre>"), encoding="utf-8")
    page.set_viewport_size({"width": 1300, "height": 400})
    page.goto(f.as_uri())
    path = OUT / f"{key}.png"
    page.screenshot(path=str(path), full_page=True)
    manifest.append({"key": key, "file": f"../submission_screens/{path.name}", "caption": caption, "section": section})
    print(f"  [screenshot] {path.name}", flush=True)


def psql(sql: str) -> str:
    return run(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "upi", "-d", "upi", "-P", "pager=off", "-c", sql])


def redis(*args: str) -> str:
    return run(["docker", "compose", "exec", "-T", "redis", "redis-cli", *args])


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page()

        # ------------------------------------------------------------ phase 1: gateway, health, rate limiting
        shot_html(page, "p1_compose_up", "docker compose up --build -d", "docker compose up --build -d && docker compose ps",
                  run(["docker", "compose", "ps", "--format", "table {{.Name}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"]),
                  "`docker compose up` starts the gateway, 2 payment nodes, account service, 2 workers, bank mock, "
                  "PostgreSQL and Redis - every container healthy", "phase1")
        shot_html(page, "p1_health_cli", "Health of every node", "python scripts/demo.py health",
                  run([py, "scripts/demo.py", "health"]),
                  "Health check endpoint: the gateway polls each node's /health and reports status, circuit breaker, "
                  "and queue depth per node", "phase1")
        health = json.loads(urllib.request.urlopen(f"{GATEWAY}/health?live=true", timeout=10).read())
        health["nodes"] = health["nodes"][:3] + [{"...": f"{len(health['nodes']) - 3} more nodes"}]
        shot_html(page, "p1_health_json", "GET /health", f"curl -s {GATEWAY}/health?live=true",
                  json.dumps(health, indent=2)[:6000],
                  "GET /health (JSON): overall status UP / DEGRADED / DOWN, rate-limiter backend and per-node details", "phase1")
        routes = json.loads(urllib.request.urlopen(f"{GATEWAY}/", timeout=10).read())
        shot_html(page, "p1_routes", "Gateway route table", f"curl -s {GATEWAY}/", json.dumps(routes, indent=2),
                  "Gateway routing table: /api/v1/payments -> payment-service (2 nodes, round robin), "
                  "/api/v1/accounts and /api/v1/vpa -> account-service", "phase1")
        shot_html(page, "p1_burst", "Rate limiter: 80 requests from one client", "python scripts/demo.py burst --n 80",
                  run([py, "scripts/demo.py", "burst", "--n", "80"]),
                  "Token-bucket rate limiter: one client floods 80 requests; requests pass while its bucket has tokens "
                  "(burst 40, refilling 20/s), the rest get 429 Too Many Requests with Retry-After, while another client "
                  "is unaffected", "phase1")
        shot_html(page, "p1_smoke", "End-to-end smoke test", "python scripts/smoke_test.py",
                  run([py, "scripts/smoke_test.py"]),
                  "End-to-end smoke test: health, routing, cache HIT, async payment, idempotency, 400 without "
                  "Idempotency-Key, bank decline, 404 route, 429 burst, /stats - all 11 checks pass", "phase1")

        # ------------------------------------------------------------ phase 2: database, cache, queue + workers
        shot_html(page, "p2_tables", "PostgreSQL schema", 'psql -U upi -d upi -c "\\dt+"', psql("\\dt+"),
                  "Database tables created by the migration scripts (db/migrations/001-003)", "phase2")
        shot_html(page, "p2_txn_table", "transactions table", 'psql -U upi -d upi -c "\\d transactions"', psql("\\d transactions"),
                  "Schema of the transactions table: unique idempotency_key, status enum, lease columns, "
                  "request_id for tracing, CHECK payer <> payee", "phase2")
        shot_html(page, "p2_migrations", "Applied migrations", 'psql -c "select * from schema_migrations"',
                  psql("SELECT * FROM schema_migrations ORDER BY 1;") + "\n\n" + run(["docker", "compose", "logs", "--no-log-prefix", "migrate"])[-1500:],
                  "Migration runner: every SQL file applied once and recorded in schema_migrations", "phase2")
        shot_html(page, "p2_data", "Transactions, ledger and audit log",
                  "psql: latest transactions / double-entry ledger / audit events",
                  psql("SELECT left(txn_id::text,8) AS txn, payer_vpa, payee_vpa, amount_paise, status, failure_reason, attempts "
                       "FROM transactions ORDER BY created_at DESC LIMIT 8;")
                  + "\n\n" + psql("SELECT left(txn_id::text,8) AS txn, account_id, direction, amount_paise, balance_after_paise "
                                  "FROM ledger_entries ORDER BY id DESC LIMIT 6;")
                  + "\n\n" + psql("SELECT status, count(*), sum(amount_paise) AS paise FROM transactions GROUP BY status;"),
                  "Worker results in PostgreSQL: transaction status, one debit + one credit ledger row per SUCCESS, "
                  "totals by status", "phase2")
        shot_html(page, "p2_events", "Audit trail of one payment",
                  "psql: transaction_events of the latest payment",
                  psql("SELECT e.id, e.from_status, e.to_status, e.actor, e.detail FROM transaction_events e "
                       "WHERE e.txn_id = (SELECT txn_id FROM transactions WHERE status='SUCCESS' ORDER BY created_at DESC LIMIT 1) ORDER BY e.id;"),
                  "Append-only audit log: PENDING (payment service) -> PROCESSING -> SUCCESS (worker) for one payment",
                  "phase2")
        shot_html(page, "p2_cache", "Cache-aside: MISS then HIT", "python scripts/demo.py balance bob@oksbi",
                  run([py, "scripts/demo.py", "balance", "bob@oksbi", "--client", "cache-demo"]),
                  "Redis cache: the first balance read is a MISS (PostgreSQL), the second a HIT from Redis - faster", "phase2")
        shot_html(page, "p2_redis", "Redis: queue, cache and rate-limit keys",
                  "redis-cli XINFO GROUPS / XLEN / SCAN / TTL",
                  "> XINFO GROUPS upi:payments:stream\n" + redis("XINFO", "GROUPS", "upi:payments:stream")
                  + "\n\n> XLEN upi:payments:stream\n" + redis("XLEN", "upi:payments:stream")
                  + "\n\n> XLEN upi:payments:dlq\n" + redis("XLEN", "upi:payments:dlq")
                  + "\n\n> SCAN 0 MATCH cache:* COUNT 1000  (first 8)\n" + "\n".join(redis("--scan", "--pattern", "cache:*").splitlines()[:8])
                  + "\n\n> TTL cache:balance:bob@oksbi\n" + redis("TTL", "cache:balance:bob@oksbi")
                  + "\n\n> SCAN 0 MATCH rl:* (first 6)\n" + "\n".join(redis("--scan", "--pattern", "rl:*").splitlines()[:6]),
                  "Redis holds the payment stream + consumer group (async queue), cache entries with TTLs, and the "
                  "token buckets of the rate limiter", "phase2")
        pay_out = run([py, "scripts/demo.py", "pay", "alice@okaxis", "bob@oksbi", "150", "--twice"])
        shot_html(page, "p2_pay_cli", "Async payment, sent twice with the same key",
                  "python scripts/demo.py pay alice@okaxis bob@oksbi 150 --twice", pay_out,
                  "Asynchronous processing: POST returns 202 at once, the second request with the same Idempotency-Key "
                  "is replayed (no second debit), the worker moves it PENDING -> SUCCESS", "phase2")
        m = re.search(r"trace\s+([0-9a-f]{16})", pay_out)
        if m:
            shot_html(page, "p3_trace_cli", "Trace one request through every service",
                      f"python scripts/demo.py trace {m.group(1)}", run([py, "scripts/demo.py", "trace", m.group(1), "--since", "10m"]),
                      "Structured logging: one request id followed Gateway -> payment service -> Redis Stream -> worker "
                      "-> bank -> PostgreSQL", "phase3")

        # ------------------------------------------------------------ phase 3: metrics, tests, load test reports
        shot_html(page, "p3_stats", "Metrics of every node", "python scripts/demo.py stats", run([py, "scripts/demo.py", "stats"]),
                  "Metrics: requests, 429s by scope, cache hit ratio, DB queries, settled payments, retries and "
                  "circuit-breaker openings per node (from GET /stats)", "phase3")
        shot_html(page, "p3_pytest", "Unit and behaviour tests", "python -m pytest -q",
                  run([py, "-m", "pytest", "-q", "-p", "no:logging", "-p", "no:cacheprovider"]),
                  "Automated tests: token bucket, Lua script, circuit breaker, retry/backoff, gateway proxy, worker "
                  "settlement logic, dashboard and Pay Console routes - all pass", "phase3")
        page.set_viewport_size({"width": 1400, "height": 1100})
        for label in ("scale-u100", "scale-u200", "scale-u500"):
            report = ROOT / "loadtest" / "results" / label / "locust_report.html"
            if report.exists():
                page.goto(report.as_uri())
                page.wait_for_timeout(1500)
                path = OUT / f"p3_locust_{label}.png"
                page.screenshot(path=str(path), full_page=False)
                users = label.split("u")[-1]
                manifest.append({"key": f"p3_locust_{label}", "file": f"../submission_screens/{path.name}", "section": "loadtest",
                                 "caption": f"Locust load-test report - {users} concurrent users (60 s): request statistics "
                                            "per endpoint, failures (429) and response-time percentiles"})
                print(f"  [screenshot] {path.name}", flush=True)
        browser.close()
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"{len(manifest)} screens in {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
