"""Runs the real Lua token-bucket script against a real Redis.

Skipped automatically when redis-py is not installed or no Redis is reachable
(CI provides one: see .github/workflows/ci.yml). Locally:
    docker compose up -d redis && pip install redis pytest && pytest tests/test_lua_token_bucket_redis.py
"""
import os
import uuid

import pytest

redis = pytest.importorskip("redis")

from gateway.ratelimit import TOKEN_BUCKET_LUA  # noqa: E402


@pytest.fixture()
def client():
    r = redis.Redis.from_url(os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15"), decode_responses=True)
    try:
        r.ping()
    except Exception:  # noqa: BLE001
        pytest.skip("no Redis reachable")
    yield r
    r.close()


def test_lua_bucket_allows_burst_then_rejects(client):
    script = client.register_script(TOKEN_BUCKET_LUA)
    key, glob = f"test:{uuid.uuid4().hex}", f"test:g:{uuid.uuid4().hex}"
    results = [script(keys=[key, glob], args=[1, 5, 1, 1000, 1000]) for _ in range(7)]
    assert [int(r[0]) for r in results] == [1, 1, 1, 1, 1, 0, 0]
    assert int(results[-1][1]) == 1                  # blocked by bucket #1 (client)
    assert 0 < float(results[-1][3]) <= 1.0          # retry_after
    assert client.ttl(key) > 0                       # idle buckets expire


def test_lua_global_bucket_blocks(client):
    script = client.register_script(TOKEN_BUCKET_LUA)
    glob = f"test:g:{uuid.uuid4().hex}"
    allowed = [int(script(keys=[f"test:{uuid.uuid4().hex}", glob], args=[1, 5, 1, 3, 0.1])[0]) for _ in range(5)]
    assert allowed == [1, 1, 1, 0, 0]
