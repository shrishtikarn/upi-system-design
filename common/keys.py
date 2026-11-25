"""Redis key names shared by producers (services) and consumers (worker).

Keeping them in one module guarantees that the worker invalidates exactly the
cache keys the read services populate.
"""
from __future__ import annotations

# --- Queue (Redis Streams) ---------------------------------------------------
PAYMENTS_STREAM = "upi:payments:stream"      # main work queue
PAYMENTS_GROUP = "payment-workers"           # consumer group shared by all worker nodes
PAYMENTS_DLQ = "upi:payments:dlq"            # dead-letter stream for poison/expired messages
SWEEPER_LOCK = "upi:lock:pending-sweeper"    # so only one worker runs the sweeper at a time


# --- Cache (cache-aside, every key has a TTL) ---------------------------------
def balance_key(vpa: str) -> str:
    return f"cache:balance:{vpa.lower()}"


def vpa_key(vpa: str) -> str:
    return f"cache:vpa:{vpa.lower()}"


def history_key(vpa: str) -> str:
    """Only the default page (latest N transactions) is cached."""
    return f"cache:history:{vpa.lower()}"


def txn_key(txn_id: str) -> str:
    return f"cache:txn:{txn_id}"


# --- Rate limiter ---------------------------------------------------------------
def rl_client_key(client_id: str, policy: str) -> str:
    return f"rl:client:{policy}:{client_id}"


RL_GLOBAL_KEY = "rl:global"
