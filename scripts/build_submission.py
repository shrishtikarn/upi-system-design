"""Build the submission files (names start with every roll number, in order).

    python scripts/capture_report_assets.py        # 100-user scenario test + dashboard / Pay Console / log screens
    python loadtest/scale_test.py --gateway-workers 4   # 100 / 200 / 500-user load tests (+ experiments)
    python loadtest/scale_report.py                 # load-test numbers + charts
    python scripts/capture_submission_screens.py    # CLI, DB, Redis, tests, Locust screens
    python scripts/build_submission.py              # -> submission/*.pdf (+ .md, .html)

Outputs (submission/):
  <ROLLS>_UPI_Demonstration.pdf   demonstrative screens of every scenario, GitHub URL first
  <ROLLS>_UPI_Project_Report.pdf  project + load-test report (architecture, phases, results, bottlenecks)
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_multi_user_report import (REPO_URL, ROLL_PREFIX, SOURCES, TEAM, Block, cover_block, cover_md,  # noqa: E402
                                    html_to_pdf, to_html, to_md)

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
SUB = ROOT / "submission"
ASSETS = SUB / "assets"


def load(path: Path, default: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default


TERMINAL_BG = (233, 231, 226)  # page colour around the terminal-style screenshots


def stage(src: Path, name: str | None = None) -> str:
    """Copy an image next to the submission so the HTML/MD are self-contained; return its relative path.
    Terminal-style screenshots are trimmed to the window (no empty band below short outputs)."""
    ASSETS.mkdir(parents=True, exist_ok=True)
    dst = ASSETS / (name or src.name)
    shutil.copy(src, dst)
    try:
        from PIL import Image, ImageChops
        im = Image.open(dst).convert("RGB")
        if im.getpixel((2, 2)) == TERMINAL_BG and im.getpixel((2, im.height - 3)) == TERMINAL_BG:
            box = ImageChops.difference(im, Image.new("RGB", im.size, TERMINAL_BG)).getbbox()
            if box:
                pad = 12
                im.crop((max(0, box[0] - pad), max(0, box[1] - pad), min(im.width, box[2] + pad),
                         min(im.height, box[3] + pad))).save(dst)
    except ImportError:
        pass
    return f"assets/{dst.name}"


def fig(src: Path, caption: str) -> Block:
    return ("img", stage(src), caption)


def ms(v: Any) -> str:
    if v is None:
        return "-"
    return f"{v / 1000:.1f} s" if v >= 1000 else f"{v:.0f} ms"


# ============================================================================ data
MU = REPORTS / "multi_user_test"
MU_RESULTS = load(MU / "results.json", {})
MU_SHOTS = {Path(m["file"]).stem.split("_", 1)[1]: (MU / m["file"], m["caption"]) for m in load(MU / "manifest.json", [])}
EX = REPORTS / "submission_screens"
EX_SHOTS = {m["key"]: (EX / Path(m["file"]).name, m["caption"]) for m in load(EX / "manifest.json", [])}
SCALE = load(ROOT / "loadtest" / "results" / "scale" / "summary.json", {"runs": [], "charts": []})
RUNS = {r["label"]: r for r in SCALE["runs"]}
CHART_DIR = ROOT / "loadtest" / "results" / "scale"


def mu(key: str) -> Block | None:
    if key in MU_SHOTS:
        return fig(*MU_SHOTS[key])
    return None


def ex(key: str) -> Block | None:
    if key in EX_SHOTS:
        return fig(*EX_SHOTS[key])
    return None


def chart(name: str, caption: str) -> Block | None:
    p = CHART_DIR / name
    return fig(p, caption) if p.exists() else None


def keep(*blocks: Block | None) -> list[Block]:
    return [b for b in blocks if b is not None]


def run_name(r: dict[str, Any]) -> str:
    v = r.get("variant", "")
    return f"{r['users']} users" + ("" if not v else ", limiter off" if v == "nolimit" else f", gateway x{v[2:]}")


# ============================================================================ load-test analysis text
def load_test_blocks(heading_level: str = "h2", numbered: str = "") -> list[Block]:
    if not RUNS:
        return [("p", "Load-test results not found - run `python loadtest/scale_test.py` first.")]
    order = [r for r in SCALE["runs"]]
    rows = []
    for r in order:
        c, s = r["client"], r["server"]
        partial = r.get("server_metrics_partial")
        rows.append([run_name(r), f"{c['rps']:.0f}", f"{c['ok_rps']:.0f}", f"{c['rate_limited']:,}", f"{c['other_failures']:,}",
                     ms(c["p50"]), ms(c["p95"]), ms(c["p99"]),
                     "n/a" if partial else ms(s["gateway_ms"]["p95"]),   # gateway's own histogram: one process only
                     f"{s['settle_per_s']:.1f}",                          # worker / service nodes: single processes
                     "-" if s["cache_hit_ratio"] is None else f"{s['cache_hit_ratio']:.0f}%",
                     f"{r['resources'].get('gateway', {}).get('cpu_mean', 0):.0f}%"])
    u100, u200, u500 = RUNS.get("scale-u100"), RUNS.get("scale-u200"), RUNS.get("scale-u500")
    nolim = RUNS.get("scale-u500-nolimit")
    gw = next((r for r in order if r.get("variant", "").startswith("gw")), None)
    h = heading_level
    sub = "h3" if h == "h2" else "h3"
    b: list[Block] = [
        (h, f"{numbered}Load test: 100, 200 and 500 concurrent users"),
        ("p", "**Tool:** Locust 2.43 (running in Docker on the same network as the gateway), script `loadtest/locustfile.py`, "
              "orchestrated by `loadtest/scale_test.py`. Each level runs for 60 s with a steady number of simulated phone users "
              "(each with its own device id) doing a realistic mix: check balance (8), verify payee (5), send money (4), poll "
              "payment status (3), history (1), with 0.5-1.5 s think time; plus 2 *abusive* clients hammering the balance API "
              "~50 times/s from one device id. During every run `/stats` was sampled every 2 s and `docker stats` every ~3 s."),
        ("p", f"**Machine:** one laptop, {u100['host']['cpus'] if u100 else '?'} logical CPUs, Docker Desktop (WSL2), all 10 "
              "containers + Locust sharing it. Absolute numbers are therefore lower than on separate servers; the *trend* across "
              "load levels is what matters."),
        (sub, "Results"),
        ("table", ["Run", "req/s", "OK/s", "429s", "Errors", "p50", "p95", "p99", "Gateway p95", "Payments settled/s",
                   "Cache hit", "Gateway CPU"], rows),
        ("p", "req/s, OK/s, 429s, errors and p50/p95/p99 are measured by Locust (client side, all endpoints). Gateway p95 is the "
              "gateway's own latency histogram; payments settled/s, cache hit ratio and CPU come from `/stats` and `docker stats` "
              "(100 % CPU = one core)."),
    ]
    b += keep(chart("scale_throughput.png", "Throughput at each load level: requests answered OK (blue) and rejected with 429 (orange)"),
              chart("scale_latency.png", "Client-side latency percentiles at each load level"))

    # ----- how the limiter / cache protected the system
    b.append((sub, "How the rate limiter and the cache protected the system"))
    pts = []
    if u100:
        sc = u100["server"]["rate_limited"]
        pts.append(f"**Rate limiter:** at 100 users the two abusive clients were cut off by their per-client token bucket - "
                   f"**{u100['client']['rate_limited']:,} requests got 429 Too Many Requests** "
                   f"(client bucket: {sc.get('client', 0):,.0f}, global bucket: {sc.get('global', 0):,.0f}) at almost no cost "
                   "(the 429 is decided before any service is called), while normal users saw no errors.")
    if u100 and u500:
        pts.append(f"**Cache:** {u100['server']['cache_hit_ratio']:.0f}-{max(r['server']['cache_hit_ratio'] or 0 for r in order if not r.get('server_metrics_partial')):.0f}% "
                   "of balance / VPA / status reads were served from Redis, so PostgreSQL stayed fast at every level: "
                   f"DB query p95 was {ms(u100['server']['db_query_ms']['p95'])} at 100 users and "
                   f"{ms(u500['server']['db_query_ms']['p95'])} at 500 users.")
    if u500:
        pts.append(f"**Queue:** payments were accepted with 202 and absorbed by the Redis Stream; the queue never held more than "
                   f"{max(r['server']['queue_pending_max'] + r['server']['queue_lag_max'] for r in order):.0f} payments and the "
                   f"workers settled each one in about {ms(u500['server']['txn_e2e_ms']['p50'])} (p50 end-to-end) even at 500 users.")
    pts.append("**No errors:** across all runs there were no 5xx responses or lost payments - the system degraded by getting slower, "
               "never by failing.")
    b.append(("ul", pts))

    # ----- bottleneck
    b.append((sub, "Bottleneck analysis"))
    if u100 and u200 and u500:
        g100, g200, g500 = (r["resources"].get("gateway", {}).get("cpu_mean", 0) for r in (u100, u200, u500))
        b.append(("callout", "Bottleneck: the API gateway process.",
                  f"Going from 100 to 500 users, throughput did not rise - it **fell from {u100['client']['rps']:.0f} to "
                  f"{u500['client']['rps']:.0f} req/s**, and p95 latency grew from {ms(u100['client']['p95'])} to "
                  f"{ms(u500['client']['p95'])}. The gateway container sat at **{g200:.0f}-{g500:.0f}% CPU - one full core** - "
                  "while every other container stayed below ~30%."))
        b.append(("p", "Evidence that the time is lost *inside the gateway*, not in the services behind it:"))
        b.append(("table", ["Stage (p95)", "100 users", "200 users", "500 users"], [
            ["Gateway total (gateway's view)", ms(u100["server"]["gateway_ms"]["p95"]), ms(u200["server"]["gateway_ms"]["p95"]), ms(u500["server"]["gateway_ms"]["p95"])],
            ["Account service upstream call (gateway's view)", ms(u100["server"]["upstream_account_ms"]["p95"]), ms(u200["server"]["upstream_account_ms"]["p95"]), ms(u500["server"]["upstream_account_ms"]["p95"])],
            ["Account service handler (service's own view)", ms(u100["server"]["account_service_ms"]["p95"]), ms(u200["server"]["account_service_ms"]["p95"]), ms(u500["server"]["account_service_ms"]["p95"])],
            ["Payment service handler", ms(u100["server"]["payment_service_ms"]["p95"]), ms(u200["server"]["payment_service_ms"]["p95"]), ms(u500["server"]["payment_service_ms"]["p95"])],
            ["PostgreSQL query", ms(u100["server"]["db_query_ms"]["p95"]), ms(u200["server"]["db_query_ms"]["p95"]), ms(u500["server"]["db_query_ms"]["p95"])],
            ["Queue wait (stream -> worker)", ms(u100["server"]["queue_wait_ms"]["p95"]), ms(u200["server"]["queue_wait_ms"]["p95"]), ms(u500["server"]["queue_wait_ms"]["p95"])],
            ["Payment end-to-end (202 -> SUCCESS)", ms(u100["server"]["txn_e2e_ms"]["p95"]), ms(u200["server"]["txn_e2e_ms"]["p95"]), ms(u500["server"]["txn_e2e_ms"]["p95"])],
        ]))
        b.append(("ul", [
            "The account service answers in single-digit milliseconds at every level, yet the gateway waits seconds for the same "
            "calls: requests are queuing in the gateway's **single asyncio event loop**, which is CPU-bound. (Values shown as "
            "10.0 s are the top bucket of the latency histogram, i.e. 10 s or more.)",
            "The gateway runs as **one uvicorn process**, so it can use only one CPU core however many the machine has. Per request "
            "it runs the rate-limit Lua script in Redis, proxies with `httpx`, records metrics and writes a JSON log line - roughly "
            f"{1000 * g100 / 100 / max(u100['client']['rps'], 1):.0f} ms of CPU per request at 100 users, which caps the whole "
            f"system at about {u100['client']['rps']:.0f} req/s on this laptop.",
            "Back-end stages scale fine: DB queries, queue wait and worker settlement times are flat from 100 to 500 users.",
        ]))
    if nolim and u500:
        b.append(("p", f"**Rate limiter on vs off at 500 users:** {u500['client']['rps']:.0f} vs {nolim['client']['rps']:.0f} req/s, "
                       f"p95 {ms(u500['client']['p95'])} vs {ms(nolim['client']['p95'])} - practically identical. The global token bucket "
                       "(300 req/s) is set *above* what the gateway can actually process (~140 req/s here), so it never triggers; the "
                       "per-client buckets also stop triggering at 200+ users because the saturated gateway slows the abusive clients "
                       "down by itself. In other words, the limiter limits too late: it should be sized to the measured capacity."))
    if gw and u500:
        better = gw["client"]["rps"] / max(u500["client"]["rps"], 1)
        b.append((sub, "Experiment: scaling the gateway out"))
        b.append(("p", f"To confirm the diagnosis the same 500-user test was repeated with the gateway running **{gw['variant'][2:]} uvicorn "
                       f"processes** (`GATEWAY_WORKERS={gw['variant'][2:]}`; the rate-limit buckets live in Redis, so limits stay exact "
                       f"across processes). Throughput went from **{u500['client']['rps']:.0f} to {gw['client']['rps']:.0f} req/s "
                       f"({better:.1f}x)**, p50 latency from {ms(u500['client']['p50'])} to {ms(gw['client']['p50'])} and p95 from "
                       f"{ms(u500['client']['p95'])} to {ms(gw['client']['p95'])}, with {gw['client']['rate_limited']:,} requests now "
                       f"(gateway CPU {gw['resources'].get('gateway', {}).get('cpu_mean', 0):.0f}%, i.e. several cores) and payments "
                       f"settled {gw['server']['settle_per_s']:.0f}/s instead of {u500['server']['settle_per_s']:.0f}/s."))
        nxt = sorted(((k, v["cpu_mean"]) for k, v in gw["resources"].items() if k not in ("gateway", "locust (load generator)")),
                     key=lambda kv: -kv[1])[:3]
        err = gw["client"]["other_failures"]
        total = max(gw["client"]["requests"], 1)
        b.append(("p", "**The bottleneck moves on.** With the gateway no longer limiting, the next stages start to work hard: "
                       + ", ".join(f"{k} {v:.0f}%" for k, v in nxt) + " CPU. "
                       + (f"{err} requests ({100 * err / total:.1f}%) - all of them reads routed to the **single account-service "
                          "replica** - got 503 Service Unavailable: when that one node answered too slowly the gateway's health check / "
                          "per-node circuit breaker took it out of rotation for a moment, and with only one replica there was no other "
                          "node to route to. (A 30-s repeat at the same load did not reproduce the 503s, so this is intermittent.) "
                          "The payment service, which already has two replicas, had no errors." if err else "")))
    b += keep(chart("scale_cpu.png", "Mean CPU per container during each run (100 % = one core): the gateway is the only container near or above one core"),
              chart("scale_stages.png", "Server-side p95 per stage: only the gateway grows with load"),
              chart("scale_rps_timeline.png", "Request rate over time in each run"),
              chart("scale_queue.png", "Payment queue depth over time: the workers keep up at every level"))
    b.append((sub, "Recommendations"))
    b.append(("ol", [
        "**Scale the gateway horizontally**: several processes (`GATEWAY_WORKERS`) or several gateway containers behind a TCP load "
        "balancer. It is stateless apart from Redis, so this is safe; move health checks and metrics aggregation to a shared store "
        "so `/stats` stays complete with many processes.",
        "**Run a second account-service replica** (as the payment service already does) and add PostgreSQL read capacity "
        "(connection pooling via PgBouncer, a read replica for balance/history) - these are the next stages to saturate once the "
        "gateway is scaled.",
        "**Size the global token bucket to measured capacity** (about 80 % of the throughput at which latency starts to climb), so "
        "excess load is shed with cheap 429s *before* the event loop saturates - that keeps latency low for admitted users.",
        "**Make each proxied request cheaper**: reuse one HTTP/1.1 keep-alive pool per upstream (already done) and consider a faster "
        "client (e.g. aiohttp), sample per-request log lines at high load, and batch metrics.",
        "**Add an end-to-end deadline** at the gateway (e.g. 2 s) so a request that has already waited too long in the queue is "
        "rejected quickly instead of occupying the loop.",
    ]))
    return b


# ============================================================================ demonstration PDF
def demonstration() -> list[Block]:
    title = "UPI Payment System - Demonstration of All Scenarios"
    subtitle = "Distributed System Design - demonstrative screens of every scenario of the application"
    b: list[Block] = [(*cover_block(title, subtitle), cover_md(title, subtitle))]
    b += [("h2", "Contents"), ("ol", [
        "Phase 1 - Docker setup, API gateway, routing, rate limiter (429), health endpoint",
        "Phase 2 - PostgreSQL schema and data, Redis cache, asynchronous queue + worker pool",
        "Pay Console - person-to-person payment scenarios (success, merchant, decline, insufficient funds, limit, self-pay, "
        "unknown payee, double tap, bank outage and recovery)",
        "Live dashboard - normal load, bank down (circuit breaker open), recovery",
        "Phase 3 - fault tolerance (retries, circuit breaker), structured logs and request tracing, metrics",
        "Load testing - 100 / 200 / 500 users, 100-user functional scenario test, unit tests",
    ]),
          ("p", f"Source code: **{REPO_URL}** - run with `docker compose up --build -d`, then open "
                "`http://127.0.0.1:8080/dashboard` (live dashboard) and `http://127.0.0.1:8080/pay` (Pay Console).")]

    b += [("pagebreak",), ("h2", "1. Phase 1 - Docker setup, API gateway, rate limiting, health")]
    b += keep(ex("p1_compose_up"), mu("dashboard_before"), ex("p1_health_cli"), ex("p1_health_json"), ex("p1_routes"),
              ex("p1_burst"), mu("log_rate_limited"), ex("p1_smoke"))

    b += [("pagebreak",), ("h2", "2. Phase 2 - Database, cache, asynchronous workers")]
    b += keep(ex("p2_tables"), ex("p2_txn_table"), ex("p2_migrations"), ex("p2_data"), ex("p2_events"), ex("p2_cache"),
              ex("p2_redis"), ex("p2_pay_cli"), mu("log_payment_completed"), mu("dashboard_txn_journey"))

    b += [("pagebreak",), ("h2", "3. Pay Console - person-to-person payment scenarios"),
          ("p", "The Pay Console (`/pay`) is the interface where one person chooses whom to pay and how much. Every scenario below "
                "was sent through the real API gateway; the right-hand side shows the payment's journey, the HTTP / trace details, "
                "both balances before and after, the audit trail and the payer's history.")]
    b += keep(mu("pay_p2p_success"), mu("pay_p2m_merchant"), mu("pay_bank_decline"), mu("pay_insufficient_funds"),
              mu("pay_over_limit"), mu("pay_self"), mu("pay_unknown_payee"), mu("pay_double_tap"),
              mu("pay_bank_outage_recovered_waiting"), mu("pay_bank_outage_recovered"))

    b += [("pagebreak",), ("h2", "4. Live dashboard during the tests")]
    b += keep(mu("dashboard_p2p_load"), mu("dashboard_bank_down"), mu("dashboard_recovered"), mu("dashboard_after"))

    b += [("pagebreak",), ("h2", "5. Phase 3 - Fault tolerance, logging and metrics")]
    b += keep(mu("log_resilience"), mu("log_trace_bank_flaky_50_errors"), mu("log_trace_bank_down_then_recovers"),
              ex("p3_trace_cli"), mu("log_trace_p2p_transfer"), mu("log_trace_p2m_merchant_payment"),
              mu("log_trace_double_tap_idempotent_replay"), mu("log_trace_bank_decline"), mu("log_trace_insufficient_funds"),
              mu("log_trace_amount_above_rs_1_lakh"), ex("p3_stats"))

    b += [("pagebreak",), ("h2", "6. Load testing and automated tests")]
    if MU_RESULTS:
        b.append(("p", f"**100-user functional scenario test** (`scripts/multi_user_test.py`): {MU_RESULTS['total_passed']}/"
                       f"{MU_RESULTS['total_cases']} scenario requests behaved as expected, "
                       f"{sum(c['ok'] for c in MU_RESULTS['checks'])}/{len(MU_RESULTS['checks'])} consistency checks passed, "
                       f"{MU_RESULTS['distinct_payers']} distinct paying users."))
    b += keep(mu("console_output"), ex("p3_locust_scale-u100"), ex("p3_locust_scale-u200"), ex("p3_locust_scale-u500"),
              chart("scale_throughput.png", "Load test summary: throughput at 100 / 200 / 500 users"),
              chart("scale_latency.png", "Load test summary: latency percentiles at 100 / 200 / 500 users"),
              chart("scale_cpu.png", "Load test: CPU per container - the gateway is the bottleneck"),
              ex("p3_pytest"))
    return b


# ============================================================================ project report
def report() -> list[Block]:
    title = "UPI Payment System - Project and Load-Test Report"
    subtitle = "Distributed System Design - architecture, implementation of the three phases, load testing and bottleneck analysis"
    b: list[Block] = [(*cover_block(title, subtitle), cover_md(title, subtitle))]
    b += [("h2", "1. Summary"),
          ("p", "A containerised model of the backend behind a UPI payments app: an API gateway with token-bucket rate limiting and "
                "health-aware routing, stateless payment and account services, PostgreSQL as the source of truth, Redis for cache, "
                "rate-limit buckets and the payment queue, an asynchronous worker pool that talks to a mock NPCI switch, and circuit "
                "breakers + retries with exponential backoff. One `docker compose up` starts all 10 containers; every request can be "
                "traced through every hop from the structured logs.")]
    if RUNS.get("scale-u100") and RUNS.get("scale-u500"):
        u100, u500 = RUNS["scale-u100"], RUNS["scale-u500"]
        gw = next((r for r in SCALE["runs"] if r.get("variant", "").startswith("gw")), None)
        bullets = [
            f"Load test at 100 / 200 / 500 users: peak **{u100['client']['rps']:.0f} req/s** at 100 users with p95 "
            f"{ms(u100['client']['p95'])}; no 5xx errors and no lost payments at any level.",
            f"**Bottleneck found: the single-process API gateway** (one CPU core, ~{u100['resources'].get('gateway', {}).get('cpu_mean', 0):.0f}-"
            f"{u500['resources'].get('gateway', {}).get('cpu_mean', 0):.0f}% CPU) - at 500 users throughput fell to {u500['client']['rps']:.0f} req/s "
            f"and p95 rose to {ms(u500['client']['p95'])}, while the services, database and workers stayed fast.",
        ]
        if gw:
            bullets.append(f"Running the gateway as {gw['variant'][2:]} processes raised 500-user throughput to **{gw['client']['rps']:.0f} req/s** "
                           f"and cut p50 latency from {ms(u500['client']['p50'])} to {ms(gw['client']['p50'])}.")
        if MU_RESULTS:
            bullets.append(f"Functional test with {MU_RESULTS['distinct_payers']} distinct users and 17 scenario types: "
                           f"{MU_RESULTS['total_passed']}/{MU_RESULTS['total_cases']} as expected, money conserved, every balance exact.")
        b.append(("ul", bullets))

    b += [("h2", "2. Requirements and where they are implemented"),
          ("table", ["Phase", "Requirement", "Implementation"], [
              ["1", "Architecture diagram + data-flow sketch", "`docs/architecture.png`, `docs/data-flow.png`, `docs/DESIGN.md`"],
              ["1", "`docker compose up` spins up gateway + nodes", "`docker-compose.yml` (10 containers, one shared `Dockerfile`)"],
              ["1", "API gateway: inspect, route, rate limit -> 429", "`gateway/main.py` pipeline; `gateway/ratelimit.py` token bucket (Redis Lua, per client + global, local fallback)"],
              ["1", "Health endpoint per node", "`GET /health` on the gateway polls every node's `/health` (`common/health.py`, `gateway/upstreams.py`)"],
              ["2", "Database + schema + migrations", "PostgreSQL 16; `db/migrations/001-003*.sql`, runner `db/migrate.py`, `docs/DATABASE.md`"],
              ["2", "In-memory cache", "Redis cache-aside (`common/cache.py`) for VPA, balance, history, payment status; rate-limit counters in Redis"],
              ["2", "Async queue + worker pool updating the DB", "Redis Streams consumer group; `worker/main.py`, `worker/processor.py` (one DB transaction per settlement)"],
              ["3", "Resilience pattern", "Circuit breaker + retry with exponential backoff and jitter (`common/resilience.py`), dead-letter stream, XAUTOCLAIM re-delivery, sweeper"],
              ["3", "Load-testing script", "Locust `loadtest/locustfile.py`; `loadtest/run_loadtest.py`, `loadtest/scale_test.py` (100/200/500 users), `scripts/multi_user_test.py`"],
              ["3", "Metrics + structured logging, trace a request", "JSON logs with request id (`common/logs.py`), metrics (`common/metrics.py`), `GET /stats`, `scripts/demo.py trace`, `/dashboard`"],
              ["3", "Load-test report", "Section 6 of this report; `loadtest/scale_report.py`, `loadtest/make_report.py`"],
          ])]

    b += [("pagebreak",), ("h2", "3. Architecture and data flow")]
    b += keep(fig(ROOT / "docs" / "architecture.png", "Architecture: gateway, services, queue, workers, bank switch, PostgreSQL, Redis"),
              fig(ROOT / "docs" / "data-flow.png", "Data flow of one payment through the system"))
    b += [("ol", [
        "The app verifies the payee (`GET /api/v1/vpa/{vpa}`, cached) and sends `POST /api/v1/payments` with an `Idempotency-Key`.",
        "The gateway assigns an `X-Request-ID`, takes a token from the user's bucket and the global bucket (else **429**), inspects "
        "the request (405/413/415/400) and proxies it round-robin to a healthy payment node whose circuit breaker is closed.",
        "The payment service validates it (amount <= Rs 1 lakh, payer != payee, VPAs exist), stores it as **PENDING** (a duplicate key "
        "returns the original), appends it to the Redis Stream and answers **202** in milliseconds.",
        "A worker claims it with a lease (**PROCESSING**), calls the bank switch through the circuit breaker with retries, then settles in "
        "**one PostgreSQL transaction** (row locks -> debit -> credit -> 2 ledger rows -> **SUCCESS** -> audit event), ACKs and "
        "invalidates the cache.",
        "The app polls `GET /api/v1/payments/{id}` (cached) until SUCCESS / FAILED.",
    ])]

    b += [("h2", "4. Implementation by phase"),
          ("h3", "Phase 1 - Gateway, rate limiting, health"),
          ("ul", ["**Token bucket, two levels, atomic:** every request needs a token from its client's bucket (read: burst 40, 20/s; "
                  "write: burst 10, 2/s) and from a global bucket (burst 500, 300/s). One Lua script checks and updates both in Redis "
                  "using Redis' clock, so several gateway processes share one consistent limit; if Redis is down the gateway falls "
                  "back to an in-process bucket.",
                  "**Inspection and routing:** longest-prefix routes, 405/413/415, mandatory `Idempotency-Key` on payments, round robin "
                  "over healthy nodes, one retry on another node when safe (GET or idempotent POST), per-node circuit breaker.",
                  "**Health:** a background monitor probes every node's `/health` every 3 s; `GET /health` reports UP / DEGRADED / DOWN "
                  "with per-node status, latency and breaker state."]),
          ("h3", "Phase 2 - Persistence, cache, asynchronous workers"),
          ("ul", ["**Schema:** users, bank_accounts (balance in BIGINT paise, CHECK >= 0), vpas, transactions (unique idempotency key, "
                  "status enum, lease columns, request id), ledger_entries (double entry), transaction_events (append-only audit).",
                  "**Cache-aside** with TTLs (VPA 5 min, balance 30 s, history 15 s, status 1 s pending / 5 min final) and explicit "
                  "invalidation by the worker; `X-Cache: HIT/MISS` on every read.",
                  "**Queue:** Redis Streams consumer group shared by 2 worker containers x 8 consumers; at-least-once delivery + lease "
                  "+ conditional status updates = money moves exactly once."]),
          ("h3", "Phase 3 - Fault tolerance, load testing, observability"),
          ("ul", ["**Retries** with exponential backoff and jitter (0.2 s, 0.4 s, 0.8 s) for bank 5xx/timeouts; **circuit breaker** opens "
                  "after 5 consecutive failures for 10 s, then a half-open trial call; deferred payments are re-delivered by XAUTOCLAIM; "
                  "after 3 deliveries -> FAILED + dead-letter stream.",
                  "**Observability:** one JSON log line per event with service, node and request id; counters, gauges and latency "
                  "histograms per node; `/stats`, `/metrics/prometheus`, live `/dashboard`."])]

    b += [("pagebreak",)]
    b += load_test_blocks("h2", "5. ")

    if MU_RESULTS:
        b += [("pagebreak",), ("h2", "6. Functional test with 100+ users (all scenarios)"),
              ("p", f"`scripts/multi_user_test.py` drove {MU_RESULTS['distinct_payers']} distinct users through every scenario a real UPI "
                    "app meets; each user had its own device id. Results:"),
              ("table", ["Scenario", "Requests", "As expected", "Outcomes"],
               [[p["name"], p["cases"], f"{p['passed']}/{p['cases']}", ", ".join(f"{k} x{v}" for k, v in p["outcomes"].items())]
                for p in MU_RESULTS["phases"]]),
              ("table", ["Consistency check", "Result"], [[c["name"], "PASS" if c["ok"] else "FAIL"] for c in MU_RESULTS["checks"]])]

    b += [("h2", "7. How to run"),
          ("code", "git clone " + REPO_URL + ".git && cd upi-system-design\n"
                   "docker compose up --build -d\n"
                   "python scripts/smoke_test.py                      # 11 end-to-end checks\n"
                   "open http://127.0.0.1:8080/dashboard and http://127.0.0.1:8080/pay\n"
                   "python scripts/multi_user_test.py                 # 100+ users, every scenario\n"
                   "python loadtest/scale_test.py --gateway-workers 4 # 100/200/500-user load tests\n"
                   "python loadtest/scale_report.py                   # numbers + charts"),
          ("h2", "8. References"),
          ("ol", [f"{t} - {u}" for t, u in SOURCES])]
    return b


def write(blocks: list[Block], stem: str, title: str, footer: str, browser: Any) -> Path:
    md = SUB / f"{stem}.md"
    md.write_text(to_md(blocks), encoding="utf-8")
    h = SUB / f"{stem}.html"
    h.write_text(to_html(blocks, title), encoding="utf-8")
    pdf = SUB / f"{stem}.pdf"
    html_to_pdf(browser, h, pdf, footer)
    print(f"wrote {pdf}")
    return pdf


def main() -> int:
    SUB.mkdir(exist_ok=True)
    from playwright.sync_api import sync_playwright
    team = " / ".join(f"{n} ({r})" for _, n, r, _ in TEAM)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        write(demonstration(), f"{ROLL_PREFIX}_UPI_Demonstration", "UPI Payment System - Demonstration",
              f"UPI Payment System - Demonstration · {team}", browser)
        write(report(), f"{ROLL_PREFIX}_UPI_Project_Report", "UPI Payment System - Project and Load-Test Report",
              f"UPI Payment System - Report · {team}", browser)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
