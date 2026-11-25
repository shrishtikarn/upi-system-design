"""SQL used by the account service."""
from common.models import TXN_COLUMNS

BALANCE = """
SELECT v.vpa, a.balance_paise, a.bank_name, a.account_last4, a.updated_at
FROM vpas v
JOIN bank_accounts a ON a.id = v.account_id
WHERE v.vpa = $1
"""

# Two index range scans (payer side, payee side) merged, instead of one
# "payer_vpa = $1 OR payee_vpa = $1" that the planner may not index well.
HISTORY = f"""
SELECT {TXN_COLUMNS} FROM (
    (SELECT {TXN_COLUMNS} FROM transactions WHERE payer_vpa = $1 ORDER BY created_at DESC LIMIT $2)
    UNION ALL
    (SELECT {TXN_COLUMNS} FROM transactions WHERE payee_vpa = $1 ORDER BY created_at DESC LIMIT $2)
) t
ORDER BY created_at DESC
LIMIT $2
"""

# --- dashboard (read-only, not cached: the dashboard wants live numbers) ------
RECENT_TXNS = f"SELECT {TXN_COLUMNS} FROM transactions ORDER BY created_at DESC LIMIT $1"

TXN_BY_ID = f"SELECT {TXN_COLUMNS} FROM transactions WHERE txn_id = $1"

STATUS_SUMMARY = """
SELECT status::text AS status, count(*) AS n, coalesce(sum(amount_paise), 0)::bigint AS amount_paise
FROM transactions
GROUP BY status
"""

# The total across all accounts never changes: payments only move money between accounts.
MONEY_SUMMARY = """
SELECT coalesce(sum(balance_paise), 0)::bigint AS total_paise, count(*) AS accounts
FROM bank_accounts
"""

# Payer / payee pickers on the /pay page: named demo users first, then load-test users.
USER_DIRECTORY = """
SELECT v.vpa, u.full_name, a.bank_name, a.balance_paise, v.is_active
FROM vpas v
JOIN users u         ON u.id = v.user_id
JOIN bank_accounts a ON a.id = v.account_id
WHERE ($1 = '' OR v.vpa LIKE '%' || $1 || '%' OR lower(u.full_name) LIKE '%' || $1 || '%')
ORDER BY (v.vpa LIKE '%@mockbank'), v.vpa
LIMIT $2
"""

TXN_EVENTS = """
SELECT id, from_status::text AS from_status, to_status::text AS to_status, actor, request_id,
       detail::text AS detail, created_at
FROM transaction_events
WHERE txn_id = $1
ORDER BY id
"""
