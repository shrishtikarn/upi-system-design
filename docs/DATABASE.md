# Database design (PostgreSQL 16)

Migrations live in [`db/migrations`](../db/migrations) and are applied in order by
[`db/migrate.py`](../db/migrate.py), which runs as the one-shot `migrate` container before any
service starts. Applied files are recorded in `schema_migrations`; each file runs in its own
transaction, and a Postgres advisory lock stops two runners migrating at once.

| File | Contents |
|---|---|
| `001_init_schema.sql` | tables, enum, constraints, indexes |
| `002_seed_demo_data.sql` | 3 demo users + 1,000 load-test users (development data) |
| `003_dashboard_recent_index.sql` | index on `transactions(created_at DESC)` for the dashboard's "latest transactions" |

Add a change as `003_<description>.sql`; never edit an applied file.

## Entity-relationship diagram

```mermaid
erDiagram
    users ||--o{ bank_accounts : owns
    users ||--o{ vpas : registers
    bank_accounts ||--o{ vpas : "addressed by"
    vpas ||--o{ transactions : "pays (payer_vpa)"
    vpas ||--o{ transactions : "receives (payee_vpa)"
    transactions ||--|{ ledger_entries : "posts 1 D + 1 C"
    bank_accounts ||--o{ ledger_entries : "affected"
    transactions ||--|{ transaction_events : "audit trail"

    users {
        bigserial id PK
        text full_name
        varchar phone UK
        timestamptz created_at
    }
    bank_accounts {
        bigserial id PK
        bigint user_id FK
        text bank_name
        varchar ifsc
        char account_last4
        bigint balance_paise "CHECK >= 0"
        timestamptz updated_at
    }
    vpas {
        varchar vpa PK "lower-case, name@handle"
        bigint user_id FK
        bigint account_id FK
        boolean is_active
    }
    transactions {
        uuid txn_id PK
        varchar idempotency_key UK
        varchar payer_vpa FK
        varchar payee_vpa FK
        bigint amount_paise "CHECK > 0"
        txn_status status "PENDING|PROCESSING|SUCCESS|FAILED"
        varchar failure_reason
        varchar bank_ref
        int attempts
        varchar locked_by "worker lease"
        timestamptz locked_at
        varchar request_id "trace id"
        timestamptz created_at
        timestamptz updated_at
        timestamptz completed_at
    }
    ledger_entries {
        bigserial id PK
        uuid txn_id FK
        bigint account_id FK
        char direction "D or C"
        bigint amount_paise
        bigint balance_after_paise
    }
    transaction_events {
        bigserial id PK
        uuid txn_id FK
        txn_status from_status
        txn_status to_status
        varchar actor "node"
        varchar request_id
        jsonb detail
    }
```

## Status machine

```mermaid
stateDiagram-v2
    [*] --> PENDING: POST /payments (INSERT ... ON CONFLICT DO NOTHING)
    PENDING --> PROCESSING: worker CLAIM (lease)
    PROCESSING --> PENDING: bank unavailable / circuit open (RELEASE, message not ACKed)
    PROCESSING --> PROCESSING: lease expired, another worker re-claims
    PROCESSING --> SUCCESS: settle (debit + credit + 2 ledger rows, one DB transaction)
    PROCESSING --> FAILED: declined / insufficient funds / 3 attempts / timeout
    SUCCESS --> [*]
    FAILED --> [*]
```

Every transition is a conditional `UPDATE ... WHERE status IN (...)`, so a stale or duplicate
worker can never move a final transaction again (`SUCCESS`/`FAILED` rows match no update).

## Constraints that protect money

| Constraint | Protects against |
|---|---|
| `bank_accounts.balance_paise CHECK (>= 0)` | overdraft, even if application logic were wrong |
| `transactions.idempotency_key UNIQUE` | double payment on client retry |
| `transactions CHECK (payer_vpa <> payee_vpa)` | paying yourself |
| `transactions.amount_paise CHECK (> 0)` | zero/negative transfers |
| `ledger_entries UNIQUE (txn_id, direction)` | posting a transaction twice |
| foreign keys on every reference | orphan VPAs, transactions, ledger rows |

## Indexes and why

| Index | Serves |
|---|---|
| `transactions (payer_vpa, created_at DESC)` and `(payee_vpa, created_at DESC)` | history query: two index range scans merged with `UNION ALL` |
| `transactions (updated_at) WHERE status IN ('PENDING','PROCESSING')` | sweeper; partial index stays tiny because almost all rows are final |
| `transactions (idempotency_key)` (unique) | idempotent insert / replay lookup |
| `ledger_entries (account_id, created_at DESC)` | account statement |
| `transaction_events (txn_id, id)` | audit trail of one payment |
| `vpas (account_id)`, `bank_accounts (user_id)` | joins |

## Handy queries for the demo

```bash
docker compose exec postgres psql -U upi -d upi
```

```sql
-- latest payments
SELECT txn_id, payer_vpa, payee_vpa, amount_paise/100.0 AS rupees, status, failure_reason, attempts, request_id
FROM transactions ORDER BY created_at DESC LIMIT 10;

-- the audit trail of one payment
SELECT from_status, to_status, actor, detail, created_at FROM transaction_events
WHERE txn_id = '<txn_id>' ORDER BY id;

-- double-entry check: every successful payment has exactly one debit and one credit of the same amount
SELECT t.status, count(*) FILTER (WHERE l.direction='D') AS debits, count(*) FILTER (WHERE l.direction='C') AS credits
FROM transactions t LEFT JOIN ledger_entries l USING (txn_id) GROUP BY t.status;

-- money is conserved: this total never changes, whatever the load test did
SELECT sum(balance_paise) FROM bank_accounts;

-- status breakdown after a load test
SELECT status, failure_reason, count(*) FROM transactions GROUP BY 1, 2 ORDER BY 3 DESC;
```
