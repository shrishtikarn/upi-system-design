"""SQL used by the payment service (kept separate so it can be tested directly against Postgres)."""
from common.models import TXN_COLUMNS

# ON CONFLICT DO NOTHING + RETURNING: returns a row only if *this* call created
# the transaction. A retry with the same Idempotency-Key returns nothing and
# we then read back the original - the database guarantees no duplicate.
INSERT_TXN = """
INSERT INTO transactions (txn_id, idempotency_key, payer_vpa, payee_vpa, amount_paise, note, status, request_id)
VALUES ($1, $2, $3, $4, $5, $6, 'PENDING', $7)
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING """ + TXN_COLUMNS

SELECT_TXN_BY_IDEMPOTENCY_KEY = f"SELECT {TXN_COLUMNS} FROM transactions WHERE idempotency_key = $1"

SELECT_TXN = f"SELECT {TXN_COLUMNS} FROM transactions WHERE txn_id = $1"
