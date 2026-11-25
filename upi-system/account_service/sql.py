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
