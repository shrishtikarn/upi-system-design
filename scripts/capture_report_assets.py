"""Run the multi-user test and capture every screenshot the report needs.

    pip install playwright          (uses the Microsoft Edge / Chrome already on the machine;
                                     no browser download needed)
    python scripts/capture_report_assets.py              # ~4 minutes
    python scripts/capture_report_assets.py --browser chrome

What it does:
  1. opens the live dashboard (/dashboard) in a headless browser
  2. starts scripts/multi_user_test.py and screenshots the dashboard at each phase
     (P2P load, bank down / circuit breaker open, recovered, final)
  3. drives the Pay console (/pay) through every person-to-person scenario and
     screenshots each result (success, merchant, decline, insufficient funds,
     limit, self-pay, unknown payee, double tap, bank outage + recovery)
  4. pulls `docker compose logs`, renders request traces, worker retry/circuit-breaker
     logs, gateway 429 logs and the test's console output as terminal-style pages,
     and screenshots them
  5. writes reports/multi_user_test/screenshots/*.png and manifest.json
     (then run scripts/make_multi_user_report.py)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
GATEWAY = os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").rstrip("/")
OUT = ROOT / "reports" / "multi_user_test"
SHOTS = OUT / "screenshots"
manifest: list[dict[str, str]] = []


def shot(page: Page, name: str, caption: str, section: str, full_page: bool = True) -> None:
    path = SHOTS / f"{len(manifest) + 1:02d}_{name}.png"
    page.screenshot(path=str(path), full_page=full_page)
    manifest.append({"file": f"screenshots/{path.name}", "caption": caption, "section": section})
    print(f"  [screenshot] {path.name} - {caption}", flush=True)


def set_bank(mode: str) -> None:
    req = urllib.request.Request(f"{GATEWAY}/dashboard/api/chaos", data=json.dumps({"mode": mode}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=5).read()


# --------------------------------------------------------------------------- terminal-style pages
ANSI = {"1": "font-weight:700", "2": "color:#8b949e", "31": "color:#ff7b72", "32": "color:#3fb950",
        "33": "color:#d29922", "36": "color:#79c0ff"}


def ansi_to_html(text: str) -> str:
    parts, open_spans = [], 0
    for chunk in re.split(r"(\x1b\[[0-9;]*m)", text):
        m = re.fullmatch(r"\x1b\[([0-9;]*)m", chunk)
        if not m:
            parts.append(html.escape(chunk))
            continue
        code = m.group(1)
        if code in ("", "0"):
            parts.append("</span>" * open_spans)
            open_spans = 0
        elif code in ANSI:
            parts.append(f'<span style="{ANSI[code]}">')
            open_spans += 1
    return "".join(parts) + "</span>" * open_spans


def terminal_page(title: str, command: str, body_html: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body {{ margin:0; background:#e9e7e2; font-family:"Segoe UI",system-ui,sans-serif; padding:18px; }}
.win {{ background:#0d1117; border-radius:10px; overflow:hidden; box-shadow:0 6px 24px rgba(0,0,0,.25); }}
.bar {{ background:#21262d; color:#c9d1d9; padding:8px 14px; font-size:13px; display:flex; gap:8px; align-items:center; }}
.bar i {{ width:11px; height:11px; border-radius:50%; display:inline-block; }}
pre {{ margin:0; padding:14px 16px; color:#c9d1d9; font:12.5px/1.5 "Cascadia Mono",Consolas,monospace; white-space:pre-wrap; word-break:break-word; }}
.cmd {{ color:#79c0ff; }}
table {{ border-collapse:collapse; width:100%; font:12px/1.45 "Cascadia Mono",Consolas,monospace; color:#c9d1d9; }}
td, th {{ padding:3px 10px; border-bottom:1px solid #21262d; vertical-align:top; text-align:left; }}
th {{ color:#8b949e; font-weight:600; }}
.WARNING {{ color:#d29922; }} .ERROR {{ color:#ff7b72; }} .INFO {{ color:#3fb950; }}
.node {{ color:#79c0ff; white-space:nowrap; }} .ev {{ color:#e6edf3; font-weight:600; white-space:nowrap; }} .dim {{ color:#8b949e; }}
</style></head><body><div class="win"><div class="bar"><i style="background:#ff5f56"></i><i style="background:#ffbd2e"></i>
<i style="background:#27c93f"></i>&nbsp; {html.escape(title)}</div><pre class="cmd">$ {html.escape(command)}</pre>{body_html}</div></body></html>"""


def log_table(events: list[dict[str, Any]], max_detail: int = 170) -> str:
    skip = {"ts", "level", "service", "node", "event", "request_id"}
    rows = []
    for e in events:
        detail = " ".join(f"{k}={v}" for k, v in e.items() if k not in skip and v not in (None, ""))
        rows.append(f'<tr><td class="dim">{html.escape(e.get("ts", "")[11:23])}</td>'
                    f'<td class="{html.escape(e.get("level", ""))}">{html.escape(e.get("level", ""))}</td>'
                    f'<td class="node">{html.escape(e.get("node", ""))}</td><td class="ev">{html.escape(e.get("event", ""))}</td>'
                    f'<td class="dim">{html.escape(detail[:max_detail])}</td></tr>')
    return ("<table><tr><th>time (UTC)</th><th>level</th><th>node</th><th>event</th><th>details</th></tr>"
            + "".join(rows) + "</table>")


