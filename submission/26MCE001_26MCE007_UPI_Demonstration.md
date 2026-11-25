**GitHub repository: https://github.com/shrishtikarn/upi-system-design**

# UPI Payment System - Demonstration of All Scenarios

Distributed System Design - demonstrative screens of every scenario of the application

| | Name | Roll no. | GitHub |
|---|---|---|---|
| Member 1 | **Darsh Shah** | **26MCE001** | https://github.com/darsh-1010 |
| Member 2 | **Shrishti Karn** | **26MCE007** | https://github.com/shrishtikarn |


## Contents

1. Phase 1 - Docker setup, API gateway, routing, rate limiter (429), health endpoint
2. Phase 2 - PostgreSQL schema and data, Redis cache, asynchronous queue + worker pool
3. Pay Console - person-to-person payment scenarios (success, merchant, decline, insufficient funds, limit, self-pay, unknown payee, double tap, bank outage and recovery)
4. Live dashboard - normal load, bank down (circuit breaker open), recovery
5. Phase 3 - fault tolerance (retries, circuit breaker), structured logs and request tracing, metrics
6. Load testing - 100 / 200 / 500 users, 100-user functional scenario test, unit tests

Source code: **https://github.com/shrishtikarn/upi-system-design** - run with `docker compose up --build -d`, then open `http://127.0.0.1:8080/dashboard` (live dashboard) and `http://127.0.0.1:8080/pay` (Pay Console).

## 1. Phase 1 - Docker setup, API gateway, rate limiting, health

![`docker compose up` starts the gateway, 2 payment nodes, account service, 2 workers, bank mock, PostgreSQL and Redis - every container healthy](assets/p1_compose_up.png)
**Figure: `docker compose up` starts the gateway, 2 payment nodes, account service, 2 workers, bank mock, PostgreSQL and Redis - every container healthy**

![Live dashboard before the test: every node UP, circuit breakers CLOSED](assets/02_dashboard_before.png)
**Figure: Live dashboard before the test: every node UP, circuit breakers CLOSED**

