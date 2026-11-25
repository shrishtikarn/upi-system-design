"""Scalability test: the same steady workload at 100, 200 and 500 users, plus container CPU/memory.

    python loadtest/scale_test.py                         # 100, 200, 500 users + 500 without the limiter (~8 min)
    python loadtest/scale_test.py --levels 100 200 --duration 45s --no-unlimited
    python loadtest/scale_report.py                       # charts + bottleneck analysis

Each level is one run of loadtest/run_loadtest.py (Locust in Docker, results in
loadtest/results/scale-u<N>/). While it runs, this script samples `docker stats` every
few seconds into loadtest/results/scale-u<N>/docker_stats.json so the report can show
which container saturates first. The extra "scale-u500-nolimit" run repeats 500 users
with RATE_LIMIT_ENABLED=false to show what the backend does without the gateway's
protection - that is where the real bottleneck shows.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "loadtest" / "results"


def parse_pct(text: str) -> float:
    try:
        return float(text.strip().rstrip("%"))
    except ValueError:
        return 0.0


def parse_mem_mib(text: str) -> float:
    used = text.split("/")[0].strip()
    for unit, factor in (("GiB", 1024), ("MiB", 1), ("KiB", 1 / 1024), ("GB", 1000), ("MB", 1), ("kB", 1 / 1000), ("B", 1 / 2**20)):
        if used.endswith(unit):
            try:
                return float(used[: -len(unit)]) * factor
            except ValueError:
                return 0.0
    return 0.0


class DockerStatsSampler(threading.Thread):
    def __init__(self, t0: float):
        super().__init__(daemon=True)
        self.t0 = t0
        self.samples: list[dict] = []
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                                     capture_output=True, text=True, timeout=20).stdout
                t = round(time.time() - self.t0, 1)
                for line in out.splitlines():
                    row = json.loads(line)
                    # upi-system-worker-1-1 -> worker-1 (drop the project prefix and compose's replica index)
                    name = re.sub(r"-\d+$", "", row["Name"].removeprefix("upi-system-"))
                    if "locust" in row["Name"]:
                        name = "locust (load generator)"
                    self.samples.append({"t": t, "container": name, "cpu": parse_pct(row["CPUPerc"]),
                                         "mem_mib": round(parse_mem_mib(row["MemUsage"]), 1)})
            except Exception as exc:  # noqa: BLE001
                self.samples.append({"t": round(time.time() - self.t0, 1), "error": repr(exc)})
            self.stop_event.wait(1.0)


def run_level(label: str, users: int, duration: str, limiter: str) -> None:
    spawn = min(100, max(20, users // 5))
    cmd = [sys.executable, str(ROOT / "loadtest" / "run_loadtest.py"), "--label", label, "--users", str(users),
           "--spawn-rate", str(spawn), "--duration", duration, "--limiter", limiter, "--no-report"]
    print(f"\n[scale] ===== {label}: {users} users, spawn {spawn}/s, {duration}, limiter {limiter} =====", flush=True)
    t0 = time.time()
    sampler = DockerStatsSampler(t0)
    sampler.start()
    try:
        subprocess.run(cmd, cwd=ROOT, check=True)
    finally:
        sampler.stop_event.set()
        sampler.join(timeout=30)
        out = RESULTS / label
        out.mkdir(parents=True, exist_ok=True)
        (out / "docker_stats.json").write_text(json.dumps({"samples": sampler.samples}, indent=1))
        print(f"[scale] {label}: {len(sampler.samples)} docker stats samples saved", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--levels", type=int, nargs="+", default=[100, 200, 500])
    ap.add_argument("--duration", default="60s")
    ap.add_argument("--no-unlimited", action="store_true", help="skip the extra run without the rate limiter")
    ap.add_argument("--gateway-workers", type=int, default=0,
                    help="extra run at the top level with the gateway scaled to N processes (bottleneck fix experiment)")
    ap.add_argument("--only-gateway-experiment", action="store_true", help="run just the --gateway-workers experiment")
    args = ap.parse_args()
    top = max(args.levels)
    if not args.only_gateway_experiment:
        for users in args.levels:
            run_level(f"scale-u{users}", users, args.duration, "on")
            time.sleep(5)
        if not args.no_unlimited:
            run_level(f"scale-u{top}-nolimit", top, args.duration, "off")
    if args.gateway_workers > 1:
        env = {**os.environ, "GATEWAY_WORKERS": str(args.gateway_workers)}
        subprocess.run(["docker", "compose", "up", "-d", "--no-deps", "gateway"], cwd=ROOT, env=env, check=True)
        try:
            run_level(f"scale-u{top}-gw{args.gateway_workers}", top, args.duration, "on")
        finally:
            subprocess.run(["docker", "compose", "up", "-d", "--no-deps", "gateway"], cwd=ROOT,
                           env={**os.environ, "GATEWAY_WORKERS": "1"}, check=False)
    print("\n[scale] done - now run: python loadtest/scale_report.py", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
