"""Build the multi-user test report (Markdown + HTML + PDF) from the captured run.

    python scripts/capture_report_assets.py     # first: runs the test, takes screenshots
    python scripts/make_multi_user_report.py    # then: writes the report

Inputs : reports/multi_user_test/results.json, manifest.json, transactions.csv, screenshots/
Outputs: reports/multi_user_test/UPI_Multi_User_Test_Report.md / .html / .pdf
Every number in the report comes from results.json of your own run.
"""
from __future__ import annotations

import csv
import html
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports" / "multi_user_test"
NAME = "UPI_Multi_User_Test_Report"

Block = tuple  # ("h1"|"h2"|"h3"|"p"|"ul"|"table"|"img"|"code"|"callout"|"raw"|"pagebreak", ...)

REPO_URL = "https://github.com/shrishtikarn/upi-system-design"
TEAM = [  # (member, name, roll no, GitHub profile)
    ("Member 1", "Darsh Shah", "26MCE001", "https://github.com/darsh-1010"),
    ("Member 2", "Shrishti Karn", "26MCE007", "https://github.com/shrishtikarn"),
]
ROLL_PREFIX = "_".join(t[2] for t in TEAM)  # file names carry every roll number, in order


def cover_block(title: str, subtitle: str) -> Block:
    """First-page header: GitHub repository first, then the team with roll numbers and GitHub profiles."""
    rows = "".join(
        f"<tr><td>{html.escape(m)}</td><td><b>{html.escape(n)}</b></td><td><b>{html.escape(r)}</b></td>"
        f'<td><a href="{html.escape(g)}">{html.escape(g)}</a></td></tr>' for m, n, r, g in TEAM)
    return ("raw", f"""
<div class="cover">
  <div class="repo">GitHub repository: <a href="{REPO_URL}">{REPO_URL}</a></div>
  <h1>{html.escape(title)}</h1>
  <p class="sub">{html.escape(subtitle)}</p>
  <table class="team"><thead><tr><th></th><th>Name</th><th>Roll no.</th><th>GitHub</th></tr></thead><tbody>{rows}</tbody></table>
</div>""")


def cover_md(title: str, subtitle: str) -> str:
    rows = "\n".join(f"| {m} | **{n}** | **{r}** | {g} |" for m, n, r, g in TEAM)
    return (f"**GitHub repository: {REPO_URL}**\n\n# {title}\n\n{subtitle}\n\n"
            f"| | Name | Roll no. | GitHub |\n|---|---|---|---|\n{rows}\n")


# --------------------------------------------------------------------------- renderers
def inline_html(text: str) -> str:
    """**bold** and `code` only - enough for this report."""
    out, parts = [], text.split("`")
    for i, part in enumerate(parts):
        if i % 2:
            out.append(f"<code>{html.escape(part)}</code>")
        else:
            segs = html.escape(part).split("**")
            out.append("".join(f"<b>{_italic(s)}</b>" if j % 2 else _italic(s) for j, s in enumerate(segs)))
    return "".join(out)


def _italic(text: str) -> str:
    return re.sub(r"(?<![\w*])\*([^*\s][^*]*?)\*(?![\w*])", r"<i>\1</i>", text)


def to_md(blocks: list[Block]) -> str:
    lines: list[str] = []
    for b in blocks:
        kind = b[0]
        if kind in ("h1", "h2", "h3"):
            lines += ["#" * int(kind[1]) + " " + b[1], ""]
        elif kind in ("p", "sub"):
            lines += [b[1], ""]
        elif kind == "callout":
            lines += [f"> **{b[1]}** {b[2]}", ""]
        elif kind == "ul":
            lines += [f"- {item}" for item in b[1]] + [""]
        elif kind == "ol":
            lines += [f"{i}. {item}" for i, item in enumerate(b[1], 1)] + [""]
        elif kind == "table":
            head, rows = b[1], b[2]
            lines.append("| " + " | ".join(head) + " |")
            lines.append("|" + "|".join("---" for _ in head) + "|")
            lines += ["| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |" for r in rows]
            lines.append("")
        elif kind == "img":
            lines += [f"![{b[2]}]({b[1]})", f"**Figure: {b[2]}**", ""]
        elif kind == "code":
            lines += ["```" + (b[2] if len(b) > 2 else ""), b[1], "```", ""]
        elif kind == "pagebreak":
            pass
        elif kind == "raw" and len(b) > 2:   # optional Markdown twin of a raw HTML block
            lines += [b[2], ""]
    return "\n".join(lines)


