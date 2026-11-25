"""Minimal, dependency-free migration runner.

* Applies db/migrations/NNN_*.sql in lexical order.
* Records applied files in schema_migrations, so re-running is a no-op.
* Each file runs inside its own transaction: a failing migration leaves the
  database exactly as it was.
* A Postgres advisory lock prevents two runners from migrating concurrently.

Run inside docker-compose as the one-shot `migrate` service, or manually:
    docker compose run --rm migrate
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
LOCK_ID = 727_001  # arbitrary, constant


async def connect_with_retry(dsn: str, attempts: int = 30) -> asyncpg.Connection:
    for attempt in range(1, attempts + 1):
        try:
            return await asyncpg.connect(dsn)
        except Exception as exc:  # noqa: BLE001
            print(f"[migrate] postgres not ready ({exc!r}), retry {attempt}/{attempts}", flush=True)
            await asyncio.sleep(1)
    raise SystemExit("[migrate] could not connect to postgres")


async def main() -> int:
    dsn = os.getenv("DATABASE_URL", "postgresql://upi:upi_dev_password@postgres:5432/upi")
    conn = await connect_with_retry(dsn)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", LOCK_ID)
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version     TEXT PRIMARY KEY,
                   applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
               )"""
        )
        applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        pending = [f for f in files if f.name not in applied]
        if not pending:
            print(f"[migrate] up to date ({len(applied)} migrations applied)", flush=True)
        for file in pending:
            print(f"[migrate] applying {file.name} ...", flush=True)
            async with conn.transaction():
                await conn.execute(file.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", file.name)
            print(f"[migrate] applied  {file.name}", flush=True)
        return 0
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_ID)
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
