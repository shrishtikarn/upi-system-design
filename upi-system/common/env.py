"""Tiny helpers for reading typed configuration from environment variables.

Every service is configured exclusively through env vars (12-factor style),
which keeps docker-compose.yml the single place to tune the system.
"""
from __future__ import annotations

import os


def env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_list(name: str, default: str) -> list[str]:
    """Comma-separated list, e.g. "http://a:8000,http://b:8000"."""
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]
