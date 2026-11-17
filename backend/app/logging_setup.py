"""Structured logging via structlog (JSON in production, console in dev)."""

from __future__ import annotations

import logging
import re
import sys

import structlog

_SECRET_KEYS = re.compile(r"(password|secret|token|cookie|authorization|csrf)", re.I)


def _redact(_logger, _name, event_dict):
    for key in list(event_dict.keys()):
        if _SECRET_KEYS.search(key) and key not in {"event"}:
            event_dict[key] = "[redacted]"
    return event_dict


def configure_logging(level: str = "INFO", json: bool = True) -> None:
    lvl = getattr(logging, level.upper(), logging.INFO)
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        _redact,
    ]
    renderer = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(lvl)
    # Keep noisy libraries quiet; never log request URLs with query params at INFO.
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
