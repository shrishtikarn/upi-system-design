# 10-minute live demo - run sheet

Commands use `python`; on macOS/Linux use `python3`. Log filters are given for PowerShell
(`Select-String`); on macOS/Linux replace with `grep -E`.

## Before the demo (do this 15 minutes earlier)

```bash
docker compose build                                 # images ready, no waiting on stage
docker compose --profile loadtest pull locust        # pre-pull Locust
docker compose up -d
python scripts/smoke_test.py                         # all checks must pass
python loadtest/run_loadtest.py --preset all         # if not done yet: data for the report (~12 min)
```

Open beforehand:
* **Terminal 1** (commands), large font.
* **Terminal 2** (worker logs, keep running):
  `docker compose logs -f --no-log-prefix worker-1 worker-2 | Select-String "circuit|deferred|retry|completed"`
* Browser tab 1: http://localhost:8080/health
* Browser tab 2: `loadtest/results/LOAD_TEST_REPORT.md` (or its PDF) with charts.
* Editor with `gateway/ratelimit.py`, `worker/processor.py`, `common/resilience.py`.

## Showing it on the live dashboard

Open **http://127.0.0.1:8080/dashboard** on the projector. Each command below has a dashboard twin, so you can
type the command (proves it's real) and point at the dashboard (makes it easy to follow):

| Command | Dashboard |
|---|---|
| `python scripts/demo.py health` | *Nodes* panel and the dots in *How a request flows* |
| `python scripts/demo.py burst --n 80` | **Flood: 80 requests from one phone** button; watch the 429 line in *Throughput* |
| `python scripts/demo.py pay ... --twice` | **Same payment twice**; the log shows `Idempotent-Replay`, same txn |
| `python scripts/demo.py trace <id>` | click the transaction: *Journey of one payment* shows every step and node |
| `python scripts/demo.py chaos down` / `normal` | **Bank: Down / Normal**; the workers box turns red, the queue grows, then drains |
| Locust spike | Locust UI for the load, dashboard for what the system does about it |

## Timeline

| Time | Who | Show | Say |
|---|---|---|---|
| 0:00-1:00 | Member 1 | `docs/architecture.png` | One public gateway; stateless services; Postgres = truth; Redis = cache + rate limits + queue; workers talk to the slow bank asynchronously. |
| 1:00-2:00 | Member 1 | `docker compose ps` then `python scripts/demo.py health` | 10 containers from one compose file. Gateway polls every node's `/health`; each node checks its own Postgres/Redis; per-node circuit state is visible. |
| 2:00-3:30 | Member 1 | `python scripts/demo.py burst --n 80` then `gateway/ratelimit.py` (Lua script) | 80 requests from one phone: ~40 pass (bucket capacity), the rest get **429 + Retry-After**; another client is unaffected. Token bucket = burst + sustained rate; Lua makes check-and-take atomic across gateway replicas; local fallback if Redis dies. |
| 3:30-5:00 | Member 2 | `python scripts/demo.py pay alice@okaxis bob@oksbi 150 --twice` | 202 in a few ms, the bank has not been called yet. Second call with the same Idempotency-Key returns the same transaction, so there is no double debit. Status moves PENDING -> PROCESSING -> SUCCESS. |
| | Member 2 | `python scripts/demo.py balance bob@oksbi` | First read MISS (Postgres), second HIT (Redis). The worker deleted this key when the payment completed, so the balance is fresh. |
| | Member 3 | `python scripts/demo.py trace <request_id printed above>` | One request id across gateway -> payment service -> queue -> worker -> bank -> DB commit -> cache invalidation. |
| 5:00-7:30 | Member 3 | **Live load test** (see below) + `python scripts/demo.py stats` | Spike from 60 to 600 users. The global bucket sheds the excess as fast 429s; accepted requests keep their latency; payments queue up and drain. |
| 7:30-9:00 | Member 3 | `python scripts/demo.py chaos down`, wait ~5 s, `python scripts/demo.py health`, then `python scripts/demo.py chaos normal` | Terminal 2 shows retries with growing backoff, then `circuit.state_changed CLOSED -> OPEN`; payments are deferred, not lost. After `normal`: HALF_OPEN -> CLOSED and the queue drains to SUCCESS. |
| 9:00-10:00 | all | report charts + team table in README | Results from our runs (with vs without limiter / cache), who built what, questions. |

## Live load test options

**Locust web UI (best for showing live graphs):**

```bash
# PowerShell:  $env:LOAD_SHAPE="spike"; docker compose --profile loadtest up -d locust
LOAD_SHAPE=spike docker compose --profile loadtest up -d locust
```

Open http://localhost:8089, press **Start** (the spike shape controls the user count) and show the
*Charts* tab (RPS, response times, users) and the *Failures* tab ("429 RATE_LIMITED" = deliberate
load shedding, not errors). Stop with `docker compose --profile loadtest stop locust`.

**Headless (backup):** `python loadtest/run_loadtest.py --preset spike --no-report`

## Useful one-liners during Q&A

```bash
docker compose stop payment-service-1           # kill a node -> health shows DOWN, traffic goes to node 2
python scripts/demo.py pay alice@okaxis bob@oksbi 20   # still works
docker compose start payment-service-1

python scripts/demo.py pay alice@okaxis bob@oksbi 1.13 # mock bank declines amounts ending in .13 -> FAILED

docker compose exec redis redis-cli XINFO GROUPS upi:payments:stream   # queue: pending / lag
docker compose exec postgres psql -U upi -d upi -c "SELECT status, count(*) FROM transactions GROUP BY 1"
docker compose exec postgres psql -U upi -d upi -c "SELECT sum(balance_paise) FROM bank_accounts"  # money conserved
```

## If something goes wrong on stage

* Stack is slow to become healthy: `docker compose logs migrate`, wait 20 s, re-run `demo.py health`.
* Locust UI does not load: use the headless backup command.
* Laptop overloaded during the spike: lower it, e.g. `SPIKE_USERS=300`.
* Worst case: present `LOAD_TEST_REPORT.md` and the charts you generated earlier.