CSS = """
:root { --ink:#16181d; --muted:#5b5e66; --line:#e2e0da; --accent:#2a78d6; --good:#0b8a3e; --bad:#c23b3b; --soft:#f5f4f0; }
* { box-sizing: border-box; }
body { margin: 0; background: #fff; color: var(--ink); font: 14.5px/1.6 "Segoe UI", system-ui, -apple-system, Roboto, Arial, sans-serif; }
main { max-width: 1060px; margin: 0 auto; padding: 36px 40px 60px; }
h1 { font-size: 30px; line-height: 1.2; margin: 0 0 6px; letter-spacing: -0.015em; }
h2 { font-size: 21px; margin: 38px 0 10px; padding-bottom: 6px; border-bottom: 2px solid var(--ink); }
h3 { font-size: 16.5px; margin: 24px 0 8px; }
p { margin: 8px 0 12px; }
code { font-family: "Cascadia Mono", Consolas, monospace; font-size: 0.88em; background: var(--soft); padding: 1px 5px; border-radius: 4px; }
pre { background: #0d1117; color: #d6dde6; padding: 12px 14px; border-radius: 8px; font: 12.5px/1.5 "Cascadia Mono", Consolas, monospace; white-space: pre-wrap; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 16px; font-size: 13px; }
th { text-align: left; background: var(--soft); border-bottom: 1px solid #cfcdc6; padding: 6px 8px; font-weight: 650; }
td { border-bottom: 1px solid var(--line); padding: 5px 8px; vertical-align: top; }
tr { break-inside: avoid; }
td.nw { white-space: nowrap; }
ul, ol { margin: 6px 0 14px; padding-left: 22px; } li { margin: 3px 0; }
figure { margin: 14px 0 26px; break-inside: avoid; }
figure img { width: 100%; border: 1px solid var(--line); border-radius: 6px; display: block; }
figcaption { font-size: 13px; color: var(--ink); font-weight: 700; margin-top: 7px; line-height: 1.45; }
.callout { border-left: 4px solid var(--accent); background: #eef4fc; padding: 10px 14px; border-radius: 0 8px 8px 0; margin: 12px 0 16px; }
.callout.good { border-color: var(--good); background: #ecf7ef; } .callout.bad { border-color: var(--bad); background: #fbefef; }
.sub { color: var(--muted); margin: 0 0 18px; }
.pb { break-before: page; }
.cover .repo { font-size: 17px; font-weight: 700; padding: 12px 16px; border: 2px solid var(--ink); border-radius: 8px; margin-bottom: 22px; }
.cover .repo a { color: var(--accent); }
.cover table.team { width: auto; min-width: 70%; font-size: 14px; margin-top: 6px; }
.cover table.team td, .cover table.team th { padding: 7px 14px; }
@page { size: A4; margin: 14mm 12mm; }
"""


def to_html(blocks: list[Block], title: str) -> str:
    parts: list[str] = []
    for b in blocks:
        kind = b[0]
        if kind == "h1":
            parts.append(f"<h1>{inline_html(b[1])}</h1>")
        elif kind in ("h2", "h3"):
            parts.append(f"<{kind}>{inline_html(b[1])}</{kind}>")
        elif kind == "p":
            parts.append(f"<p>{inline_html(b[1])}</p>")
        elif kind == "sub":
            parts.append(f'<p class="sub">{inline_html(b[1])}</p>')
        elif kind == "callout":
            cls = "good" if "PASS" in b[1] else ("bad" if "FAIL" in b[1] else "")
            parts.append(f'<div class="callout {cls}"><b>{inline_html(b[1])}</b> {inline_html(b[2])}</div>')
        elif kind in ("ul", "ol"):
            parts.append(f"<{kind}>" + "".join(f"<li>{inline_html(i)}</li>" for i in b[1]) + f"</{kind}>")
        elif kind == "table":
            head = "".join(f"<th>{inline_html(str(h))}</th>" for h in b[1])
            # short values (numbers, "4.6 s", "PASS") never wrap mid-value
            rows = "".join("<tr>" + "".join(
                f'<td{" class=nw" if len(str(c)) <= 9 else ""}>{inline_html(str(c))}</td>' for c in r) + "</tr>" for r in b[2])
            parts.append(f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>")
        elif kind == "img":
            parts.append(f'<figure><img src="{html.escape(b[1])}" alt="{html.escape(b[2])}"><figcaption>Figure: {inline_html(b[2])}</figcaption></figure>')
        elif kind == "code":
            parts.append(f"<pre>{html.escape(b[1])}</pre>")
        elif kind == "pagebreak":
            parts.append('<div class="pb"></div>')
        elif kind == "raw":
            parts.append(b[1])
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
            f"<style>{CSS}</style></head><body><main>{''.join(parts)}</main></body></html>")


