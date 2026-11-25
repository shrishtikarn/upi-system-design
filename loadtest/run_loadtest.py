"""Run a load-test scenario end to end and save everything the report needs.

Stdlib only - runs on Windows, macOS and Linux with any Python 3.9+.
The stack must already be running (docker compose up -d).

Examples
--------
  python loadtest/run_loadtest.py --preset baseline
  python loadtest/run_loadtest.py --preset spike
  python loadtest/run_loadtest.py --preset spike-no-limiter
  python loadtest/run_loadtest.py --preset spike-no-cache
  python loadtest/run_loadtest.py --preset chaos
  python loadtest/run_loadtest.py --preset all        # all five, ~12 minutes
  python loadtest/make_report.py                      # -> loadtest/results/LOAD_TEST_REPORT.md

Custom:
  python loadtest/run_loadtest.py --label my-run --users 200 --spawn-rate 50 --duration 90s

For each run, loadtest/results/<label>/ gets:
  locust_stats.csv, locust_failures.csv, locust_stats_history.csv, locust_report.html
  stats_before.json, stats_after.json   (gateway /stats snapshots -> exact per-run diffs)
  timeline.json                         (queue depth, 429s, breaker state every 2 s)
  meta.json
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from stats_util import compact_sample, fetch_json, gauge_max, snapshots  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "loadtest" / "results"

PRESETS: dict[str, dict[str, Any]] = {
    "baseline":         {"scenario": "steady", "users": 100, "spawn_rate": 20, "duration": "60s"},
    "spike":            {"scenario": "spike"},
    "spike-no-limiter": {"scenario": "spike", "limiter": "off"},
    "spike-no-cache":   {"scenario": "spike", "cache": "off"},
    "chaos":            {"scenario": "steady", "users": 80, "spawn_rate": 20, "duration": "90s", "chaos": True},
}
# t (seconds after start) -> bank mode
CHAOS_SCHEDULE = [(20, "flaky"), (40, "down"), (60, "normal")]


def log(msg: str) -> None:
    print(f"[loadtest {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def compose(*args: str, env: dict[str, str] | None = None, check: bool = True) -> int:
    cmd = ["docker", "compose", *args]
    log("$ " + " ".join(cmd))
    return subprocess.run(cmd, cwd=ROOT, env={**os.environ, **(env or {})}, check=check).returncode


def post_json(url: str, body: dict[str, Any]) -> Any:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def wait_for_gateway(gateway: str, timeout_s: float = 120) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            health = fetch_json(f"{gateway}/health?live=true", timeout=3)
            if health.get("status") in ("UP", "DEGRADED"):
                log(f"gateway is {health['status']}")
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    raise SystemExit("gateway did not become healthy - is `docker compose up -d` running?")


def apply_toggles(limiter: str, cache: str, restore: bool = False) -> None:
    if limiter == "off":
        env = {} if restore else {"RATE_LIMIT_ENABLED": "false"}
        compose("up", "-d", "--no-deps", "gateway", env={"RATE_LIMIT_ENABLED": "true", **env})
    if cache == "off":
        env = {} if restore else {"CACHE_ENABLED": "false"}
        compose("up", "-d", "--no-deps", "payment-service-1", "payment-service-2", "account-service",
                env={"CACHE_ENABLED": "true", **env})


class Sampler(threading.Thread):
    def __init__(self, gateway: str, t0: float, interval_s: float = 2.0):
        super().__init__(daemon=True)
        self.gateway, self.t0, self.interval_s = gateway, t0, interval_s
        self.samples: list[dict[str, Any]] = []
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.samples.append(compact_sample(fetch_json(f"{self.gateway}/stats", timeout=5), time.time() - self.t0))
            except Exception as exc:  # noqa: BLE001
                self.samples.append({"t": round(time.time() - self.t0, 1), "error": repr(exc)})
            self.stop_event.wait(self.interval_s)


def chaos_thread(bank: str, t0: float, events: list[dict[str, Any]]) -> threading.Thread:
    def run() -> None:
        for at, mode in CHAOS_SCHEDULE:
            time.sleep(max(0.0, t0 + at - time.time()))
            try:
                post_json(f"{bank}/admin/chaos", {"mode": mode})
                events.append({"t": round(time.time() - t0, 1), "mode": mode})
                log(f"chaos: bank switch -> {mode}")
            except Exception as exc:  # noqa: BLE001
                log(f"chaos: could not set mode {mode}: {exc!r}")
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def wait_for_drain(gateway: str, timeout_s: float = 90) -> float:
    """Wait until the workers have processed everything that was queued."""
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            workers = snapshots(fetch_json(f"{gateway}/stats"), "worker")
            if gauge_max(workers, "queue_pending") == 0 and gauge_max(workers, "queue_lag") == 0:
                return round(time.time() - start, 1)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    return -1.0


def run_one(label: str, cfg: dict[str, Any], gateway: str, bank: str) -> None:
    out = RESULTS / label
    out.mkdir(parents=True, exist_ok=True)
    limiter, cache = cfg.get("limiter", "on"), cfg.get("cache", "on")
    log(f"=== run '{label}': {cfg}")
    apply_toggles(limiter, cache)
    try:
        wait_for_gateway(gateway)
        time.sleep(4)  # let the health monitor see every node
        try:
            post_json(f"{bank}/admin/chaos", {"mode": "normal"})
        except Exception:  # noqa: BLE001
            pass
        before = fetch_json(f"{gateway}/stats")
        (out / "stats_before.json").write_text(json.dumps(before, indent=1))

        t0 = time.time()
        sampler = Sampler(gateway, t0)
        sampler.start()
        chaos_events: list[dict[str, Any]] = []
        if cfg.get("chaos"):
            chaos_thread(bank, t0, chaos_events)

        locust_args = ["--profile", "loadtest", "run", "--rm"]
        locust_args += ["-e", f"LOAD_SHAPE={'spike' if cfg['scenario'] == 'spike' else ''}"]
        locust_args += ["-e", f"SPIKE_USERS={cfg.get('spike_users', 600)}", "-e", f"BASE_USERS={cfg.get('base_users', 60)}"]
        locust_args += ["locust", "-f", "/mnt/locust/locustfile.py", "--headless", "--host", "http://gateway:8080",
                        "--csv", f"/mnt/locust/results/{label}/locust", "--csv-full-history",
                        "--html", f"/mnt/locust/results/{label}/locust_report.html",
                        "--exit-code-on-error", "0"]
        if cfg["scenario"] != "spike":
            locust_args += ["-u", str(cfg["users"]), "-r", str(cfg["spawn_rate"]), "-t", str(cfg["duration"])]
        compose(*locust_args)
        load_finished = time.time() - t0

        log("load finished - waiting for the worker pool to drain the queue ...")
        drain_s = wait_for_drain(gateway)
        sampler.stop_event.set()
        sampler.join(timeout=10)
        if cfg.get("chaos"):
            post_json(f"{bank}/admin/chaos", {"mode": "normal"})

        after = fetch_json(f"{gateway}/stats")
        (out / "stats_after.json").write_text(json.dumps(after, indent=1))
        (out / "timeline.json").write_text(json.dumps({"samples": sampler.samples, "chaos_events": chaos_events}, indent=1))
        meta = {
            "label": label,
            "config": cfg,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0)),
            "load_duration_s": round(load_finished, 1),
            "queue_drain_s": drain_s,
            "host": {"os": f"{platform.system()} {platform.release()}", "cpus": os.cpu_count(),
                     "python": platform.python_version()},
        }
        try:
            meta["docker"] = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                                            capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:  # noqa: BLE001
            pass
        (out / "meta.json").write_text(json.dumps(meta, indent=1))
        log(f"saved results to {out}")
    finally:
        apply_toggles(limiter, cache, restore=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--preset", choices=[*PRESETS, "all"])
    parser.add_argument("--label")
    parser.add_argument("--scenario", choices=["steady", "spike"], default="steady")
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--spawn-rate", type=int, default=20)
    parser.add_argument("--duration", default="60s")
    parser.add_argument("--spike-users", type=int, default=600)
    parser.add_argument("--base-users", type=int, default=60)
    parser.add_argument("--limiter", choices=["on", "off"], default="on")
    parser.add_argument("--cache", choices=["on", "off"], default="on")
    parser.add_argument("--chaos", action="store_true")
    parser.add_argument("--gateway", default="http://127.0.0.1:8080")
    parser.add_argument("--bank", default="http://127.0.0.1:8090")
    parser.add_argument("--no-report", action="store_true")
    args = parser.parse_args()

    if args.preset == "all":
        runs = [(name, dict(cfg)) for name, cfg in PRESETS.items()]
    elif args.preset:
        runs = [(args.label or args.preset, dict(PRESETS[args.preset]))]
    else:
        runs = [(args.label or f"custom-{time.strftime('%H%M%S')}", {
            "scenario": args.scenario, "users": args.users, "spawn_rate": args.spawn_rate,
            "duration": args.duration, "limiter": args.limiter, "cache": args.cache, "chaos": args.chaos})]
    for _, cfg in runs:
        cfg.setdefault("spike_users", args.spike_users)
        cfg.setdefault("base_users", args.base_users)

    for label, cfg in runs:
        run_one(label, cfg, args.gateway.rstrip("/"), args.bank.rstrip("/"))

    if not args.no_report:
        subprocess.run([sys.executable, str(Path(__file__).parent / "make_report.py")], check=False)


if __name__ == "__main__":
    main()
