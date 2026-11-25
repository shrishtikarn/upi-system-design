"""SQL and row serializers shared by more than one service."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Mapping

# lower-case handle (2-64 chars) @ lower-case PSP handle, e.g. alice@okaxis, user0001@mockbank
VPA_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}@[a-z][a-z0-9]{1,31}$")

FINAL_STATUSES = ("SUCCESS", "FAILED")

VPA_LOOKUP_SQL = """
SELECT v.vpa, v.is_active, u.full_name, a.bank_name
FROM vpas v
JOIN users u         ON u.id = v.user_id
JOIN bank_accounts a ON a.id = v.account_id
WHERE v.vpa = $1
"""

INSERT_EVENT_SQL = """
INSERT INTO transaction_events (txn_id, from_status, to_status, actor, request_id, detail)
VALUES ($1, $2, $3, $4, $5, $6::jsonb)
"""


def normalize_vpa(vpa: str) -> str:
    return vpa.strip().lower()


def is_valid_vpa(vpa: str) -> bool:
    return bool(VPA_PATTERN.match(vpa))


def rupees(paise: int) -> str:
    """12345 -> "123.45" (integer arithmetic, no float rounding)."""
    sign = "-" if paise < 0 else ""
    paise = abs(int(paise))
    return f"{sign}{paise // 100}.{paise % 100:02d}"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def serialize_vpa(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "vpa": row["vpa"],
        "name": row["full_name"],
        "bank": row["bank_name"],
        "active": row["is_active"],
    }


def serialize_txn(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "txn_id": str(row["txn_id"]),
        "status": row["status"],
        "payer_vpa": row["payer_vpa"],
        "payee_vpa": row["payee_vpa"],
        "amount": rupees(row["amount_paise"]),
        "amount_paise": row["amount_paise"],
        "note": row["note"],
        "failure_reason": row["failure_reason"],
        "bank_ref": row["bank_ref"],
        "attempts": row["attempts"],
        "request_id": row["request_id"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
        "completed_at": _iso(row["completed_at"]),
    }


TXN_COLUMNS = """txn_id, payer_vpa, payee_vpa, amount_paise, note, status, failure_reason, bank_ref,
       attempts, request_id, created_at, updated_at, completed_at"""
