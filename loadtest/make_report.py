"""Build the load-test report from the runs saved by run_loadtest.py.

    python loadtest/make_report.py
    -> loadtest/results/LOAD_TEST_REPORT.md  (+ charts in loadtest/results/charts/ if matplotlib is installed)

Every number in the report is computed from files in loadtest/results/<run>/:
  * Locust CSVs                -> client-side throughput and latency
  * stats_before/after.json    -> server-side counters diffed for exactly this run
                                  (429s by bucket, cache hits, DB queries, payments processed,
                                   breaker openings, gateway latency of accepted requests)
  * timeline.json              -> queue depth / breaker state over time
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from stats_util import counter_breakdown, counter_sum, histogram_diff, histogram_merge, quantile, snapshots  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
RUN_ORDER = ["baseline", "spike", "spike-no-limiter", "spike-no-cache", "chaos"]


# --------------------------------------------------------------------------- loading
def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _diff_counter(before: dict, after: dict, service: str, name: str, **labels: str) -> float:
    return max(0.0, counter_sum(snapshots(after, service), name, **labels)
               - counter_sum(snapshots(before, service), name, **labels))


def _diff_breakdown(before: dict, after: dict, service: str, name: str, by: str, **labels: str) -> dict[str, float]:
    a = counter_breakdown(snapshots(after, service), name, by, **labels)
    b = counter_breakdown(snapshots(before, service), name, by, **labels)
    return {k: max(0.0, v - b.get(k, 0)) for k, v in a.items() if v - b.get(k, 0) > 0}


def _diff_hist(before: dict, after: dict, service: str, name: str, **labels: str) -> dict | None:
    return histogram_diff(histogram_merge(snapshots(after, service), name, **labels),
                          histogram_merge(snapshots(before, service), name, **labels))


def analyze(run_dir: Path) -> dict[str, Any] | None:
    meta_path = run_dir / "meta.json"
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text())
    before = json.loads((run_dir / "stats_before.json").read_text())
    after = json.loads((run_dir / "stats_after.json").read_text())
    timeline = json.loads((run_dir / "timeline.json").read_text()) if (run_dir / "timeline.json").exists() else {}

    stats_rows = _read_csv(run_dir / "locust_stats.csv")
    failures = _read_csv(run_dir / "locust_failures.csv")
    history = _read_csv(run_dir / "locust_stats_history.csv")
    agg = next((r for r in stats_rows if r.get("Name") == "Aggregated"), {})
    endpoints = [r for r in stats_rows if r.get("Name") != "Aggregated"]
    agg_history = [r for r in history if r.get("Name") == "Aggregated"]

    f429 = sum(int(_num(f.get("Occurrences"))) for f in failures if "429" in f.get("Error", ""))
    f_other = sum(int(_num(f.get("Occurrences"))) for f in failures if "429" not in f.get("Error", ""))

    gw_total = _diff_counter(before, after, "gateway", "gw_requests_total")
    outcomes = _diff_breakdown(before, after, "gateway", "gw_requests_total", "outcome")
    proxied_status = _diff_breakdown(before, after, "gateway", "gw_requests_total", "status", outcome="proxied")
    rl_scope = _diff_breakdown(before, after, "gateway", "gw_rate_limited_total", "scope")
    accepted_hist = _diff_hist(before, after, "gateway", "gw_latency_ms", outcome="proxied")
    rejected_hist = _diff_hist(before, after, "gateway", "gw_latency_ms", outcome="rate_limited")

    cache: dict[str, float] = {}
    db_queries = 0.0
    for service in ("payment-service", "account-service"):
        for result, n in _diff_breakdown(before, after, service, "cache_requests_total", "result").items():
            cache[result] = cache.get(result, 0) + n
        db_queries += _diff_counter(before, after, service, "db_queries_total")
    lookups = cache.get("hit", 0) + cache.get("miss", 0)

    processed = _diff_breakdown(before, after, "worker", "payments_processed_total", "status")
    fail_reasons = _diff_breakdown(before, after, "worker", "payments_processed_total", "reason", status="FAILED")
    e2e = _diff_hist(before, after, "worker", "txn_e2e_ms")
    queue_wait = _diff_hist(before, after, "worker", "queue_wait_ms")

    samples = [s for s in timeline.get("samples", []) if "error" not in s]
    max_depth = max((s["queue_pending"] + s["queue_lag"] for s in samples), default=0)

    return {
        "label": meta["label"], "meta": meta, "config": before.get("config", {}),
        "locust": {
            "requests": int(_num(agg.get("Request Count"))),
            "failures_429": f429, "failures_other": f_other,
            "rps_avg": _num(agg.get("Requests/s")),
            "rps_peak": max((_num(r.get("Requests/s")) for r in agg_history), default=0.0),
            "p50": _num(agg.get("50%")), "p95": _num(agg.get("95%")), "p99": _num(agg.get("99%")),
            "max": _num(agg.get("Max Response Time")),
            "endpoints": endpoints, "history": agg_history,
        },
        "gateway": {
            "total": gw_total, "outcomes": outcomes, "proxied_status": proxied_status,
            "rate_limited_by_scope": rl_scope,
            "accepted_p50": quantile(accepted_hist, 0.5), "accepted_p95": quantile(accepted_hist, 0.95),
            "accepted_p99": quantile(accepted_hist, 0.99),
            "rejected_p99": quantile(rejected_hist, 0.99),
            "server_errors": sum(v for k, v in proxied_status.items() if k.startswith("5")) + outcomes.get("upstream_error", 0),
        },
        "cache": {"hits": cache.get("hit", 0), "misses": cache.get("miss", 0), "bypass": cache.get("bypass", 0),
                  "errors": cache.get("error", 0),
                  "hit_ratio": (cache.get("hit", 0) / lookups) if lookups else None, "db_queries": db_queries},
        "payments": {
            "created": _diff_counter(before, after, "payment-service", "payments_created_total"),
            "success": processed.get("SUCCESS", 0), "failed": processed.get("FAILED", 0),
            "fail_reasons": fail_reasons,
            "e2e_p50": quantile(e2e, 0.5), "e2e_p95": quantile(e2e, 0.95), "e2e_p99": quantile(e2e, 0.99),
            "queue_wait_p95": quantile(queue_wait, 0.95),
            "bank_retries": _diff_counter(before, after, "worker", "bank_retries_total"),
            "deferred": _diff_counter(before, after, "worker", "payments_deferred_total"),
            "breaker_opened": _diff_counter(before, after, "worker", "circuit_transitions_total", to="OPEN"),
            "dead_letters": _diff_counter(before, after, "worker", "dlq_messages_total"),
            "max_queue_depth": max_depth, "drain_s": meta.get("queue_drain_s"),
        },
        "timeline": timeline,
    }


# --------------------------------------------------------------------------- charts
def make_charts(runs: list[dict[str, Any]], out_dir: Path) -> dict[str, str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[report] matplotlib not installed - skipping charts (pip install matplotlib)")
        return {}
    out_dir.mkdir(parents=True, exist_ok=True)
    charts: dict[str, str] = {}
    blue, orange, red, grey, green = "#2a6fdb", "#e8833a", "#d1453b", "#8a8f98", "#2e9e6a"

    for run in runs:
        hist = run["locust"]["history"]
        samples = [s for s in run["timeline"].get("samples", []) if "error" not in s]
        if not hist and not samples:
            continue
        fig, axes = plt.subplots(3, 1, figsize=(9, 8), sharex=False)
        fig.suptitle(f"Run: {run['label']}", fontsize=13, fontweight="bold")
        if hist:
            t0 = _num(hist[0]["Timestamp"])
            ts = [_num(r["Timestamp"]) - t0 for r in hist]
            ax = axes[0]
            ax.plot(ts, [_num(r["Requests/s"]) for r in hist], color=blue, label="requests/s (all)")
            ax.plot(ts, [_num(r["Failures/s"]) for r in hist], color=orange, label="failures/s (mostly 429)")
            ax2 = ax.twinx()
            ax2.plot(ts, [_num(r["User Count"]) for r in hist], color=grey, linestyle="--", label="users")
            ax2.set_ylabel("users", color=grey)
            ax.set_ylabel("req/s")
            ax.set_title("Throughput (client side)", fontsize=10, loc="left")
            ax.legend(loc="upper left", fontsize=8, frameon=False)
            ax = axes[1]
            ax.plot(ts, [_num(r["50%"], float("nan")) for r in hist], color=blue, label="p50")
            ax.plot(ts, [_num(r["95%"], float("nan")) for r in hist], color=red, label="p95")
            ax.set_ylabel("ms")
            ax.set_title("Response time (client side, all responses)", fontsize=10, loc="left")
            ax.legend(loc="upper left", fontsize=8, frameon=False)
        if samples:
            ax = axes[2]
            ts = [s["t"] for s in samples]
            ax.plot(ts, [s["queue_pending"] + s["queue_lag"] for s in samples], color=green, label="queue depth")
            rl_rate = [0.0] + [max(0.0, (b["gw_rate_limited"] - a["gw_rate_limited"]) / max(0.1, b["t"] - a["t"]))
                               for a, b in zip(samples, samples[1:])]
            ax3 = ax.twinx()
            ax3.plot(ts, rl_rate, color=orange, alpha=0.8, label="429/s")
            ax3.set_ylabel("429 per s", color=orange)
            for ev in run["timeline"].get("chaos_events", []):
                ax.axvline(ev["t"], color=red, linestyle=":", linewidth=1)
                ax.text(ev["t"], ax.get_ylim()[1] * 0.9, f" bank {ev['mode']}", color=red, fontsize=8)
            ax.set_ylabel("messages")
            ax.set_xlabel("seconds since start")
            ax.set_title("Queue depth (pending + lag) and rate-limited requests (server side)", fontsize=10, loc="left")
            ax.legend(loc="upper left", fontsize=8, frameon=False)
        for ax in axes:
            ax.grid(alpha=0.25)
            ax.spines[["top"]].set_visible(False)
        fig.tight_layout()
        path = out_dir / f"{run['label']}.png"
        fig.savefig(path, dpi=110)
        plt.close(fig)
        charts[run["label"]] = f"charts/{path.name}"

    if len(runs) > 1:
        labels = [r["label"] for r in runs]
        fig, axes = plt.subplots(1, 3, figsize=(11, 3.4))
        series = [
            ("Gateway p95 of accepted requests (ms)", [r["gateway"]["accepted_p95"] or 0 for r in runs], blue),
            ("Requests shed with 429 (%)", [100 * r["locust"]["failures_429"] / max(1, r["locust"]["requests"]) for r in runs], orange),
            ("5xx / timeouts (count)", [r["gateway"]["server_errors"] for r in runs], red),
        ]
        for ax, (title, values, color) in zip(axes, series):
            ax.bar(labels, values, color=color)
            ax.set_title(title, fontsize=9)
            ax.tick_params(axis="x", rotation=30, labelsize=8)
            ax.grid(axis="y", alpha=0.25)
            ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()
        fig.savefig(out_dir / "comparison.png", dpi=110)
        plt.close(fig)
        charts["comparison"] = "charts/comparison.png"
    return charts


# --------------------------------------------------------------------------- markdown
def _f(value: Any, digits: int = 1, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float) and value.is_integer() and digits == 0:
        return f"{int(value):,}{suffix}"
    return f"{value:,.{digits}f}{suffix}"


def _pct(part: float, whole: float) -> str:
    return "n/a" if not whole else f"{100 * part / whole:.1f}%"


def _describe(cfg: dict[str, Any]) -> str:
    if cfg.get("scenario") == "spike":
        text = f"spike {cfg.get('base_users', 60)} -> {cfg.get('spike_users', 600)} -> {cfg.get('base_users', 60)} users (2 min)"
    else:
        text = f"steady {cfg.get('users')} users, {cfg.get('duration')}"
    extras = [x for x, on in (("rate limiter OFF", cfg.get("limiter") == "off"), ("cache OFF", cfg.get("cache") == "off"),
                               ("bank chaos", cfg.get("chaos"))) if on]
    return text + (f" ({', '.join(extras)})" if extras else "")


def render(runs: list[dict[str, Any]], charts: dict[str, str]) -> str:
    by = {r["label"]: r for r in runs}
    first = runs[0]
    cfg = first["config"]
    meta = first["meta"]
    lines: list[str] = []
    w = lines.append

    w("# Load Test Report - UPI Payment System")
    w("")
    w(f"_Generated {time.strftime('%Y-%m-%d %H:%M')} from {len(runs)} run(s) in `loadtest/results/`._  ")
    host = meta.get("host", {})
    w(f"Environment: {host.get('os', '?')}, {host.get('cpus', '?')} CPUs, Docker {meta.get('docker', '?')}, "
      "all services on one machine via docker compose.")
    w("")
    w("## 1. Test setup")
    w("")
    w("| Run | Scenario |")
    w("|---|---|")
    for r in runs:
        w(f"| `{r['label']}` | {_describe(r['meta']['config'])} |")
    w("")
    pol = cfg.get("policies", {})
    w("Traffic mix per simulated phone: balance check 38%, payee verification 24%, send money 19%, "
      "status poll 14%, history 5%; 2 additional *abusive* clients send ~50 req/s each from a single device id.")
    w("")
    if pol:
        w("| Rate-limit bucket | Capacity (burst) | Refill (sustained) |")
        w("|---|---|---|")
        for name, p in pol.items():
            w(f"| {name} | {p['capacity']:g} | {p['refill_per_s']:g} req/s |")
        w("")

    w("## 2. Results at a glance")
    w("")
    w("| Run | Requests | Avg / peak req/s | Client p95 (ms) | Gateway p95 accepted (ms) | 429 shed | 5xx/timeouts | Cache hit | Payments OK / failed |")
    w("|---|---|---|---|---|---|---|---|---|")
    for r in runs:
        lc, gw, ca, pay = r["locust"], r["gateway"], r["cache"], r["payments"]
        hit = "n/a" if ca["hit_ratio"] is None else f"{100 * ca['hit_ratio']:.0f}%"
        w(f"| `{r['label']}` | {lc['requests']:,} | {lc['rps_avg']:.0f} / {lc['rps_peak']:.0f} | {lc['p95']:.0f} | "
          f"{_f(gw['accepted_p95'])} | {_pct(lc['failures_429'], lc['requests'])} | {gw['server_errors']:.0f} | "
          f"{hit} | {pay['success']:.0f} / {pay['failed']:.0f} |")
    w("")
    w("_Client p95_ is measured by Locust and includes the (very fast) 429 rejections. "
      "_Gateway p95 accepted_ is measured inside the gateway for requests that were actually forwarded to a service.")
    w("")
    if "comparison" in charts:
        w(f"![comparison]({charts['comparison']})")
        w("")

    w("## 3. Throughput and latency per run")
    for r in runs:
        lc, gw = r["locust"], r["gateway"]
        w("")
        w(f"### `{r['label']}` - {_describe(r['meta']['config'])}")
        w("")
        w("| Endpoint | Requests | Failures | Median | p95 | p99 | req/s |")
        w("|---|---|---|---|---|---|---|")
        for e in lc["endpoints"]:
            w(f"| {e['Name']} | {int(_num(e['Request Count'])):,} | {int(_num(e['Failure Count'])):,} | "
              f"{_num(e['50%']):.0f} | {_num(e['95%']):.0f} | {_num(e['99%']):.0f} | {_num(e['Requests/s']):.1f} |")
        w("")
        w(f"Gateway view: {gw['total']:,.0f} requests - "
          + ", ".join(f"{k} {v:,.0f}" for k, v in sorted(gw["outcomes"].items()))
          + f". Accepted latency p50/p95/p99 = {_f(gw['accepted_p50'])} / {_f(gw['accepted_p95'])} / {_f(gw['accepted_p99'])} ms; "
          f"a 429 rejection took at most {_f(gw['rejected_p99'])} ms (p99).")
        if r["label"] in charts:
            w("")
            w(f"![{r['label']}]({charts[r['label']]})")

    w("")
    w("## 4. How the rate limiter protected the system")
    w("")
    for r in runs:
        scope = r["gateway"]["rate_limited_by_scope"]
        if not scope and r["meta"]["config"].get("limiter") != "off":
            continue
        lc = r["locust"]
        if r["meta"]["config"].get("limiter") == "off":
            w(f"- `{r['label']}`: limiter disabled - every request reached the services.")
        else:
            w(f"- `{r['label']}`: {sum(scope.values()):,.0f} requests rejected with 429 "
              f"({_pct(lc['failures_429'], lc['requests'])} of traffic): "
              f"{scope.get('global', 0):,.0f} by the global bucket, {scope.get('client', 0):,.0f} by per-client buckets.")
    if "spike" in by and "spike-no-limiter" in by:
        a, b = by["spike"], by["spike-no-limiter"]
        w("")
        w(f"Same spike with and without the limiter: gateway p95 for forwarded requests "
          f"**{_f(a['gateway']['accepted_p95'])} ms vs {_f(b['gateway']['accepted_p95'])} ms**, "
          f"5xx/timeouts **{a['gateway']['server_errors']:.0f} vs {b['gateway']['server_errors']:.0f}**, "
          f"peak queue depth **{a['payments']['max_queue_depth']:.0f} vs {b['payments']['max_queue_depth']:.0f}** messages, "
          f"payment end-to-end p95 **{_f(a['payments']['e2e_p95'], 0)} ms vs {_f(b['payments']['e2e_p95'], 0)} ms**.")
    w("")
    w("> Discussion (team): explain what these numbers show - e.g. the limiter converts overload into cheap, "
      "explicit 429s (with Retry-After) instead of slow responses and timeouts for everyone.")

    w("")
    w("## 5. How the cache protected the database")
    w("")
    for r in runs:
        ca = r["cache"]
        w(f"- `{r['label']}`: {ca['hits']:,.0f} hits / {ca['misses']:,.0f} misses"
          + (f" (hit ratio {100 * ca['hit_ratio']:.1f}%)" if ca["hit_ratio"] is not None else "")
          + (f", {ca['bypass']:,.0f} bypassed (cache off)" if ca["bypass"] else "")
          + f"; {ca['db_queries']:,.0f} database queries from the API services.")
    if "spike" in by and "spike-no-cache" in by:
        a, b = by["spike"], by["spike-no-cache"]
        ratio = b["cache"]["db_queries"] / a["cache"]["db_queries"] if a["cache"]["db_queries"] else None
        w("")
        w(f"Disabling the cache multiplied database queries by **{_f(ratio)}x** "
          f"({a['cache']['db_queries']:,.0f} -> {b['cache']['db_queries']:,.0f}); gateway p95 for forwarded requests "
          f"{_f(a['gateway']['accepted_p95'])} ms -> {_f(b['gateway']['accepted_p95'])} ms.")

    w("")
    w("## 6. Asynchronous processing and fault tolerance")
    w("")
    w("| Run | Payments accepted (202) | Succeeded | Failed (reasons) | End-to-end p50 / p95 (ms) | Peak queue depth | Queue drained after load |")
    w("|---|---|---|---|---|---|---|")
    for r in runs:
        p = r["payments"]
        reasons = ", ".join(f"{k} {v:.0f}" for k, v in sorted(p["fail_reasons"].items())) or "-"
        drain = "n/a" if p["drain_s"] in (None, -1, -1.0) else f"{p['drain_s']:.0f} s"
        w(f"| `{r['label']}` | {p['created']:,.0f} | {p['success']:,.0f} | {p['failed']:,.0f} ({reasons}) | "
          f"{_f(p['e2e_p50'], 0)} / {_f(p['e2e_p95'], 0)} | {p['max_queue_depth']:.0f} | {drain} |")
    if "chaos" in by:
        p = by["chaos"]["payments"]
        events = ", ".join(f"t={e['t']:.0f}s {e['mode']}" for e in by["chaos"]["timeline"].get("chaos_events", []))
        w("")
        w(f"Chaos run ({events}): the worker retried bank calls {p['bank_retries']:,.0f} times with exponential "
          f"backoff, the circuit breaker opened {p['breaker_opened']:.0f} time(s), {p['deferred']:,.0f} deliveries were "
          f"deferred back to the queue, and {p['dead_letters']:.0f} message(s) were dead-lettered. "
          f"The API kept accepting payments throughout (gateway 5xx/timeouts: {by['chaos']['gateway']['server_errors']:.0f}).")

    w("")
    w("## 7. Conclusions")
    w("")
    w("> Team: summarise in 3-5 bullets (bottleneck observed, what protected the system, what you would scale next).")
    w("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", default=str(RESULTS))
    args = parser.parse_args()
    results = Path(args.results)
    dirs = [d for d in results.iterdir() if d.is_dir() and (d / "meta.json").exists()] if results.exists() else []
    dirs.sort(key=lambda d: (RUN_ORDER.index(d.name) if d.name in RUN_ORDER else 99, d.name))
    runs = [r for r in (analyze(d) for d in dirs) if r]
    if not runs:
        raise SystemExit(f"no runs found in {results} - run loadtest/run_loadtest.py first")
    charts = make_charts(runs, results / "charts")
    out = results / "LOAD_TEST_REPORT.md"
    out.write_text(render(runs, charts), encoding="utf-8")
    print(f"[report] wrote {out}")


if __name__ == "__main__":
    main()