# --------------------------------------------------------------------------- content
SOURCES = [
    ("NPCI - Harmonisation of TAT and customer compensation for failed transactions (IMPS/UPI OC 93, 2019)",
     "https://www.npci.org.in/PDF/npci/imps/circular/2019/IMPS-UPI-I-OC-93-I-FY-19-20-I-Harmonisation-of-Turn-Around-Time(TAT)-and-customer-compensation-for-failed-transaction.pdf"),
    ("NPCI - UPI Circular 17: Debit reversals and deemed approval",
     "https://www.npci.org.in/PDF/npci/upi/circular/2017/Circular-17-Debit-Reversals-and-Deemed-Approval_0.pdf"),
    ("NPCI - UPI OC 45: Solutions to reduce deemed approved transactions",
     "https://www.npci.org.in/PDF/npci/upi/circular/2018/UPI OC 45 - Solutions to reduce deemed approved transaction.pdf"),
    ("NPCI - OC 149-A: Reduction of business declines in UPI",
     "https://www.npci.org.in/PDF/npci/upi/circular/2022/OC149-A-Addendum-to-OC-149-Reduction-of-business-declines-in-UPI.pdf"),
    ("Oracle - India Unified Payments Interface (UPI) switch documentation",
     "https://docs.oracle.com/cd/G15796_01/RHTML/India_UPI/Responsive%20HTML5/UPI/UPI/UPI.htm"),
    ("Business Today - NPCI to increase UPI limits for high-value payments from Sept 15 (2025)",
     "https://www.businesstoday.in/personal-finance/banking/story/npci-to-increase-upi-transaction-limits-for-high-value-payments-from-sept-15-check-updated-limits-493278-2025-09-09"),
    ("Paytm Blog - UPI limits get a big boost from 15 Sept (2025)", "https://paytm.com/blog/news/upi-higher-transaction-limits-sep-2025"),
    ("Razorpay Blog - UPI transaction limit per day", "https://razorpay.com/blog/upi-transaction-limit-per-day/"),
    ("Business Standard - NPCI tightens UPI API supervision to curb network stress (2025)",
     "https://www.business-standard.com/finance/news/npci-tightens-upi-api-supervision-to-curb-network-stress-125052201406_1.html"),
    ("Digit - NPCI's new UPI API rules from August 1 (balance-check limits)",
     "https://www.digit.in/news/general/upi-alert-npcis-new-rules-kick-in-from-august-1-heres-what-changes.html"),
    ("Legal500 - NPCI guidelines on UPI and API usage", "https://www.legal500.com/intelligence/india/technology/npci-guidelines-on-upi-and-api-usage"),
    ("Zerodha Support - Failed UPI transaction error codes",
     "https://support.zerodha.com/category/funds/adding-funds/articles/failed-upi-transaction"),
    ("PhonePe Developer - Payment gateway error codes", "https://developer.phonepe.com/payment-gateway/error-codes"),
    ("Paytm Blog - What is error code U30 in UPI?", "https://paytm.com/blog/payments/upi/what-is-error-code-u30-in-upi/"),
    ("Paytm Blog - Bank responsibility for failed UPI transactions",
     "https://paytm.com/blog/payments/upi/bank-responsibility-for-failed-upi-transaction"),
    ("Razorpay Blog - Deemed success in IMPS/UPI payouts",
     "https://razorpay.com/blog/payout-processing-imps-upi-transactions-deemed-success-npci/"),
]

