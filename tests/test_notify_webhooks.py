"""Tests for the notifier Lambda."""

import json
import logging

import pytest

from notify_webhooks import build_discord_payload, handle_event

NOTIFICATION = {
    "playerName": "Player",
    "gameMode": "CLASSIC",
    "championId": 99,
    "gameStartTime": 1700000000000,
}


class FakeSes:
    """Minimal SES client double that records send_email calls."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.messages: list[dict] = []

    def send_email(self, **kwargs: object) -> None:
        if self.error is not None:
            raise self.error
        self.messages.append(kwargs)


class FakePoster:
    """HTTP poster double returning a canned (status, headers, body) result."""

    def __init__(self, status: int = 204, body: str = "") -> None:
        self.status = status
        self.body = body
        self.calls: list[dict] = []

    def __call__(self, url: str, payload: dict, timeout: float | None = None):
        self.calls.append({"url": url, "payload": payload, "timeout": timeout})
        return self.status, {}, self.body


def sns_event(*messages: str) -> dict:
    return {"Records": [{"Sns": {"Message": message}} for message in messages]}


def run_handler(
    event: dict,
    poster: FakePoster,
    ses: FakeSes,
    *,
    discord_url: str = "https://discord.test/hook",
    sender: str = "sender@test",
    recipient: str = "recipient@test",
) -> None:
    handle_event(
        event,
        discord_url=discord_url,
        ses_client=ses,
        sender_email=sender,
        recipient_email=recipient,
        http_post=poster,
    )


def test_sends_to_discord_and_email_when_both_configured() -> None:
    poster = FakePoster()
    ses = FakeSes()

    run_handler(sns_event(json.dumps(NOTIFICATION)), poster, ses)

    assert len(poster.calls) == 1
    assert len(ses.messages) == 1


def test_discord_payload_contains_expected_embed() -> None:
    payload = build_discord_payload(NOTIFICATION)

    assert payload == {
        "embeds": [
            {
                "title": "Player is in a game!",
                "fields": [
                    {"name": "Game Mode", "value": "CLASSIC", "inline": True},
                    {"name": "Champion ID", "value": "99", "inline": True},
                ],
                "timestamp": "2023-11-14T22:13:20Z",
            }
        ]
    }


def test_discord_request_uses_the_configured_url_and_timeout() -> None:
    poster = FakePoster()

    run_handler(sns_event(json.dumps(NOTIFICATION)), poster, FakeSes())

    assert poster.calls[0]["url"] == "https://discord.test/hook"
    assert poster.calls[0]["timeout"] == 8


def test_email_subject_and_body() -> None:
    ses = FakeSes()

    run_handler(sns_event(json.dumps(NOTIFICATION)), FakePoster(), ses)

    message = ses.messages[0]
    assert message["Source"] == "sender@test"
    assert message["Destination"] == {"ToAddresses": ["recipient@test"]}
    assert message["Message"]["Subject"]["Data"] == "Player is in a game!"
    assert message["Message"]["Body"]["Text"]["Data"] == (
        "Player is currently in a CLASSIC game "
        "(Champion ID: 99, Start Time: 1700000000000)"
    )


def test_discord_failure_does_not_prevent_email(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ses = FakeSes()

    with caplog.at_level(logging.ERROR):
        run_handler(
            sns_event(json.dumps(NOTIFICATION)),
            FakePoster(status=500, body="boom"),
            ses,
        )

    assert len(ses.messages) == 1
    assert any("discord" in record.message for record in caplog.records)


def test_email_failure_does_not_fail_the_handler(
    caplog: pytest.LogCaptureFixture,
) -> None:
    poster = FakePoster()

    with caplog.at_level(logging.ERROR):
        run_handler(
            sns_event(json.dumps(NOTIFICATION)),
            poster,
            FakeSes(error=RuntimeError("ses is down")),
        )

    assert len(poster.calls) == 1
    assert any("email" in record.message.lower() for record in caplog.records)


def test_malformed_record_is_skipped_and_later_records_processed() -> None:
    poster = FakePoster()
    ses = FakeSes()

    run_handler(sns_event("not json", json.dumps(NOTIFICATION)), poster, ses)

    assert len(poster.calls) == 1
    assert len(ses.messages) == 1


def test_empty_configuration_skips_every_channel() -> None:
    poster = FakePoster()
    ses = FakeSes()

    run_handler(
        sns_event(json.dumps(NOTIFICATION)),
        poster,
        ses,
        discord_url="",
        sender="",
        recipient="",
    )

    assert poster.calls == []
    assert ses.messages == []


def test_email_is_skipped_unless_both_addresses_are_set() -> None:
    ses = FakeSes()

    run_handler(sns_event(json.dumps(NOTIFICATION)), FakePoster(), ses, recipient="")

    assert ses.messages == []


def test_non_2xx_discord_response_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        run_handler(
            sns_event(json.dumps(NOTIFICATION)),
            FakePoster(status=500, body="boom"),
            FakeSes(),
        )

    assert any("discord" in record.message for record in caplog.records)
