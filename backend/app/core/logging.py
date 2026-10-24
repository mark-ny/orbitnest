"""Structured logging with secret redaction and rotation."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

REDACTED = "[REDACTED]"
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "csrf",
    "session",
)
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=\-]+")
_KEY_VALUE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|csrf)\b(\s*[=:]\s*)([^\s,;&\"']+)"
)
_URL_CREDENTIALS = re.compile(r"(?i)(https?://)([^/\s:@]+):([^/\s@]+)@")
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def redact_text(text: str) -> str:
    text = _BEARER.sub(r"\1" + REDACTED, text)
    text = _URL_CREDENTIALS.sub(r"\1" + REDACTED + "@", text)
    return _KEY_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)


def redact(value: Any) -> Any:
    """Recursively redact sensitive keys and secret-looking text."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if isinstance(k, str) and is_sensitive_key(k) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class RedactingFilter(logging.Filter):
    """Redacts message text, args and extra fields before any handler formats them."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed format string: keep the raw msg, redacted
            message = str(record.msg)
        record.msg = redact_text(message)
        record.args = ()
        for key in list(record.__dict__):
            if key in _RESERVED:
                continue
            value = record.__dict__[key]
            record.__dict__[key] = REDACTED if is_sensitive_key(key) else redact(value)
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = redact_text(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, default=str, ensure_ascii=False)


_HANDLER_TAG = "_orbitnest_handler"


def configure_logging(settings: Settings) -> logging.Logger:
    """Configure the ``orbitnest`` logger. Safe to call repeatedly (replaces its own handlers)."""
    logger = logging.getLogger("orbitnest")
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(settings.log_level)
    logger.propagate = False

    formatter: logging.Formatter = (
        JsonFormatter()
        if settings.log_json
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if settings.env != "test":
        try:
            settings.logs_path.mkdir(parents=True, exist_ok=True)
            handlers.append(
                logging.handlers.RotatingFileHandler(
                    settings.logs_path / "orbitnest.log",
                    maxBytes=5 * 1024 * 1024,
                    backupCount=5,
                    encoding="utf-8",
                )
            )
        except OSError as exc:
            logging.getLogger("orbitnest").warning("file logging disabled: %s", exc.strerror)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(RedactingFilter())
        setattr(handler, _HANDLER_TAG, True)
        logger.addHandler(handler)
    return logger