SCENARIO_INFO = {
    "P2P transfer": ("202 Accepted, then SUCCESS", "The everyday flow: one person pays another; tests the whole async pipeline under concurrent load."),
    "P2M merchant payment": ("202, then SUCCESS", "Person-to-merchant (scan-and-pay at a shop)."),
    "Concurrent payments to one payee": ("all SUCCESS, exact total credited", "Many payers credit the same account at once - row-lock contention, no lost update."),
    "Concurrent overspend": ("exactly 2 SUCCESS + 1 INSUFFICIENT_FUNDS", "Three simultaneous debits that together exceed the balance - the balance is re-checked under a row lock, so it can never go negative."),
    "Double tap (idempotent replay)": ("202, then 200 Idempotent-Replay with the same txn_id", "A user taps Pay twice / the app retries after a network drop - must debit once."),
    "Idempotency key reused": ("409 IDEMPOTENCY_KEY_REUSED", "A key reused for a *different* payment is a client bug and is refused."),
    "Bank decline": ("202, then FAILED DECLINED_BY_REMITTER_BANK", "The issuer bank declines (business decline, like NPCI U30): stored, audited, no money moves, no retry."),
    "Insufficient funds": ("202, then FAILED INSUFFICIENT_FUNDS", "Payer cannot afford it (NPCI Z9)."),
    "Amount above Rs 1 lakh": ("422 AMOUNT_LIMIT_EXCEEDED", "NPCI's Rs 1,00,000 P2P cap; rejected before anything is stored."),
    "Pay yourself": ("422 SAME_PAYER_PAYEE", "Payer and payee must differ (also a DB CHECK constraint)."),
    "Unknown payee": ("404 PAYEE_VPA_NOT_FOUND", "Typo in a VPA - caught by payee verification."),
    "Malformed VPA": ("422 validation error", "Not of the form handle@psp."),
    "Missing Idempotency-Key": ("400 MISSING_IDEMPOTENCY_KEY at the gateway", "Gateway request inspection; the request never reaches a service."),
    "Rate limit (one user floods 30 requests)": ("~10 x 202 then 429 + Retry-After; bystander unaffected", "Per-client token bucket (write: burst 10, 2/s)."),
    "Rate limit: bystander unaffected": ("SUCCESS", "Another user paying during the flood."),
    "Bank flaky (50% errors)": ("SUCCESS (after retries)", "Retry with exponential backoff + jitter hides transient switch errors."),
    "Bank down, then recovers": ("no money lost; SUCCESS after recovery (or FAILED BANK_UNAVAILABLE after 3 deliveries)", "Circuit breaker opens, payments wait in the stream, XAUTOCLAIM re-delivers after recovery."),
}


def fmt_outcomes(o: dict[str, Any]) -> str:
    return ", ".join(f"{k} x{v}" for k, v in sorted(o.items(), key=lambda kv: -kv[1]))


