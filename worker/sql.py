"""SQL used by the payment worker."""

# Lease-based claim: only one worker can move a transaction to PROCESSING.
# A PROCESSING row whose lease is older than $3 seconds is considered abandoned
# (the worker crashed) and can be claimed again. Duplicate queue messages are
# therefore harmless - the second claimant simply gets no row back.
CLAIM = """
UPDATE transactions
SET status = 'PROCESSING', locked_by = $2, locked_at = now(), updated_at = now()
WHERE txn_id = $1
  AND (status = 'PENDING'
       OR (status = 'PROCESSING' AND locked_at < now() - make_interval(secs => $3)))
RETURNING txn_id, payer_vpa, payee_vpa, amount_paise, attempts, request_id, created_at,
          (created_at < now() - make_interval(secs => $4)) AS expired
"""

# Give the transaction back to the queue (bank unavailable / circuit open).
RELEASE = """
UPDATE transactions
SET status = 'PENDING', locked_by = NULL, locked_at = NULL,
    attempts = attempts + $3, failure_reason = $2, updated_at = now()
WHERE txn_id = $1 AND status = 'PROCESSING' AND locked_by = $4
RETURNING attempts
"""

MARK_FAILED = """
UPDATE transactions
SET status = 'FAILED', failure_reason = $2, attempts = attempts + $3,
    locked_by = NULL, locked_at = NULL, updated_at = now(), completed_at = now()
WHERE txn_id = $1 AND status IN ('PENDING', 'PROCESSING')
RETURNING created_at, completed_at
"""

PAYER_BALANCE = """
SELECT a.balance_paise
FROM vpas v JOIN bank_accounts a ON a.id = v.account_id
WHERE v.vpa = $1
"""

# --- settlement (one database transaction) ------------------------------------
LOCK_TXN = "SELECT status, locked_by FROM transactions WHERE txn_id = $1 FOR UPDATE"

# Lock both accounts in a fixed order (by id) so two concurrent transfers
# A->B and B->A can never deadlock each other.
LOCK_ACCOUNTS = """
SELECT v.vpa, a.id AS account_id, a.balance_paise
FROM vpas v
JOIN bank_accounts a ON a.id = v.account_id
WHERE v.vpa = ANY($1::varchar[])
ORDER BY a.id
FOR UPDATE OF a
"""

DEBIT = """
UPDATE bank_accounts SET balance_paise = balance_paise - $2, updated_at = now()
WHERE id = $1 RETURNING balance_paise
"""

CREDIT = """
UPDATE bank_accounts SET balance_paise = balance_paise + $2, updated_at = now()
WHERE id = $1 RETURNING balance_paise
"""

INSERT_LEDGER = """
INSERT INTO ledger_entries (txn_id, account_id, direction, amount_paise, balance_after_paise)
VALUES ($1, $2, $3, $4, $5)
"""

MARK_SUCCESS = """
UPDATE transactions
SET status = 'SUCCESS', bank_ref = $2, failure_reason = NULL, attempts = attempts + 1,
    locked_by = NULL, locked_at = NULL, updated_at = now(), completed_at = now()
WHERE txn_id = $1
RETURNING created_at, completed_at
"""

# --- sweeper (safety net for payments whose queue message was never published) --
STALE_UNFINISHED = """
SELECT txn_id, request_id
FROM transactions
WHERE status IN ('PENDING', 'PROCESSING') AND updated_at < now() - make_interval(secs => $1)
ORDER BY updated_at
LIMIT $2
"""
