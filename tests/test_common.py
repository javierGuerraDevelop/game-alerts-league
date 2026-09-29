"""Tests for the shared JSON logging and configuration helpers."""

import json
import logging
import sys

import pytest

from common import (
    DEFAULT_DELAY_SECONDS,
    DEFAULT_TTL_DAYS,
    MAX_DELAY_SECONDS,
    MIN_DELAY_SECONDS,
    JsonFormatter,
    configure_logging,
    parse_delay_seconds,
    parse_ttl_days,
    require_env,
)


def make_record(message: str = "hello", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="trolling-time",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_valid_json() -> None:
    payload = json.loads(JsonFormatter().format(make_record("player detected")))

    assert payload["message"] == "player detected"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "trolling-time"
    assert "timestamp" in payload


def test_json_formatter_includes_stable_context_fields() -> None:
    payload = json.loads(
        JsonFormatter().format(
            make_record(
                "detected",
                playerId="Player#NA1",
                matchId="NA1_1",
                executionName="game-NA1_1-abcdef12",
                err="boom",
            )
        )
    )

    assert payload["playerId"] == "Player#NA1"
    assert payload["matchId"] == "NA1_1"
    assert payload["executionName"] == "game-NA1_1-abcdef12"
    assert payload["err"] == "boom"


def test_json_formatter_omits_absent_context_fields() -> None:
    payload = json.loads(JsonFormatter().format(make_record()))

    assert "playerId" not in payload
    assert "matchId" not in payload
    assert "executionName" not in payload
    assert "err" not in payload


def test_configure_logging_attaches_one_json_stdout_handler() -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        configure_logging()
        configure_logging()

        json_handlers = [
            handler
            for handler in root.handlers
            if isinstance(handler.formatter, JsonFormatter)
        ]
        assert len(json_handlers) == 1
        assert isinstance(json_handlers[0], logging.StreamHandler)
        assert json_handlers[0].stream is sys.stdout
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)


def test_require_env_returns_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAYERS_TABLE_NAME", "players")

    assert require_env("PLAYERS_TABLE_NAME") == "players"


def test_require_env_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLAYERS_TABLE_NAME", raising=False)

    with pytest.raises(RuntimeError, match="PLAYERS_TABLE_NAME"):
        require_env("PLAYERS_TABLE_NAME")


def test_require_env_blank_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAYERS_TABLE_NAME", "   ")

    with pytest.raises(RuntimeError, match="PLAYERS_TABLE_NAME"):
        require_env("PLAYERS_TABLE_NAME")


def test_parse_delay_seconds_defaults_to_3600() -> None:
    assert parse_delay_seconds(None) == DEFAULT_DELAY_SECONDS
    assert parse_delay_seconds("") == DEFAULT_DELAY_SECONDS


def test_parse_delay_seconds_accepts_values_in_range() -> None:
    assert parse_delay_seconds(str(MIN_DELAY_SECONDS)) == MIN_DELAY_SECONDS
    assert parse_delay_seconds("7200") == 7200


def test_parse_delay_seconds_clamps_below_minimum(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert parse_delay_seconds("30") == MIN_DELAY_SECONDS

    assert caplog.records
    assert "GAME_STATS_DELAY_SECONDS" in caplog.records[0].message


def test_parse_delay_seconds_clamps_above_maximum(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert parse_delay_seconds(str(MAX_DELAY_SECONDS + 1)) == MAX_DELAY_SECONDS

    assert caplog.records
    assert "GAME_STATS_DELAY_SECONDS" in caplog.records[0].message


def test_parse_delay_seconds_invalid_uses_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert parse_delay_seconds("soon") == DEFAULT_DELAY_SECONDS

    assert caplog.records
    assert "GAME_STATS_DELAY_SECONDS" in caplog.records[0].message


def test_parse_ttl_days_defaults_to_30() -> None:
    assert parse_ttl_days(None) == DEFAULT_TTL_DAYS
    assert parse_ttl_days("") == DEFAULT_TTL_DAYS


def test_parse_ttl_days_accepts_positive_values() -> None:
    assert parse_ttl_days("7") == 7


def test_parse_ttl_days_invalid_uses_default(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert parse_ttl_days("never") == DEFAULT_TTL_DAYS

    assert caplog.records
    assert "STATS_TTL_DAYS" in caplog.records[0].message


def test_parse_ttl_days_non_positive_uses_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert parse_ttl_days("0") == DEFAULT_TTL_DAYS

    assert caplog.records
    assert "STATS_TTL_DAYS" in caplog.records[0].message
