-- =============================================================================
-- 001_init_schema.sql  -  core UPI data model
--
--   users 1---* bank_accounts 1---* vpas            (who owns which money)
--   transactions  *---1 vpas (payer)  *---1 vpas (payee)
--   ledger_entries      double-entry record of every balance change
--   transaction_events  append-only audit log of every status change
--
-- Money is stored as BIGINT paise (1 INR = 100 paise): never use floating
-- point for money.
-- =============================================================================

CREATE TABLE users (
    id          BIGSERIAL PRIMARY KEY,
    full_name   TEXT        NOT NULL,
    phone       VARCHAR(15) NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE bank_accounts (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT      NOT NULL REFERENCES users(id),
    bank_name       TEXT        NOT NULL,
    ifsc            VARCHAR(11) NOT NULL,
    account_last4   CHAR(4)     NOT NULL,           -- never store full account numbers in an app DB
    balance_paise   BIGINT      NOT NULL CHECK (balance_paise >= 0),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_bank_accounts_user ON bank_accounts(user_id);

-- Virtual Payment Address, e.g. alice@okaxis -> one bank account
CREATE TABLE vpas (
    vpa         VARCHAR(255) PRIMARY KEY CHECK (vpa = lower(vpa) AND vpa LIKE '%_@_%'),
    user_id     BIGINT      NOT NULL REFERENCES users(id),
    account_id  BIGINT      NOT NULL REFERENCES bank_accounts(id),
    is_active   BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_vpas_account ON vpas(account_id);

CREATE TYPE txn_status AS ENUM ('PENDING', 'PROCESSING', 'SUCCESS', 'FAILED');

CREATE TABLE transactions (
    txn_id           UUID PRIMARY KEY,
    idempotency_key  VARCHAR(64)  NOT NULL UNIQUE,  -- same key twice => same transaction, never a double debit
    payer_vpa        VARCHAR(255) NOT NULL REFERENCES vpas(vpa),
    payee_vpa        VARCHAR(255) NOT NULL REFERENCES vpas(vpa),
    amount_paise     BIGINT       NOT NULL CHECK (amount_paise > 0),
    note             VARCHAR(100),
    status           txn_status   NOT NULL DEFAULT 'PENDING',
    failure_reason   VARCHAR(64),
    bank_ref         VARCHAR(32),                    -- reference returned by the (mock) NPCI switch
    attempts         INT          NOT NULL DEFAULT 0, -- deliveries that reached the bank call
    locked_by        VARCHAR(64),                    -- worker node holding the processing lease
    locked_at        TIMESTAMPTZ,
    request_id       VARCHAR(64),                    -- X-Request-ID that created it (tracing)
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    completed_at     TIMESTAMPTZ,
    CONSTRAINT chk_distinct_parties CHECK (payer_vpa <> payee_vpa)
);
-- history screens: "my latest transactions"
CREATE INDEX idx_txn_payer_created ON transactions(payer_vpa, created_at DESC);
CREATE INDEX idx_txn_payee_created ON transactions(payee_vpa, created_at DESC);
-- sweeper: small partial index over the (few) unfinished transactions only
CREATE INDEX idx_txn_unfinished ON transactions(updated_at) WHERE status IN ('PENDING', 'PROCESSING');

CREATE TABLE ledger_entries (
    id                   BIGSERIAL PRIMARY KEY,
    txn_id               UUID    NOT NULL REFERENCES transactions(txn_id),
    account_id           BIGINT  NOT NULL REFERENCES bank_accounts(id),
    direction            CHAR(1) NOT NULL CHECK (direction IN ('D', 'C')),   -- Debit / Credit
    amount_paise         BIGINT  NOT NULL CHECK (amount_paise > 0),
    balance_after_paise  BIGINT  NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (txn_id, direction)        -- each transaction posts exactly one debit and one credit
);
CREATE INDEX idx_ledger_account ON ledger_entries(account_id, created_at DESC);

CREATE TABLE transaction_events (
    id           BIGSERIAL PRIMARY KEY,
    txn_id       UUID        NOT NULL REFERENCES transactions(txn_id),
    from_status  txn_status,
    to_status    txn_status  NOT NULL,
    actor        VARCHAR(64) NOT NULL,       -- service/node that made the change
    request_id   VARCHAR(64),
    detail       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_txn_events_txn ON transaction_events(txn_id, id);
