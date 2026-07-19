"""Structured JSON logging for the DeepBox server.

The server runs behind Azure App Service and on local machines. In both
environments operators want machine-parseable logs that can be shipped to a log
aggregator without a bespoke parser. This module provides a :class:`JsonFormatter`
that renders each log record as a single-line JSON object, plus helpers to
configure the root logger and to emit structured events with arbitrary fields.

Design goals:

* One JSON object per line (newline-delimited JSON) so downstream tooling can
  ``split`` on newlines.
* Deterministic, stable key ordering so tests and humans can read diffs.
* Never raise from logging: a formatting failure must not crash a request.
* Never leak secrets. Callers own field selection; this module simply serialises
  what it is given, but :func:`log_event` drops values that are ``None``.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from typing import Any

# Attributes that ``logging.LogRecord`` sets by default. Anything *not* in this
# set was attached by the caller via ``extra=`` and should be surfaced as a
# structured field.
_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


def _isoformat(created: float) -> str:
    """Return an ISO-8601 UTC timestamp (millisecond precision, ``Z`` suffix)."""

    dt = _dt.datetime.fromtimestamp(created, tz=_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        payload: dict[str, Any] = {
            "ts": _isoformat(record.created),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Surface caller-provided structured fields (``extra=``). Skip private
        # names and anything that collides with our core keys.
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            if key in payload:
                continue
            payload[key] = _safe(value)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _safe(value: Any) -> Any:
    """Coerce a value into something ``json.dumps`` can serialise."""

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    return repr(value)


def configure_logging(level: str | int = "INFO") -> None:
    """Install :class:`JsonFormatter` on the root logger.

    Idempotent: repeated calls replace the handler rather than stacking new
    ones, which matters because uvicorn workers may import the app more than
    once during reloads/tests.
    """

    root = logging.getLogger()
    if isinstance(level, str):
        level = logging.getLevelName(level.upper())
    root.setLevel(level)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    # Remove any handler we previously installed so log lines are not doubled.
    for existing in list(root.handlers):
        if getattr(existing, "_deepbox_json", False):
            root.removeHandler(existing)
    handler._deepbox_json = True  # type: ignore[attr-defined]
    root.addHandler(handler)

    # uvicorn installs its own handlers; route them through the root logger so
    # access/error lines are JSON too.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    # httpx logs every request at INFO; that is noise for a server process.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Emit a structured event.

    ``event`` becomes both the human message and an ``event`` field so logs can
    be filtered either way. ``None`` fields are dropped to avoid noise and to
    reduce the risk of accidentally serialising an unset secret.
    """

    extra = {"event": event}
    for key, value in fields.items():
        if value is None:
            continue
        extra[key] = value
    logger.log(level, event, extra=extra)
