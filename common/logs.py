"""Structured (JSON-lines) logging with request-id propagation.

Every log line looks like:

    {"ts":"2026-11-10T15:01:02.123+00:00","level":"INFO","service":"worker",
     "node":"worker-1","event":"payment.completed","request_id":"3f9c...","txn_id":"..."}

The request id is generated at the API Gateway (or taken from the client's
X-Request-ID header), forwarded to every downstream service as a header,
copied into the Redis Stream message, and stored on the transaction row.
That lets you follow one request through Gateway -> Cache -> Queue -> Worker
-> Database with a single grep (see scripts/demo.py trace <request_id>).
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_SERVICE = os.getenv("SERVICE_NAME", "unknown")
_NODE = os.getenv("NODE_ID", socket.gethostname())


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def service_name() -> str:
    return _SERVICE


def node_id() -> str:
    return _NODE


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": _SERVICE,
            "node": _NODE,
            "event": record.getMessage(),
        }
        rid = request_id_var.get()
        if rid:
            payload["request_id"] = rid
        fields = getattr(record, "fields", None)
        if fields:
            payload.update(fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def setup_logging(service: str | None = None, level: str | None = None) -> None:
    """Route all logging (ours + uvicorn's) to stdout as JSON lines."""
    global _SERVICE
    if service:
        _SERVICE = service
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel((level or os.getenv("LOG_LEVEL", "INFO")).upper())
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = [handler]
        lg.propagate = False
    # Our middleware already logs every request; silence duplicate/noisy loggers.
    for name in ("uvicorn.access", "httpx", "httpcore", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)


class EventLogger:
    """logger.info("payment.created", txn_id=..., amount_paise=...)"""

    def __init__(self, name: str):
        self._logger = logging.getLogger(name)

    def _emit(self, level: int, event: str, fields: dict[str, Any], exc_info: bool = False) -> None:
        if self._logger.isEnabledFor(level):
            self._logger.log(level, event, exc_info=exc_info, extra={"fields": fields})

    def debug(self, event: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, event, fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit(logging.INFO, event, fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit(logging.WARNING, event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, event, fields)

    def exception(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, event, fields, exc_info=True)


def get_logger(name: str) -> EventLogger:
    return EventLogger(name)
