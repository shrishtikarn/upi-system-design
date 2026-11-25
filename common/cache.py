"""Cache-aside helper on top of Redis with graceful degradation.

read path :  GET key  --hit-->  return cached JSON
                      --miss--> load from Postgres -> SET key EX ttl -> return
write path:  the worker DELetes affected keys after it commits a payment, so
             the next read repopulates the cache with fresh data.

If Redis is down every call quietly falls back to the database (and the
"cache_requests_total{result="error"}" metric goes up) instead of failing the
user's request: the cache is an optimisation, never a dependency.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from common.logs import EventLogger
from common.metrics import Metrics


class Cache:
    def __init__(self, redis: Any, metrics: Metrics, log: EventLogger, enabled: bool = True):
        self.redis = redis
        self.metrics = metrics
        self.log = log
        self.enabled = enabled

    async def get_json(self, cache_name: str, key: str) -> Any | None:
        if not self.enabled:
            self.metrics.inc("cache_requests_total", cache=cache_name, result="bypass")
            return None
        try:
            raw = await self.redis.get(key)
        except Exception as exc:  # noqa: BLE001 - any Redis problem => treat as miss
            self.metrics.inc("cache_requests_total", cache=cache_name, result="error")
            self.log.warning("cache.error", op="get", key=key, error=repr(exc))
            return None
        if raw is None:
            self.metrics.inc("cache_requests_total", cache=cache_name, result="miss")
            self.log.debug("cache.miss", key=key)
            return None
        self.metrics.inc("cache_requests_total", cache=cache_name, result="hit")
        self.log.debug("cache.hit", key=key)
        return json.loads(raw)

    async def set_json(self, key: str, value: Any, ttl_s: float) -> None:
        if not self.enabled or ttl_s <= 0:
            return
        try:
            await self.redis.set(key, json.dumps(value, default=str), px=int(ttl_s * 1000))
        except Exception as exc:  # noqa: BLE001
            self.log.warning("cache.error", op="set", key=key, error=repr(exc))

    async def delete(self, *keys: str) -> None:
        if not keys:
            return
        try:
            await self.redis.delete(*keys)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("cache.error", op="delete", keys=list(keys), error=repr(exc))

    async def get_or_load(
        self,
        cache_name: str,
        key: str,
        ttl_s: float,
        loader: Callable[[], Awaitable[Any]],
    ) -> tuple[Any, str]:
        """Returns (value, "HIT"|"MISS"|"BYPASS"). None values are not cached."""
        cached = await self.get_json(cache_name, key)
        if cached is not None:
            return cached, "HIT"
        value = await loader()
        if value is not None:
            await self.set_json(key, value, ttl_s)
        return value, ("MISS" if self.enabled else "BYPASS")