![Health check endpoint: the gateway polls each node's /health and reports status, circuit breaker, and queue depth per node](assets/p1_health_cli.png)
**Figure: Health check endpoint: the gateway polls each node's /health and reports status, circuit breaker, and queue depth per node**

![GET /health (JSON): overall status UP / DEGRADED / DOWN, rate-limiter backend and per-node details](assets/p1_health_json.png)
**Figure: GET /health (JSON): overall status UP / DEGRADED / DOWN, rate-limiter backend and per-node details**

![Gateway routing table: /api/v1/payments -> payment-service (2 nodes, round robin), /api/v1/accounts and /api/v1/vpa -> account-service](assets/p1_routes.png)
**Figure: Gateway routing table: /api/v1/payments -> payment-service (2 nodes, round robin), /api/v1/accounts and /api/v1/vpa -> account-service**

![Token-bucket rate limiter: one client floods 80 requests; requests pass while its bucket has tokens (burst 40, refilling 20/s), the rest get 429 Too Many Requests with Retry-After, while another client is unaffected](assets/p1_burst.png)
**Figure: Token-bucket rate limiter: one client floods 80 requests; requests pass while its bucket has tokens (burst 40, refilling 20/s), the rest get 429 Too Many Requests with Retry-After, while another client is unaffected**

![Gateway log lines for requests rejected by the per-client token bucket](assets/28_log_rate_limited.png)
**Figure: Gateway log lines for requests rejected by the per-client token bucket**

![End-to-end smoke test: health, routing, cache HIT, async payment, idempotency, 400 without Idempotency-Key, bank decline, 404 route, 429 burst, /stats - all 11 checks pass](assets/p1_smoke.png)
**Figure: End-to-end smoke test: health, routing, cache HIT, async payment, idempotency, 400 without Idempotency-Key, bank decline, 404 route, 429 burst, /stats - all 11 checks pass**

## 2. Phase 2 - Database, cache, asynchronous workers

![Database tables created by the migration scripts (db/migrations/001-003)](assets/p2_tables.png)
**Figure: Database tables created by the migration scripts (db/migrations/001-003)**

![Schema of the transactions table: unique idempotency_key, status enum, lease columns, request_id for tracing, CHECK payer <> payee](assets/p2_txn_table.png)
**Figure: Schema of the transactions table: unique idempotency_key, status enum, lease columns, request_id for tracing, CHECK payer <> payee**

![Migration runner: every SQL file applied once and recorded in schema_migrations](assets/p2_migrations.png)
**Figure: Migration runner: every SQL file applied once and recorded in schema_migrations**

![Worker results in PostgreSQL: transaction status, one debit + one credit ledger row per SUCCESS, totals by status](assets/p2_data.png)
**Figure: Worker results in PostgreSQL: transaction status, one debit + one credit ledger row per SUCCESS, totals by status**

![Append-only audit log: PENDING (payment service) -> PROCESSING -> SUCCESS (worker) for one payment](assets/p2_events.png)
**Figure: Append-only audit log: PENDING (payment service) -> PROCESSING -> SUCCESS (worker) for one payment**

![Redis cache: the first balance read is a MISS (PostgreSQL), the second a HIT from Redis - faster](assets/p2_cache.png)
**Figure: Redis cache: the first balance read is a MISS (PostgreSQL), the second a HIT from Redis - faster**

![Redis holds the payment stream + consumer group (async queue), cache entries with TTLs, and the token buckets of the rate limiter](assets/p2_redis.png)
**Figure: Redis holds the payment stream + consumer group (async queue), cache entries with TTLs, and the token buckets of the rate limiter**

![Asynchronous processing: POST returns 202 at once, the second request with the same Idempotency-Key is replayed (no second debit), the worker moves it PENDING -> SUCCESS](assets/p2_pay_cli.png)
**Figure: Asynchronous processing: POST returns 202 at once, the second request with the same Idempotency-Key is replayed (no second debit), the worker moves it PENDING -> SUCCESS**

![Worker log lines for settled payments (SUCCESS / FAILED with reason)](assets/29_log_payment_completed.png)
**Figure: Worker log lines for settled payments (SUCCESS / FAILED with reason)**

![Dashboard: clicking a transaction shows its journey from the audit log](assets/07_dashboard_txn_journey.png)
**Figure: Dashboard: clicking a transaction shows its journey from the audit log**

## 3. Pay Console - person-to-person payment scenarios

The Pay Console (`/pay`) is the interface where one person chooses whom to pay and how much. Every scenario below was sent through the real API gateway; the right-hand side shows the payment's journey, the HTTP / trace details, both balances before and after, the audit trail and the payer's history.

![Pay console - P2P: Alice pays Bob Rs 150, payee verified, SUCCESS, balances move](assets/08_pay_p2p_success.png)
**Figure: Pay console - P2P: Alice pays Bob Rs 150, payee verified, SUCCESS, balances move**

![Pay console - P2M: Bob pays the merchant Chai Point](assets/09_pay_p2m_merchant.png)
**Figure: Pay console - P2M: Bob pays the merchant Chai Point**

![Pay console - remitter bank declines (amount ending .13): FAILED, no money moves](assets/10_pay_bank_decline.png)
**Figure: Pay console - remitter bank declines (amount ending .13): FAILED, no money moves**

![Pay console - payer cannot afford it: FAILED INSUFFICIENT_FUNDS](assets/11_pay_insufficient_funds.png)
**Figure: Pay console - payer cannot afford it: FAILED INSUFFICIENT_FUNDS**

![Pay console - Rs 1,50,000 is above the Rs 1 lakh P2P limit: rejected with 422](assets/12_pay_over_limit.png)
**Figure: Pay console - Rs 1,50,000 is above the Rs 1 lakh P2P limit: rejected with 422**

![Pay console - paying yourself is rejected (SAME_PAYER_PAYEE)](assets/13_pay_self.png)
**Figure: Pay console - paying yourself is rejected (SAME_PAYER_PAYEE)**

![Pay console - unknown payee VPA: verification fails, 404](assets/14_pay_unknown_payee.png)
**Figure: Pay console - unknown payee VPA: verification fails, 404**

![Pay console - double tap: the same Idempotency-Key twice, one debit](assets/15_pay_double_tap.png)
**Figure: Pay console - double tap: the same Idempotency-Key twice, one debit**

![Pay console - bank DOWN: the payment is accepted (202) and waits; the worker keeps retrying](assets/16_pay_bank_outage_recovered_waiting.png)
**Figure: Pay console - bank DOWN: the payment is accepted (202) and waits; the worker keeps retrying**

![Pay console - payment sent while the bank was down finishes after the bank recovers](assets/17_pay_bank_outage_recovered.png)
**Figure: Pay console - payment sent while the bank was down finishes after the bank recovers**

## 4. Live dashboard during the tests

![Dashboard while 100 users send P2P payments: traffic flowing gateway -> payment nodes -> queue -> workers -> bank](assets/03_dashboard_p2p_load.png)
**Figure: Dashboard while 100 users send P2P payments: traffic flowing gateway -> payment nodes -> queue -> workers -> bank**

![Dashboard with the bank switch DOWN: worker circuit breakers OPEN, payments waiting in the queue](assets/04_dashboard_bank_down.png)
**Figure: Dashboard with the bank switch DOWN: worker circuit breakers OPEN, payments waiting in the queue**

![Dashboard after the bank recovered: the deferred payments were re-delivered and settled](assets/05_dashboard_recovered.png)
**Figure: Dashboard after the bank recovered: the deferred payments were re-delivered and settled**

![Dashboard after the whole test run (totals, latest transactions)](assets/06_dashboard_after.png)
**Figure: Dashboard after the whole test run (totals, latest transactions)**

## 5. Phase 3 - Fault tolerance, logging and metrics

![Retries with exponential backoff, circuit breaker OPEN / HALF_OPEN / CLOSED and deferred payments during the bank outage](assets/27_log_resilience.png)
**Figure: Retries with exponential backoff, circuit breaker OPEN / HALF_OPEN / CLOSED and deferred payments during the bank outage**

![Structured logs of one 'Bank flaky (50% errors)' request followed across every service (request_id 72c0cc906ae94548)](assets/24_log_trace_bank_flaky_50_errors.png)
**Figure: Structured logs of one 'Bank flaky (50% errors)' request followed across every service (request_id 72c0cc906ae94548)**

![Structured logs of one 'Bank down, then recovers' request followed across every service (request_id fd055e92f9234e6c)](assets/25_log_trace_bank_down_then_recovers.png)
**Figure: Structured logs of one 'Bank down, then recovers' request followed across every service (request_id fd055e92f9234e6c)**

![Structured logging: one request id followed Gateway -> payment service -> Redis Stream -> worker -> bank -> PostgreSQL](assets/p3_trace_cli.png)
**Figure: Structured logging: one request id followed Gateway -> payment service -> Redis Stream -> worker -> bank -> PostgreSQL**

![Structured logs of one 'P2P transfer' request followed across every service (request_id d187a9390fb3496d)](assets/19_log_trace_p2p_transfer.png)
**Figure: Structured logs of one 'P2P transfer' request followed across every service (request_id d187a9390fb3496d)**

![Structured logs of one 'P2M merchant payment' request followed across every service (request_id 67414d1e5dc14426)](assets/20_log_trace_p2m_merchant_payment.png)
**Figure: Structured logs of one 'P2M merchant payment' request followed across every service (request_id 67414d1e5dc14426)**

![Structured logs of one 'Double tap (idempotent replay)' request followed across every service (request_id 1aeed662d2ca426c)](assets/21_log_trace_double_tap_idempotent_replay.png)
**Figure: Structured logs of one 'Double tap (idempotent replay)' request followed across every service (request_id 1aeed662d2ca426c)**

![Structured logs of one 'Bank decline' request followed across every service (request_id 99a4a355d33d48c2)](assets/22_log_trace_bank_decline.png)
**Figure: Structured logs of one 'Bank decline' request followed across every service (request_id 99a4a355d33d48c2)**

![Structured logs of one 'Insufficient funds' request followed across every service (request_id cb17ff60ea7e4a23)](assets/23_log_trace_insufficient_funds.png)
**Figure: Structured logs of one 'Insufficient funds' request followed across every service (request_id cb17ff60ea7e4a23)**

![Structured logs of one 'Amount above Rs 1 lakh' request followed across every service (request_id 6e03c1db09084d0e)](assets/26_log_trace_amount_above_rs_1_lakh.png)
**Figure: Structured logs of one 'Amount above Rs 1 lakh' request followed across every service (request_id 6e03c1db09084d0e)**

![Metrics: requests, 429s by scope, cache hit ratio, DB queries, settled payments, retries and circuit-breaker openings per node (from GET /stats)](assets/p3_stats.png)
**Figure: Metrics: requests, 429s by scope, cache hit ratio, DB queries, settled payments, retries and circuit-breaker openings per node (from GET /stats)**

## 6. Load testing and automated tests

**100-user functional scenario test** (`scripts/multi_user_test.py`): 259/259 scenario requests behaved as expected, 5/5 consistency checks passed, 228 distinct paying users.

![Console output of the 100-user scenario test](assets/18_console_output.png)
**Figure: Console output of the 100-user scenario test**

![Locust load-test report - 100 concurrent users (60 s): request statistics per endpoint, failures (429) and response-time percentiles](assets/p3_locust_scale-u100.png)
**Figure: Locust load-test report - 100 concurrent users (60 s): request statistics per endpoint, failures (429) and response-time percentiles**

![Locust load-test report - 200 concurrent users (60 s): request statistics per endpoint, failures (429) and response-time percentiles](assets/p3_locust_scale-u200.png)
**Figure: Locust load-test report - 200 concurrent users (60 s): request statistics per endpoint, failures (429) and response-time percentiles**

![Locust load-test report - 500 concurrent users (60 s): request statistics per endpoint, failures (429) and response-time percentiles](assets/p3_locust_scale-u500.png)
**Figure: Locust load-test report - 500 concurrent users (60 s): request statistics per endpoint, failures (429) and response-time percentiles**

![Load test summary: throughput at 100 / 200 / 500 users](assets/scale_throughput.png)
**Figure: Load test summary: throughput at 100 / 200 / 500 users**

![Load test summary: latency percentiles at 100 / 200 / 500 users](assets/scale_latency.png)
**Figure: Load test summary: latency percentiles at 100 / 200 / 500 users**

![Load test: CPU per container - the gateway is the bottleneck](assets/scale_cpu.png)
**Figure: Load test: CPU per container - the gateway is the bottleneck**

![Automated tests: token bucket, Lua script, circuit breaker, retry/backoff, gateway proxy, worker settlement logic, dashboard and Pay Console routes - all pass](assets/p3_pytest.png)
**Figure: Automated tests: token bucket, Lua script, circuit breaker, retry/backoff, gateway proxy, worker settlement logic, dashboard and Pay Console routes - all pass**
