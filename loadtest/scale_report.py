"""Analyse the 100 / 200 / 500-user runs of loadtest/scale_test.py: numbers, charts, bottlenecks.

    python loadtest/scale_report.py

Reads   loadtest/results/scale-u*/  (Locust CSVs, /stats before/after, timeline, docker_stats.json)
Writes  loadtest/results/scale/summary.json and charts scale_*.png (matplotlib needed for charts)

Every number is taken from the runs themselves:
  client side  - Locust: requests/s, failures, p50/p95/p99 response times per endpoint
  server side  - diff of the gateway's /stats before/after: forwarded vs 429 (client / global bucket),
                 gateway + upstream latency, service latency, DB query time, cache hit ratio,
                 payments settled, queue wait, end-to-end settlement time
  resources    - `docker stats` CPU % (100 % = one core) and memory per container during the load
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path
from statistics import mean
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from stats_util import (counter_breakdown, counter_sum, histogram_diff, histogram_merge,  # noqa: E402
                        quantile, snapshots)

RESULTS = Path(__file__).resolve().parent / "results"
OUT = RESULTS / "scale"


LABEL = re.compile(r"scale-u(\d+)(?:-(nolimit|gw\d+))?$")


def label_order(p: Path) -> tuple[int, int]:
    m = LABEL.match(p.name)
    if not m:
        return (10**6, 0)
    variant = m.group(2) or ""
    return int(m.group(1)), (0 if not variant else 1 if variant == "nolimit" else 2)


def display_name(run: dict[str, Any]) -> str:
    v = run["variant"]
    suffix = "" if not v else "\nlimiter off" if v == "nolimit" else f"\ngateway x{v[2:]}"
    return f"{run['users']} users{suffix}"


def hist_q(before: dict, after: dict, node_service: str | None, name: str, qs=(0.5, 0.95, 0.99),
           **labels: str) -> dict[str, Any]:
    h = histogram_diff(histogram_merge(snapshots(after, node_service), name, **labels),
                       histogram_merge(snapshots(before, node_service), name, **labels))
    res = {f"p{int(q * 100)}": quantile(h, q) for q in qs}
    res["count"] = h["count"] if h else 0
    res["mean"] = round(h["sum"] / h["count"], 2) if h and h["count"] else None
    return res


def diff_counter(before: dict, after: dict, service: str | None, name: str, **labels: str) -> float:
    return counter_sum(snapshots(after, service), name, **labels) - counter_sum(snapshots(before, service), name, **labels)


def diff_breakdown(before: dict, after: dict, service: str | None, name: str, by: str, **labels: str) -> dict[str, float]:
    a = counter_breakdown(snapshots(after, service), name, by, **labels)
    b = counter_breakdown(snapshots(before, service), name, by, **labels)
    return {k: a.get(k, 0) - b.get(k, 0) for k in a}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def num(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "", "N/A") else None
    except ValueError:
        return None


def analyse(run: Path) -> dict[str, Any]:
    meta = json.loads((run / "meta.json").read_text())
    before = json.loads((run / "stats_before.json").read_text())
    after = json.loads((run / "stats_after.json").read_text())
    timeline = json.loads((run / "timeline.json").read_text()).get("samples", [])
    stats = read_csv(run / "locust_stats.csv")
    failures = read_csv(run / "locust_failures.csv")
    history = [r for r in read_csv(run / "locust_stats_history.csv") if r.get("Name") == "Aggregated"]
    agg = next((r for r in stats if r["Name"] == "Aggregated"), {})
    load_s = meta["load_duration_s"]

    endpoints = [{"name": r["Name"], "requests": int(r["Request Count"]), "failures": int(r["Failure Count"]),
                  "rps": num(r["Requests/s"]), "p50": num(r["50%"]), "p95": num(r["95%"]), "p99": num(r["99%"])}
                 for r in stats if r["Name"] != "Aggregated"]
    fail_by = {}
    for f in failures:
        fail_by[f["Error"]] = fail_by.get(f["Error"], 0) + int(f["Occurrences"])
    rl_fail = sum(v for k, v in fail_by.items() if "429" in k)
    other_fail = sum(v for k, v in fail_by.items() if "429" not in k)

    # server side
    forwarded = diff_counter(before, after, "gateway", "gw_requests_total", outcome="proxied")
    rl_scope = diff_breakdown(before, after, "gateway", "gw_rate_limited_total", "scope")
    upstream_err = diff_counter(before, after, "gateway", "gw_requests_total", outcome="upstream_error")
    cache = diff_breakdown(before, after, None, "cache_requests_total", "result")
    hits, misses = cache.get("hit", 0), cache.get("miss", 0)
    created = diff_counter(before, after, "payment-service", "payments_created_total")
    settled_ok = diff_counter(before, after, "worker", "payments_processed_total", status="SUCCESS")
    settled_fail = diff_counter(before, after, "worker", "payments_processed_total", status="FAILED")

    # resources: only samples while the load was on (skip the first 8 s of ramp-up)
    res: dict[str, dict[str, float]] = {}
    ds = json.loads((run / "docker_stats.json").read_text()).get("samples", []) if (run / "docker_stats.json").exists() else []
    window = [s for s in ds if "container" in s and 8 <= s["t"] <= load_s]
    for c in sorted({s["container"] for s in window}):
        vals = [s["cpu"] for s in window if s["container"] == c]
        mem = [s["mem_mib"] for s in window if s["container"] == c]
        res[c] = {"cpu_mean": round(mean(vals), 1), "cpu_max": round(max(vals), 1), "mem_max_mib": round(max(mem), 1)}

    m = LABEL.match(run.name)
    users, variant = int(m.group(1)), m.group(2) or ""
    return {
        "label": run.name, "users": users, "variant": variant, "limiter": meta["config"].get("limiter", "on"),
        # with several gateway processes /stats comes from whichever process answered: partial numbers
        "server_metrics_partial": variant.startswith("gw"),
        "load_s": load_s, "queue_drain_s": meta.get("queue_drain_s"), "host": meta.get("host"),
        "client": {
            "requests": int(agg.get("Request Count", 0)), "failures": int(agg.get("Failure Count", 0)),
            "rps": num(agg.get("Requests/s")), "p50": num(agg.get("50%")), "p95": num(agg.get("95%")),
            "p99": num(agg.get("99%")), "avg": num(agg.get("Average Response Time")), "max": num(agg.get("Max Response Time")),
            "rate_limited": rl_fail, "other_failures": other_fail, "failures_by_error": fail_by,
            "ok_rps": round((int(agg.get("Request Count", 0)) - int(agg.get("Failure Count", 0))) / load_s, 1) if load_s else None,
        },
        "endpoints": endpoints,
        "history": [{"t": i, "users": num(h.get("User Count")), "rps": num(h.get("Requests/s")),
                     "fps": num(h.get("Failures/s")), "p95": num(h.get("95%"))} for i, h in enumerate(history)],
        "server": {
            "forwarded": forwarded, "rate_limited": rl_scope, "upstream_errors": upstream_err,
            "gateway_ms": hist_q(before, after, "gateway", "gw_latency_ms", outcome="proxied"),
            "upstream_payment_ms": hist_q(before, after, "gateway", "gw_upstream_ms", service="payment-service"),
            "upstream_account_ms": hist_q(before, after, "gateway", "gw_upstream_ms", service="account-service"),
            "payment_service_ms": hist_q(before, after, "payment-service", "http_request_ms"),
            "account_service_ms": hist_q(before, after, "account-service", "http_request_ms"),
            "db_query_ms": hist_q(before, after, "account-service", "db_query_ms"),
            "db_queries": diff_counter(before, after, None, "db_queries_total"),
            "cache_hit_ratio": round(100 * hits / (hits + misses), 1) if hits + misses else None,
            "cache_hits": hits, "cache_misses": misses,
            "payments_created": created, "payments_success": settled_ok, "payments_failed": settled_fail,
            "settle_per_s": round((settled_ok + settled_fail) / load_s, 1) if load_s else None,
            "queue_wait_ms": hist_q(before, after, "worker", "queue_wait_ms"),
            "processing_ms": hist_q(before, after, "worker", "processing_ms"),
            "bank_call_ms": hist_q(before, after, "worker", "bank_call_ms"),
            "txn_e2e_ms": hist_q(before, after, "worker", "txn_e2e_ms"),
            "queue_pending_max": max((s.get("queue_pending", 0) for s in timeline if "error" not in s), default=0),
            "queue_lag_max": max((s.get("queue_lag", 0) for s in timeline if "error" not in s), default=0),
        },
        "timeline": [{k: s.get(k) for k in ("t", "queue_pending", "queue_lag", "gw_rate_limited", "gw_requests", "payments_success")}
                     for s in timeline if "error" not in s],
        "resources": res,
    }


def charts(runs: list[dict[str, Any]]) -> list[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed - skipping charts")
        return []
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False,
                         "axes.grid": True, "grid.color": "#e6e6e6", "axes.axisbelow": True})
    BLUE, ORANGE, GREEN, PURPLE, GREY = "#2a78d6", "#eb6834", "#1baf7a", "#6250d6", "#9a988f"
    names = [display_name(r) for r in runs]
    files = []

    def save(fig, name):
        path = OUT / name
        fig.tight_layout()
        fig.savefig(path, dpi=150)
        plt.close(fig)
        files.append(path.name)

    # 1 throughput
    fig, ax = plt.subplots(figsize=(8.5, 4))
    ok = [r["client"]["ok_rps"] or 0 for r in runs]
    rl = [r["client"]["rate_limited"] / r["load_s"] for r in runs]
    oth = [r["client"]["other_failures"] / r["load_s"] for r in runs]
    ax.bar(names, ok, color=BLUE, label="answered OK")
    ax.bar(names, rl, bottom=ok, color=ORANGE, label="429 rate limited")
    ax.bar(names, oth, bottom=[a + b for a, b in zip(ok, rl)], color="#d03b3b", label="other errors")
    for i, r in enumerate(runs):
        ax.text(i, ok[i] + rl[i] + oth[i] + 4, f"{r['client']['rps']:.0f} req/s", ha="center", fontsize=9)
    ax.set_ylabel("requests per second (Locust)")
    ax.set_title("Throughput at each load level", pad=26)
    ax.set_ylim(0, max(a + b + c for a, b, c in zip(ok, rl, oth)) * 1.15)
    ax.legend(frameon=False, loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=3, fontsize=9)
    save(fig, "scale_throughput.png")

    # 2 latency
    fig, ax = plt.subplots(figsize=(8.5, 4))
    w = 0.26
    xs = range(len(runs))
    for k, (q, col) in enumerate((("p50", GREEN), ("p95", BLUE), ("p99", PURPLE))):
        vals = [r["client"][q] or 0 for r in runs]
        ax.bar([x + (k - 1) * w for x in xs], vals, w, color=col, label=q)
        for x, v in zip(xs, vals):
            ax.text(x + (k - 1) * w, v, f"{v:.0f}", ha="center", va="bottom", fontsize=8)
    ax.set_xticks(list(xs), names)
    ax.set_ylabel("response time (ms, client side)")
    ax.set_title("Latency percentiles at each load level (all endpoints)", pad=26)
    ax.set_ylim(0, max(r["client"]["p99"] or 0 for r in runs) * 1.12)
    ax.legend(frameon=False, loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=3, fontsize=9)
    save(fig, "scale_latency.png")

    # 3 CPU per container
    containers = sorted({c for r in runs for c in r["resources"]},
                        key=lambda c: -max(r["resources"].get(c, {}).get("cpu_mean", 0) for r in runs))[:10]
    if containers:
        fig, ax = plt.subplots(figsize=(9, 4.6))
        h = 0.8 / len(runs)
        palette = [GREEN, BLUE, PURPLE, ORANGE, GREY]
        for i, r in enumerate(runs):
            vals = [r["resources"].get(c, {}).get("cpu_mean", 0) for c in containers]
            ax.barh([y + i * h for y in range(len(containers))], vals, h, color=palette[i % 5], label=names[i])
        ax.set_yticks([y + h * (len(runs) - 1) / 2 for y in range(len(containers))], containers)
        ax.invert_yaxis()
        ax.set_xlabel("mean CPU % during the load (100 % = one core)")
        ax.set_title("Which container works hardest?")
        ax.legend(frameon=False, fontsize=8)
        save(fig, "scale_cpu.png")

    # 4 RPS over time + 5 queue depth over time
    fig, ax = plt.subplots(figsize=(8.5, 3.8))
    for r, n_, col in zip(runs, names, [GREEN, BLUE, PURPLE, ORANGE, "#c0392b"]):
        ax.plot([h["t"] for h in r["history"]], [h["rps"] or 0 for h in r["history"]], color=col, label=n_, lw=1.6)
    ax.set_xlabel("seconds since start")
    ax.set_ylabel("requests / s")
    ax.set_title("Request rate over time")
    ax.legend(frameon=False, fontsize=8)
    save(fig, "scale_rps_timeline.png")

    fig, ax = plt.subplots(figsize=(8.5, 3.8))
    for r, n_, col in zip(runs, names, [GREEN, BLUE, PURPLE, ORANGE, "#c0392b"]):
        ax.plot([s["t"] for s in r["timeline"]], [(s["queue_pending"] or 0) + (s["queue_lag"] or 0) for s in r["timeline"]],
                color=col, label=n_, lw=1.6)
    ax.set_xlabel("seconds since start")
    ax.set_ylabel("payments waiting in the queue")
    ax.set_title("Queue depth (Redis Stream pending + not yet read)")
    ax.legend(frameon=False, fontsize=8)
    save(fig, "scale_queue.png")

    # 6 where time goes (server-side p95 per stage)
    fig, ax = plt.subplots(figsize=(8.5, 4))
    stages = [("gateway total", "gateway_ms"), ("payment svc", "payment_service_ms"), ("account svc", "account_service_ms"),
              ("DB query", "db_query_ms"), ("queue wait", "queue_wait_ms"), ("worker processing", "processing_ms")]
    w = 0.8 / len(runs)
    for i, (r, col) in enumerate(zip(runs, [GREEN, BLUE, PURPLE, ORANGE, "#c0392b"])):
        vals = [r["server"][k]["p95"] or 0 for _, k in stages]
        ax.bar([x + i * w for x in range(len(stages))], vals, w, color=col, label=names[i])
    ax.set_xticks([x + w * (len(runs) - 1) / 2 for x in range(len(stages))], [s for s, _ in stages], fontsize=9)
    ax.set_ylabel("p95 (ms, server side; 10 s = histogram top bucket)")
    ax.set_title("Where the time goes: p95 per stage")
    ax.legend(frameon=False, fontsize=8)
    save(fig, "scale_stages.png")
    return files


def main() -> int:
    runs_dirs = sorted((p for p in RESULTS.glob("scale-u*") if (p / "stats_after.json").exists() and LABEL.match(p.name)),
                       key=label_order)
    if not runs_dirs:
        print("no scale-u* runs found - run python loadtest/scale_test.py first")
        return 1
    OUT.mkdir(exist_ok=True)
    runs = [analyse(p) for p in runs_dirs]
    files = charts(runs)
    (OUT / "summary.json").write_text(json.dumps({"runs": runs, "charts": files}, indent=1))
    print(f"{'run':<22}{'req/s':>8}{'ok/s':>8}{'429':>7}{'errors':>7}{'p50':>7}{'p95':>7}{'p99':>7}"
          f"{'gw p95':>8}{'settled/s':>10}{'q max':>7}{'cache%':>7}")
    for r in runs:
        c, s = r["client"], r["server"]
        print(f"{r['label']:<22}{c['rps']:>8.1f}{c['ok_rps']:>8.1f}{c['rate_limited']:>7}{c['other_failures']:>7}"
              f"{c['p50']:>7.0f}{c['p95']:>7.0f}{c['p99']:>7.0f}{s['gateway_ms']['p95'] or 0:>8.1f}"
              f"{s['settle_per_s']:>10.1f}{s['queue_pending_max'] + s['queue_lag_max']:>7.0f}{s['cache_hit_ratio'] or 0:>7.1f}")
        top = sorted(r["resources"].items(), key=lambda kv: -kv[1]["cpu_mean"])[:5]
        print("    CPU mean: " + ", ".join(f"{k} {v['cpu_mean']}%" for k, v in top))
    print(f"\nwrote {OUT / 'summary.json'} and {len(files)} charts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
