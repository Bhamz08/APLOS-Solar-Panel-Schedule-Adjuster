"""Structured (JSON lines) logging to `logs/` plus readable console output."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            payload.update(ctx)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name}: {record.getMessage()}"
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict) and ctx:
            base += "  " + " ".join(f"{k}={v}" for k, v in ctx.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(log_dir: Path, level: str = "INFO", console: bool = True) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger()
    root.setLevel(level)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "aplos.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)
    if console and sys.stderr is not None:  # pythonw (background task) has no console
        stream = logging.StreamHandler()
        stream.setFormatter(ConsoleFormatter())
        root.addHandler(stream)
    for noisy in ("urllib3", "watchdog", "streamlit"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True
