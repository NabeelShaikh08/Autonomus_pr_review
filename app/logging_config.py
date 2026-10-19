"""
Structured JSON logging with per-request trace ID propagation.

Every log line emits a single JSON object. The trace_id field carries
the GitHub delivery ID (X-GitHub-Delivery header) so all log lines for
one PR review can be correlated in any log aggregator.
"""

import json
import logging
import time
import contextvars
from typing import Any

_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default="-"
)


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": _trace_id_var.get("-"),
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj)


def get_trace_id() -> str:
    return _trace_id_var.get("-")


def set_trace_id(trace_id: str) -> None:
    _trace_id_var.set(trace_id)


def configure_logging(log_level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JSONFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(log_level.upper())
