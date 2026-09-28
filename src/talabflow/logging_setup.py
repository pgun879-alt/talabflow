"""Logging configuration.

Structured single-line JSON is used because these logs are meant to be greppable and
machine-readable on a small server without a log-aggregation stack. ``stdlib logging`` is
enough for that; a dependency on ``structlog`` would buy nothing here.

**Customer personal data is never logged.** Log records carry order references, ids, counts
and timings -- never a phone number, an address, or the body of a customer's message. Those are
in the database, where access is controlled, not in a log file that gets copied around.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Render a log record as one line of JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Anything passed via logger.info(..., extra={...}) is included.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def safe_extra(**values: Any) -> dict[str, Any]:
    """Build a ``logging`` ``extra`` mapping that cannot collide with reserved record fields.

    ``Logger.info(..., extra={"created": x})`` raises
    ``KeyError: Attempt to overwrite 'created' in LogRecord`` -- because ``created`` is the
    record's own timestamp. The failure happens *inside the logging call*, so an otherwise
    correct endpoint returns a 500 the moment it tries to log a successful outcome. There are
    more than twenty such names (``name``, ``module``, ``process``, ``message``, ``args``...),
    several of which are natural words to use for domain data.

    This renames any colliding key by appending an underscore, so a log line degrades to a
    slightly odd field name instead of taking the request down.
    """
    return {
        (f"{key}_" if key in _RESERVED else key): value for key, value in values.items()
    }


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Install a single stderr handler at ``level``. Idempotent."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for existing in list(root.handlers):
        root.removeHandler(existing)
    handler = logging.StreamHandler(sys.stderr)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)-8s %(name)s: %(message)s"))
    root.addHandler(handler)
    # Uvicorn's access log duplicates information our own middleware records.
    logging.getLogger("uvicorn.access").propagate = False
