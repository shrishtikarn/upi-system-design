"""Connection factories for Postgres (asyncpg) and Redis (redis-py asyncio).

Both retry on startup: inside docker-compose a service can start a few
seconds before Postgres/Redis accept connections.
"""
from __future__ import annotations

import asyncio
from typing import Any

from common.logs import EventLogger


async def create_pg_pool(dsn: str, log: EventLogger, min_size: int = 2, max_size: int = 10, attempts: int = 30) -> Any:
    import asyncpg

    for attempt in range(1, attempts + 1):
        try:
            pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size, command_timeout=10)
            log.info("postgres.connected", pool_min=min_size, pool_max=max_size)
            return pool
        except Exception as exc:  # noqa: BLE001
            delay = min(3.0, 0.25 * 2 ** (attempt - 1))
            log.warning("postgres.connect_failed", attempt=attempt, retry_in_s=delay, error=repr(exc))
            await asyncio.sleep(delay)
    raise RuntimeError("could not connect to Postgres")


def create_redis(url: str, socket_timeout_s: float = 2.0) -> Any:
    import redis.asyncio as redis

    return redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_timeout=socket_timeout_s,
        socket_connect_timeout=socket_timeout_s,
        health_check_interval=30,
    )
