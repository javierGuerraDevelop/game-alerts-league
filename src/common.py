"""Shared helpers: JSON logging and environment configuration."""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

DEFAULT_DELAY_SECONDS = 3600
MIN_DELAY_SECONDS = 60
MAX_DELAY_SECONDS = 31_536_000
DEFAULT_TTL_DAYS = 30

_LOG = logging.getLogger(__name__)

_CONTEXT_FIELDS = ("playerId", "matchId", "executionName", "err")


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON for CloudWatch Logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Attach a single JSON stdout handler to the root logger.

    Safe to call more than once: an existing JSON handler is reused.
    """
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler.formatter, JsonFormatter):
            root.setLevel(level)
            return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)


def require_env(name: str) -> str:
    """Return a required environment variable, failing fast when missing or blank."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def parse_delay_seconds(raw: str | None) -> int:
    """Parse GAME_STATS_DELAY_SECONDS, clamping to the allowed range."""
    if raw is None or not raw.strip():
        return DEFAULT_DELAY_SECONDS
    try:
        value = int(raw.strip())
    except ValueError:
        _LOG.warning(
            "GAME_STATS_DELAY_SECONDS=%r is not an integer; using %d",
            raw,
            DEFAULT_DELAY_SECONDS,
        )
        return DEFAULT_DELAY_SECONDS
    if value < MIN_DELAY_SECONDS:
        _LOG.warning(
            "GAME_STATS_DELAY_SECONDS=%d is below the minimum; clamping to %d",
            value,
            MIN_DELAY_SECONDS,
        )
        return MIN_DELAY_SECONDS
    if value > MAX_DELAY_SECONDS:
        _LOG.warning(
            "GAME_STATS_DELAY_SECONDS=%d is above the maximum; clamping to %d",
            value,
            MAX_DELAY_SECONDS,
        )
        return MAX_DELAY_SECONDS
    return value


def parse_ttl_days(raw: str | None) -> int:
    """Parse STATS_TTL_DAYS, falling back to the default for invalid input."""
    if raw is None or not raw.strip():
        return DEFAULT_TTL_DAYS
    try:
        value = int(raw.strip())
    except ValueError:
        _LOG.warning(
            "STATS_TTL_DAYS=%r is not an integer; using %d", raw, DEFAULT_TTL_DAYS
        )
        return DEFAULT_TTL_DAYS
    if value <= 0:
        _LOG.warning(
            "STATS_TTL_DAYS=%d is not positive; using %d", value, DEFAULT_TTL_DAYS
        )
        return DEFAULT_TTL_DAYS
    return value


def http_get(
    url: str,
    headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> tuple[int, Mapping[str, str], str]:
    """Perform an HTTP GET and return (status, headers, body).

    HTTP error responses are returned instead of raised; transport failures
    (DNS, connection errors, timeouts) still propagate.
    """
    request = urllib.request.Request(url, headers=dict(headers or {}))
    return _perform(request, timeout)


def http_post_json(
    url: str,
    payload: Mapping[str, Any],
    timeout: float | None = None,
) -> tuple[int, Mapping[str, str], str]:
    """POST the payload as JSON and return (status, headers, body)."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return _perform(request, timeout)


def _perform(
    request: urllib.request.Request, timeout: float | None
) -> tuple[int, Mapping[str, str], str]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers, response.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        return err.code, err.headers, body
