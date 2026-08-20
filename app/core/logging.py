"""Structured logging.

Every log line carries whatever identifiers are bound to the current context —
``request_id``, ``operation_id``, ``client_id``, ``bot_id``, ``user_id`` — so a
single Dream decision can be traced end to end without threading a logger
argument through the whole call stack.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import structlog

_request_id: ContextVar[str | None] = ContextVar("dream_request_id", default=None)
_operation_id: ContextVar[str | None] = ContextVar("dream_operation_id", default=None)
_scope_fields: ContextVar[dict[str, str] | None] = ContextVar("dream_scope", default=None)

_configured = False


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:24]}"


def new_operation_id() -> str:
    return f"op_{uuid.uuid4().hex[:24]}"


def _context_processor(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    request_id = _request_id.get()
    if request_id:
        event_dict.setdefault("request_id", request_id)
    operation_id = _operation_id.get()
    if operation_id:
        event_dict.setdefault("operation_id", operation_id)
    scope = _scope_fields.get()
    if scope:
        for key, value in scope.items():
            event_dict.setdefault(key, value)
    return event_dict


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    """Configure structlog and the stdlib root logger. Idempotent."""
    global _configured
    if _configured:
        return

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
        force=True,
    )
    # These are chatty at INFO and say nothing we do not already log ourselves.
    for noisy in ("httpx", "httpcore", "openai", "pymongo", "asyncio"):
        logging.getLogger(noisy).setLevel(max(numeric_level, logging.WARNING))

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _context_processor,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[return-value]


def set_request_id(request_id: str | None) -> None:
    _request_id.set(request_id)


def set_operation_id(operation_id: str | None) -> None:
    _operation_id.set(operation_id)


def bind_scope(scope) -> None:
    """Bind a `MemoryScope` to the log context. Unset dimensions are omitted."""
    _scope_fields.set(scope.as_log_fields())


def clear_scope() -> None:
    _scope_fields.set(None)


@contextmanager
def operation_context(operation_id: str | None = None) -> Iterator[str]:
    """Bind an operation id for the duration of a Dream operation."""
    op_id = operation_id or new_operation_id()
    token = _operation_id.set(op_id)
    try:
        yield op_id
    finally:
        _operation_id.reset(token)