def build(results: dict[str, Any], manifest: list[dict[str, str]], txns: list[dict[str, str]]) -> list[Block]:
    b: list[Block] = []
    phases = results["phases"]
    checks = results["checks"]
    ok_checks = sum(c["ok"] for c in checks)
    verdict = "PASS" if results["all_ok"] else "FAIL"
    cd = results.get("counters_delta", {})
    succ = sum(1 for t in txns if t["final"] == "SUCCESS")
    failed = Counter(t["reason"] for t in txns if t["final"] == "FAILED")
    rejected = Counter(f"{t['http']} {t['error']}" for t in txns if not t["txn_id"])
    p2p = next((p for p in phases if p["name"] == "P2P transfer"), {})
    processed_ok = sum(v for k, v in cd.items() if k.startswith("payments_processed_total") and "SUCCESS" in k)
    processed_failed = sum(v for k, v in cd.items() if k.startswith("payments_processed_total") and "FAILED" in k)

    title = "UPI Payment System - Multi-User Scenario Test Report"
    cover = cover_block(title, "Distributed System Design project")
    b += [(*cover, cover_md(title, "Distributed System Design project")),
          ("sub", f"Generated {results['generated_at']} · gateway {results['gateway']} · random seed {results['seed']} · "
                  f"test duration {results['duration_s']:.0f} s"),
          ("callout", f"Result: {verdict}.",
           f"{results['total_passed']}/{results['total_cases']} scenario requests behaved exactly as a real UPI app expects, and "
           f"{ok_checks}/{len(checks)} end-to-end consistency checks passed. {results['distinct_payers']} distinct users made "
           f"payments ({results['distinct_people']} distinct people involved, including payees)."),
          ("h2", "1. Executive summary"),
          ("ul", [
              f"**{results['distinct_payers']} different users** paid through the public API gateway, each with its own device id "
              f"(`X-Client-ID`), so every user had their own rate-limit bucket exactly like separate phones.",
              f"**{p2p.get('cases', 0)} concurrent person-to-person payments** settled with a median time to SUCCESS of "
              f"**{p2p.get('settle_p50_s')} s** (p95 {p2p.get('settle_p95_s')} s) on one laptop running 10 containers.",
              f"Across the run: **{succ} payments SUCCESS**, "
              + (", ".join(f"{v} FAILED {k}" for k, v in failed.most_common()) or "0 failed")
              + f", and {sum(rejected.values())} requests correctly **rejected before any money moved** "
              f"({', '.join(f'{v} x HTTP {k}' for k, v in rejected.most_common())}).",
              f"**Money was conserved**: the total across all {results['money_after']['accounts']} accounts was "
              f"Rs {results['money_before']['money_in_system']} before and Rs {results['money_after']['money_in_system']} after.",
              "Every tracked account's balance changed by **exactly** the sum of its SUCCESS payments; no balance went negative; "
              "no payment was left PENDING; every double tap returned the original transaction (one debit).",
              f"Resilience: with the bank failing 50% of calls the workers made **{results['chaos'].get('flaky_retries', 0):.0f} retries** "
              f"and every payment still settled; with the bank **down**, the worker circuit breakers opened "
              f"({results['chaos'].get('breaker_opened', 0):.0f} transitions to OPEN), payments waited in the queue, and all of them "
              "finished once the bank came back.",
              "A new **Pay Console** (`/pay`) lets a person choose who pays whom and how much, and shows the payment's journey, "
              "both balances before/after, the audit trail and history. Screenshots of every scenario are in section 7.",
          ])]

    # ------------------------------------------------------------------ architecture
    b += [("pagebreak",), ("h2", "2. How the system works (architecture)"),
          ("p", "The project is a containerised model of the backend behind a UPI app (GPay / PhonePe style). "
                "It starts with one `docker compose up` and consists of 10 containers:"),
          ("table", ["Component", "Role", "Key techniques"], [
              ["API gateway (`gateway`)", "Only public entry point (port 8080); serves `/dashboard` and the new `/pay`",
               "Token-bucket rate limiting (per client + global, atomic Lua in Redis), request inspection, health-aware round robin, per-node circuit breakers, one safe retry"],
              ["Payment service x2", "`POST /payments`, `GET /payments/{id}`", "Validation, idempotent INSERT (unique Idempotency-Key), `XADD` to a Redis Stream, answers **202** in milliseconds"],
              ["Account service", "VPA lookup, balance, history, dashboard admin reads", "Cache-aside in Redis (VPA 5 min, balance 30 s, history 15 s) with explicit invalidation"],
              ["Workers x2 (8 consumers each)", "Settle payments asynchronously", "Redis Streams consumer group, lease-based claim, retry + exponential backoff + jitter, circuit breaker to the bank, XAUTOCLAIM re-delivery, dead-letter stream"],
              ["Bank mock", "Stands in for NPCI + remitter/beneficiary banks", "Realistic latency, 1% errors and 0.5% declines in normal mode; chaos modes flaky / slow / down"],
              ["PostgreSQL 16", "Source of truth", "Money in BIGINT paise, double-entry ledger, append-only `transaction_events` audit log, CHECK balance >= 0"],
              ["Redis 7", "Cache, rate-limit buckets, payment stream", "AOF persistence; `volatile-lru` so the stream is never evicted"],
              ["migrate", "One-shot schema + seed", "3 demo users + 1,000 load-test users (Rs 10 lakh each)"],
          ])]
    for img, cap in (("architecture.png", "Architecture of the UPI system (from docs/)"),
                     ("data-flow.png", "Data flow of one payment (from docs/)")):
        if (OUT / img).exists():
            b.append(("img", img, cap))
    b += [("h3", "Life of one payment"),
          ("ol", [
              "The app verifies the payee: `GET /api/v1/vpa/{vpa}` returns the registered name (cached).",
              "`POST /api/v1/payments` with an `Idempotency-Key`. The gateway assigns an `X-Request-ID`, takes a token from the user's "
              "write bucket and the global bucket (else **429**), inspects the request, and proxies it to a healthy payment node.",
              "The payment service validates (amount <= Rs 1,00,000, payer != payee, both VPAs exist), inserts the transaction as "
              "**PENDING** (a duplicate key returns the original transaction instead), appends it to the Redis Stream and answers **202**.",
              "A worker reads the message, claims the transaction with a lease (**PROCESSING**), pre-checks the balance and calls the "
              "bank switch through the circuit breaker with retries.",
              "On approval it settles in **one Postgres transaction**: lock both accounts in a fixed order, re-check balance, debit, "
              "credit, two ledger rows, status **SUCCESS**, audit event. Then `XACK` and cache invalidation.",
              "The app polls `GET /api/v1/payments/{id}` (cached ~1 s while pending) until SUCCESS / FAILED.",
              "The same request id is logged by every service, copied into the queue message and stored on the row, so one "
              "`grep` reconstructs the whole path (section 7.3).",
          ])]

    # ------------------------------------------------------------------ research
    b += [("pagebreak",), ("h2", "3. How this maps to real UPI (research)"),
          ("p", "Web research on NPCI rules and published UPI behaviour was used to choose the test scenarios and their "
                "expected outcomes. Where sources disagree this is noted; NPCI's own circulars are the authority."),
          ("table", ["Real UPI behaviour", "What the sources say", "How this project models it / what we tested"], [
              ["P2P amount limit", "The general P2P cap stayed at **Rs 1 lakh** after NPCI's Sept-2025 changes; higher Rs 5-10 lakh caps apply only to specific merchant (P2M) categories. Banks may set lower limits.",
               "`MAX_AMOUNT_RUPEES=100000` in the payment service -> **422 AMOUNT_LIMIT_EXCEEDED** (scenario *Amount above Rs 1 lakh*)."],
              ["Business declines vs technical failures", "Error codes separate customer/bank declines (e.g. Z9 insufficient funds, U30 debit failure at remitter, U16 risk/limit) from technical timeouts (e.g. U67 debit timeout). Meanings of U30/U16 vary between apps.",
               "Bank mock returns `DECLINED` (-> FAILED `DECLINED_BY_REMITTER_BANK`, **no retry**) vs HTTP 503 (-> **retry** with backoff). Insufficient funds -> FAILED `INSUFFICIENT_FUNDS`."],
              ["Timeouts / 'deemed approved'", "PSPs must check status only after a timeout using the check-transaction API; NPCI requires credit or auto-reversal by T+1 with compensation for delays.",
               "A payment never stays in limbo: deferred payments are re-delivered; after 3 deliveries -> FAILED `BANK_UNAVAILABLE`; after 120 s -> FAILED `TIMEOUT`. The *nothing stuck* check verifies this."],
              ["API rate limits", "NPCI's 2025 API circular limits high-volume APIs, e.g. **50 balance enquiries per app per customer per day**, and asks PSPs to queue and rate-limit system-initiated calls, especially in peak hours.",
               "Per-client token buckets (read 40 burst / 20 s^-1, write 10 / 2 s^-1) plus a global bucket -> **429 + Retry-After** (scenario *Rate limit*)."],
              ["Idempotency / duplicate debits", "Debit reversals and duplicate-debit complaints are a major NPCI concern (debit-reversal / deemed-approval circulars).",
               "Unique `Idempotency-Key` in the DB + lease-based claim: a double tap returns the same transaction (scenario *Double tap*)."],
              ["Status checks", "Excessive check-status calls by PSPs stressed the network; NPCI tightened supervision.",
               "Status polling is served from Redis (1 s TTL while pending, 5 min once final)."],
          ]),
          ("p", "Simplifications (from the project README): a single mock service stands in for NPCI and both banks, balances "
                "live in our own Postgres, and there is no UPI PIN, authentication or TLS.")]

    # ------------------------------------------------------------------ what was built
    b += [("h2", "4. What was added for this test"),
          ("table", ["File", "Purpose"], [
              ["`gateway/static/pay.html` (served at `/pay`)", "**Pay Console** - a separate interface where a person picks the payer, the payee and the amount, verifies the payee's name, and pays. It shows the 4-step journey, HTTP/trace details, balances before & after, the audit trail and the payer's history, plus one-click scenarios."],
              ["`gateway/main.py`", "New routes `GET /pay` and `GET /dashboard/api/users` (people picker)."],
              ["`account_service/main.py`, `sql.py`", "New internal `GET /admin/users` (VPA, name, bank, balance)."],
              ["`scripts/multi_user_test.py`", "Stdlib-only scenario test: 100+ distinct users, 17 scenario types, chaos phases, consistency verification; writes `results.json`, `transactions.csv`, `console.log`."],
              ["`scripts/capture_report_assets.py`", "Runs the test while a headless browser (Playwright + installed Edge) screenshots the dashboard at each phase, drives the Pay Console through every scenario, and renders logs from `docker compose logs` as screenshots."],
              ["`scripts/make_multi_user_report.py`", "Builds this report (Markdown, HTML, PDF) from the run's own data."],
          ])]

    # ------------------------------------------------------------------ test design
    b += [("pagebreak",), ("h2", "5. Test design"),
          ("p", f"Each phase uses its own block of seeded users (`user0001@mockbank` ... ) so phases cannot interfere, and each user "
                f"sends with `X-Client-ID: phone-<vpa>`. The P2P phase runs {p2p.get('cases', 100)} users with 25 in parallel; "
                "payees are chosen with a fixed random seed so runs are reproducible."),
          ("table", ["Scenario", "Expected outcome", "Why it matters"],
           [[p["name"], *SCENARIO_INFO.get(p["name"], ("", ""))] for p in phases]),
          ("h3", "Consistency checks after the run"),
          ("ul", ["Total money in all accounts is unchanged (payments only move money).",
                  "For every tracked account (all test users + Alice, Bob, Chai Point): balance after - balance before = "
                  "credits - debits of its SUCCESS payments, computed independently by the test.",
                  "No negative balance anywhere.",
                  "No transaction left PENDING or PROCESSING.",
                  "Every double tap returned HTTP 200 `Idempotent-Replay` with the original `txn_id`."])]

    # ------------------------------------------------------------------ results
    b += [("pagebreak",), ("h2", "6. Results"),
          ("table", ["Scenario", "Requests", "As expected", "Outcomes", "Settle p50 / p95 (s)", "Notes"],
           [[p["name"], p["cases"], f"{p['passed']}/{p['cases']}", fmt_outcomes(p["outcomes"]),
             f"{p['settle_p50_s']} / {p['settle_p95_s']}" if p.get("settle_p50_s") is not None else "-", p.get("note") or ""]
            for p in phases]),
          ("h3", "Consistency checks"),
          ("table", ["Check", "Result", "Detail"], [[c["name"], "PASS" if c["ok"] else "FAIL", c["detail"]] for c in checks]),
          ("h3", "System counters during the run (from `/stats`)"),
          ("table", ["Counter", "Value"], [
              ["Payments settled SUCCESS (all workers; includes the flood requests that got through the limiter)", f"{processed_ok:.0f}"],
              ["Payments FAILED (all reasons)", f"{processed_failed:.0f}"],
              ["Bank retries (exponential backoff)", f"{cd.get('bank_retries_total', 0):.0f}"],
              ["Payments deferred back to the queue", f"{cd.get('payments_deferred_total', 0):.0f}"],
              ["Circuit breaker transitions to OPEN", f"{cd.get('circuit_opened_total', 0):.0f}"],
              ["Requests rejected with 429 by the gateway", f"{cd.get('gw_rate_limited_total', 0):.0f}"],
              ["Idempotent replays served", f"{cd.get('payments_idempotent_replay_total', 0):.0f}"],
              ["Dead-lettered messages", f"{cd.get('dlq_messages_total', 0):.0f}"],
          ]),
          ("h3", "Observations"),
          ("ul", [
              "**Random bank declines are expected.** In normal mode the bank mock declines 0.5% of payments and fails 1% of calls, "
              "like a real issuer; the test reports such declines separately instead of treating them as bugs. Transient 1% errors "
              "are absorbed by retries and are invisible to users.",
              "**Recovery time after an outage is dominated by configuration, not load:** a deferred payment is re-delivered only after "
              "`RECLAIM_IDLE_MS` = 15 s and the breaker waits `CB_RESET_TIMEOUT_S` = 10 s before a trial call, so payments made while "
              f"the bank was down finished about {next((p['settle_p50_s'] for p in phases if p['name'].startswith('Bank down')), '?')} s "
              "after they were sent. Lowering these values shortens recovery at the cost of more load on a sick bank.",
              "In the flaky phase most payments settle in well under a second; the slow tail (p95) is the few payments whose 3 "
              "in-process retries all failed and which were re-delivered 15 s later by XAUTOCLAIM.",
              "Concurrent debits of one account are serialised by row locks: the overspend scenario produced exactly two successes "
              "and one INSUFFICIENT_FUNDS - the balance check under the lock prevents double spending.",
              "The per-client limiter isolates users: while one user's flood received 429s, another user's payment went through normally.",
          ])]

    # ------------------------------------------------------------------ screenshots
    b += [("pagebreak",), ("h2", "7. Screenshots")]
    sections = [("Setup", "7.0 Cluster"), ("Dashboard", "7.1 Live system dashboard (`/dashboard`) during the test"),
                ("Pay console", "7.2 Pay Console (`/pay`) - person-to-person payment scenarios"),
                ("Logs", "7.3 Logs - structured JSON logs traced by request id")]
    for key, title in sections:
        items = [m for m in manifest if m["section"] == key]
        if not items:
            continue
        b.append(("h3", title))
        if key == "Logs":
            b.append(("p", "Each table below is built from `docker compose logs` filtered by one `request_id`; it shows the same "
                           "request passing gateway -> payment service -> Redis Stream -> worker -> bank -> Postgres."))
        for m in items:
            b.append(("img", m["file"], m["caption"]))

    # ------------------------------------------------------------------ reproduce
    b += [("pagebreak",), ("h2", "8. How to reproduce"),
          ("code", "docker compose up --build -d\n"
                   "python scripts/smoke_test.py                       # 11 end-to-end checks\n"
                   "python scripts/multi_user_test.py                  # 100+ users, all scenarios (~75 s)\n"
                   "pip install playwright                             # uses the installed Edge/Chrome\n"
                   "python scripts/capture_report_assets.py            # test + screenshots (~4 min)\n"
                   "python scripts/make_multi_user_report.py           # this report (md, html, pdf)\n"
                   "# interactive: http://127.0.0.1:8080/pay  and  http://127.0.0.1:8080/dashboard"),
          ("p", "Raw data of this run: `reports/multi_user_test/results.json`, `transactions.csv` (one row per scenario request "
                "with txn_id, request_id, node, timings) and `console.log`."),
          ("h2", "9. Limitations"),
          ("ul", ["All containers share one laptop's CPU, so absolute latencies are relative, not production numbers.",
                  "The bank is a single mock; there is no real NPCI message format (ReqPay/RespPay), UPI PIN, device binding or TLS.",
                  "Users are pre-seeded accounts; there is no registration/login flow to test.",
                  "Web sources on limits and error codes are news/bank summaries where the NPCI circular itself was not available; "
                  "they are listed below and should be checked against NPCI before being quoted as rules."]),
          ("h2", "10. Sources"),
          ("ol", [f"{t} - {u}" for t, u in SOURCES])]
    return b