def docker_logs(since: str) -> list[dict[str, Any]]:
    res = subprocess.run(["docker", "compose", "logs", "--no-color", "--no-log-prefix", "--since", since],
                         cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    events = []
    for line in res.stdout.splitlines():
        i = line.find("{")
        if i >= 0:
            try:
                events.append(json.loads(line[i:]))
            except ValueError:
                pass
    return sorted(events, key=lambda e: e.get("ts", ""))


def render(page: Page, html_text: str, name: str, caption: str, section: str, width: int = 1400) -> None:
    f = OUT / "_render" / f"{name}.html"
    f.parent.mkdir(exist_ok=True)
    f.write_text(html_text, encoding="utf-8")
    page.set_viewport_size({"width": width, "height": 400})
    page.goto(f.as_uri())
    shot(page, name, caption, section)


# --------------------------------------------------------------------------- pay console
def pay_scenario(page: Page, key: str, name: str, caption: str, before_pay=None, mid_shot: str | None = None) -> None:
    page.goto(f"{GATEWAY}/pay")
    page.wait_for_function("window.upiPay && document.querySelector('#people option')", timeout=15000)
    page.click(f'#scenarios [data-s="{key}"]')
    page.wait_for_timeout(900)
    if before_pay:
        before_pay()
    page.click("#pay-btn")
    if mid_shot:
        page.wait_for_function("document.querySelector('#st-process').classList.contains('live')", timeout=20000)
        page.wait_for_timeout(6000)
        shot(page, name + "_waiting", mid_shot, "Pay console")
        set_bank("normal")
    page.wait_for_selector("#result.show", timeout=120000)
    page.wait_for_timeout(1800)
    shot(page, name, caption, "Pay console")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--browser", default="msedge", choices=["msedge", "chrome", "chromium"])
    ap.add_argument("--users", type=int, default=100)
    args = ap.parse_args()
    SHOTS.mkdir(parents=True, exist_ok=True)
    for old in SHOTS.glob("*.png"):
        old.unlink()
    started_utc = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 5))

    with sync_playwright() as p:
        browser = p.chromium.launch(channel=None if args.browser == "chromium" else args.browser, headless=True)
        ctx = browser.new_context(viewport={"width": 1520, "height": 1000}, device_scale_factor=1)
        dash = ctx.new_page()
        aux = ctx.new_page()

        # ---------------------------------------------------------- docker ps
        ps = subprocess.run(["docker", "compose", "ps", "--format", "table {{.Name}}\t{{.Status}}\t{{.Ports}}"],
                            cwd=ROOT, capture_output=True, text=True).stdout
        render(aux, terminal_page("docker compose ps", "docker compose ps", f"<pre>{html.escape(ps)}</pre>"),
               "docker_ps", "All containers of the cluster running and healthy before the test", "Setup")

        dash.goto(f"{GATEWAY}/dashboard")
        dash.wait_for_timeout(5000)
        shot(dash, "dashboard_before", "Live dashboard before the test: every node UP, circuit breakers CLOSED", "Dashboard")

        # ---------------------------------------------------------- run the test, screenshot on phase markers
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "multi_user_test.py"), "--users", str(args.users)],
                                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", env=env)
        assert proc.stdout
        recovering = False
        for line in proc.stdout:
            sys.stdout.write("    | " + line)
            if "=== PHASE p2p" in line:
                dash.wait_for_timeout(3500)
                shot(dash, "dashboard_p2p_load", f"Dashboard while {args.users} users send P2P payments: traffic flowing "
                     "gateway -> payment nodes -> queue -> workers -> bank", "Dashboard")
            elif "=== PHASE chaos-down" in line:
                dash.wait_for_timeout(8000)
                shot(dash, "dashboard_bank_down", "Dashboard with the bank switch DOWN: worker circuit breakers OPEN, "
                     "payments waiting in the queue", "Dashboard")
            elif "=== PHASE chaos-recover" in line:
                recovering = True
            elif recovering and "as expected" in line:
                recovering = False
                dash.wait_for_timeout(2500)
                shot(dash, "dashboard_recovered", "Dashboard after the bank recovered: the deferred payments were "
                     "re-delivered and settled", "Dashboard")
        proc.wait()
        print(f"  multi_user_test exit code {proc.returncode}", flush=True)
        dash.wait_for_timeout(2500)
        shot(dash, "dashboard_after", "Dashboard after the whole test run (totals, latest transactions)", "Dashboard")
        # open the audit trail of the newest transaction
        try:
            dash.evaluate("document.querySelector('#txs tr.click').click()")
            dash.wait_for_timeout(2500)
            shot(dash, "dashboard_txn_journey", "Dashboard: clicking a transaction shows its journey from the audit log",
                 "Dashboard")
        except Exception as exc:  # noqa: BLE001
            print(f"  (could not open a transaction row: {exc})")

        # ---------------------------------------------------------- pay console scenarios
        pay = ctx.new_page()
        pay.set_viewport_size({"width": 1440, "height": 1000})
        pay_scenario(pay, "p2p", "pay_p2p_success", "Pay console - P2P: Alice pays Bob Rs 150, payee verified, SUCCESS, balances move")
        pay_scenario(pay, "p2m", "pay_p2m_merchant", "Pay console - P2M: Bob pays the merchant Chai Point")
        pay_scenario(pay, "decline", "pay_bank_decline", "Pay console - remitter bank declines (amount ending .13): FAILED, no money moves")
        pay_scenario(pay, "insufficient", "pay_insufficient_funds", "Pay console - payer cannot afford it: FAILED INSUFFICIENT_FUNDS")
        pay_scenario(pay, "limit", "pay_over_limit", "Pay console - Rs 1,50,000 is above the Rs 1 lakh P2P limit: rejected with 422")
        pay_scenario(pay, "self", "pay_self", "Pay console - paying yourself is rejected (SAME_PAYER_PAYEE)")
        pay_scenario(pay, "unknown", "pay_unknown_payee", "Pay console - unknown payee VPA: verification fails, 404")
        pay_scenario(pay, "double", "pay_double_tap", "Pay console - double tap: the same Idempotency-Key twice, one debit")
        pay_scenario(pay, "p2p", "pay_bank_outage_recovered",
                     "Pay console - payment sent while the bank was down finishes after the bank recovers",
                     before_pay=lambda: set_bank("down"),
                     mid_shot="Pay console - bank DOWN: the payment is accepted (202) and waits; the worker keeps retrying")

        # ---------------------------------------------------------- logs
        print("  collecting docker compose logs ...", flush=True)
        events = docker_logs(started_utc)
        results = json.loads((OUT / "results.json").read_text(encoding="utf-8"))
        console = (OUT / "console.log").read_text(encoding="utf-8")
        render(aux, terminal_page("multi_user_test.py - console output", f"python scripts/multi_user_test.py --users {args.users}",
                                  f"<pre>{ansi_to_html(console)}</pre>"),
               "console_output", "Console output of the 100-user scenario test", "Logs")

        wanted = ["P2P transfer", "P2M merchant payment", "Double tap (idempotent replay)", "Bank decline",
                  "Insufficient funds", "Bank flaky (50% errors)", "Bank down, then recovers", "Amount above Rs 1 lakh"]
        for scen in wanted:
            sample = results["samples"].get(scen)
            if not sample:
                continue
            trail = [e for e in events if e.get("request_id") == sample["request_id"]]
            if not trail:
                continue
            slug = re.sub(r"[^a-z0-9]+", "_", scen.lower()).strip("_")
            render(aux, terminal_page(f"Trace: {scen} - {sample['payer']} -> {sample['payee']} Rs {sample['amount']} ({sample['final']})",
                                      f"python scripts/demo.py trace {sample['request_id']}", log_table(trail)),
                   f"log_trace_{slug}", f"Structured logs of one '{scen}' request followed across every service "
                   f"(request_id {sample['request_id']})", "Logs")

        chaos_ev = [e for e in events if e.get("event") in ("bank.retry_scheduled", "circuit.state_changed", "payment.deferred",
                                                            "chaos.changed", "dashboard.chaos", "queue.dead_lettered",
                                                            "worker.reclaimed", "queue.reclaimed")]
        if chaos_ev:
            render(aux, terminal_page("Worker resilience logs (retries, circuit breaker, deferrals)",
                                      'docker compose logs worker-1 worker-2 bank-mock | grep -E "retry|circuit|deferred|chaos"',
                                      log_table(chaos_ev[:70])),
                   "log_resilience", "Retries with exponential backoff, circuit breaker OPEN / HALF_OPEN / CLOSED and "
                   "deferred payments during the bank outage", "Logs")
        rl = [e for e in events if e.get("event") == "gateway.request" and e.get("outcome") == "rate_limited"]
        if rl:
            render(aux, terminal_page("Gateway rate limiter (429 Too Many Requests)",
                                      'docker compose logs gateway | grep rate_limited', log_table(rl[:25])),
                   "log_rate_limited", "Gateway log lines for requests rejected by the per-client token bucket", "Logs")
        done = [e for e in events if e.get("event") == "payment.completed"]
        if done:
            render(aux, terminal_page("Workers settling payments", "docker compose logs worker-1 worker-2 | grep payment.completed",
                                      log_table(done[:40])),
                   "log_payment_completed", "Worker log lines for settled payments (SUCCESS / FAILED with reason)", "Logs")
        browser.close()

    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\n{len(manifest)} screenshots in {SHOTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
