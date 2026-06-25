"""Logging configuration for the remediation engine.

Call configure() once at startup (main.py does this).
All other modules just do:  log = logging.getLogger(__name__)
"""
from __future__ import annotations
import logging
import os
import sys


def configure() -> None:
    """Set up root logger. JSON in production, plain text in dev."""
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    use_json = os.getenv("LOG_FORMAT", "text").lower() == "json"

    if use_json:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    # Quiet noisy libraries
    for noisy in ("httpx", "httpcore", "multipart", "PIL", "fitz"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class _JsonFormatter(logging.Formatter):
    """One JSON object per line -- works with CloudWatch, Datadog, etc."""
    import json as _json

    def format(self, record: logging.LogRecord) -> str:
        import json, traceback
        obj = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            obj["exc"] = traceback.format_exception(*record.exc_info)[-1].strip()
        return json.dumps(obj)