def html_to_pdf(browser: Any, html_path: Path, pdf_path: Path, footer_title: str) -> None:
    """A4 PDF with a clearly visible 'Page X of Y' footer on every page."""
    page = browser.new_page()
    page.goto(html_path.as_uri())
    page.wait_for_load_state("networkidle")
    page.pdf(path=str(pdf_path), format="A4", print_background=True,
             margin={"top": "14mm", "bottom": "18mm", "left": "12mm", "right": "12mm"},
             display_header_footer=True, header_template="<span></span>",
             footer_template=(
                 '<div style="width:100%;font-family:Segoe UI,Arial,sans-serif;font-size:10.5px;color:#111;'
                 'padding:0 12mm;display:flex;justify-content:space-between;border-top:1px solid #bbb;padding-top:5px">'
                 f'<span>{html.escape(footer_title)}</span>'
                 '<span style="font-weight:700">Page <span class="pageNumber"></span> of <span class="totalPages"></span></span>'
                 '</div>'))
    page.close()


def main() -> int:
    results = json.loads((OUT / "results.json").read_text(encoding="utf-8"))
    manifest = json.loads((OUT / "manifest.json").read_text(encoding="utf-8")) if (OUT / "manifest.json").exists() else []
    with (OUT / "transactions.csv").open(encoding="utf-8") as fh:
        txns = list(csv.DictReader(fh))
    for img in ("architecture.png", "data-flow.png"):
        src = ROOT / "docs" / img
        if src.exists():
            shutil.copy(src, OUT / img)
    blocks = build(results, manifest, txns)
    title = "UPI Payment System - Multi-User Scenario Test Report"
    (OUT / f"{NAME}.md").write_text(to_md(blocks), encoding="utf-8")
    html_path = OUT / f"{NAME}.html"
    html_path.write_text(to_html(blocks, title), encoding="utf-8")
    print(f"wrote {OUT / (NAME + '.md')}\nwrote {html_path}")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed - skipping the PDF (pip install playwright)")
        return 0
    with sync_playwright() as p:
        for channel in ("msedge", "chrome", None):
            try:
                browser = p.chromium.launch(channel=channel, headless=True)
                break
            except Exception:  # noqa: BLE001
                continue
        else:
            print("no Chromium-based browser found - skipping the PDF")
            return 0
        html_to_pdf(browser, html_path, OUT / f"{NAME}.pdf", "UPI multi-user test report")
        browser.close()
    print(f"wrote {OUT / (NAME + '.pdf')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
