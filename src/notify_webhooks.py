"""Notifier Lambda: deliver SNS game alerts to Discord and email."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import boto3

from common import configure_logging, http_post_json

_LOG = logging.getLogger(__name__)

DISCORD_TIMEOUT_SECONDS = 8.0

HttpPost = Callable[..., tuple[int, Mapping[str, str], str]]


def _rfc3339_timestamp(milliseconds: float | str) -> str:
    moment = datetime.fromtimestamp(int(milliseconds) / 1000, tz=UTC)
    return moment.isoformat().replace("+00:00", "Z")


def build_discord_payload(notification: Mapping[str, Any]) -> dict[str, Any]:
    """Build the Discord webhook payload for a game notification."""
    return {
        "embeds": [
            {
                "title": f"{notification.get('playerName', '')} is in a game!",
                "fields": [
                    {
                        "name": "Game Mode",
                        "value": str(notification.get("gameMode", "")),
                        "inline": True,
                    },
                    {
                        "name": "Champion ID",
                        "value": str(notification.get("championId", 0)),
                        "inline": True,
                    },
                ],
                "timestamp": _rfc3339_timestamp(notification.get("gameStartTime", 0)),
            }
        ]
    }


def send_discord(
    webhook_url: str,
    notification: Mapping[str, Any],
    *,
    http_post: HttpPost = http_post_json,
) -> None:
    """POST the notification to the Discord webhook; non-2xx responses are errors."""
    payload = build_discord_payload(notification)
    status, _headers, body = http_post(
        webhook_url, payload, timeout=DISCORD_TIMEOUT_SECONDS
    )
    if not 200 <= status < 300:
        raise RuntimeError(f"discord webhook returned {status}: {body}")
    _LOG.info("discord notification sent")


def send_email(
    ses_client: Any,
    sender: str,
    recipient: str,
    notification: Mapping[str, Any],
) -> None:
    """Send the notification through SES."""
    player_name = notification.get("playerName", "")
    text_body = (
        f"{player_name} is currently in a {notification.get('gameMode', '')} game "
        f"(Champion ID: {notification.get('championId', 0)}, "
        f"Start Time: {notification.get('gameStartTime', 0)})"
    )
    ses_client.send_email(
        Source=sender,
        Destination={"ToAddresses": [recipient]},
        Message={
            "Subject": {"Data": f"{player_name} is in a game!"},
            "Body": {"Text": {"Data": text_body}},
        },
    )
    _LOG.info("email notification sent to %s", recipient)


def send_notification(
    notification: Mapping[str, Any],
    *,
    discord_url: str,
    ses_client: Any,
    sender_email: str,
    recipient_email: str,
    http_post: HttpPost = http_post_json,
) -> None:
    """Fan one notification out to every configured channel, isolating failures.

    Channel errors are logged, never raised: SNS retries would send duplicates,
    which is worse than a logged, missed message.
    """
    if discord_url:
        try:
            send_discord(discord_url, notification, http_post=http_post)
        except Exception:  # noqa: BLE001 - channel errors must not fail the handler
            _LOG.exception("discord notification failed")
    else:
        _LOG.info("discord webhook not configured; skipping")

    if sender_email and recipient_email:
        try:
            send_email(ses_client, sender_email, recipient_email, notification)
        except Exception:  # noqa: BLE001 - channel errors must not fail the handler
            _LOG.exception("email notification failed")
    else:
        _LOG.info("ses email not fully configured; skipping")


def handle_event(
    event: Mapping[str, Any],
    *,
    discord_url: str,
    ses_client: Any,
    sender_email: str,
    recipient_email: str,
    http_post: HttpPost = http_post_json,
) -> None:
    """Process every SNS record; a malformed record never blocks the rest."""
    for record in event.get("Records", []):
        raw_message = record.get("Sns", {}).get("Message", "")
        try:
            notification = json.loads(raw_message)
        except (TypeError, ValueError) as err:
            _LOG.error("skipping malformed sns message: %s", err)
            continue
        send_notification(
            notification,
            discord_url=discord_url,
            ses_client=ses_client,
            sender_email=sender_email,
            recipient_email=recipient_email,
            http_post=http_post,
        )


def lambda_handler(event, context):
    """Lambda entry point: build real clients and configuration."""
    configure_logging()
    handle_event(
        event,
        discord_url=os.environ.get("DISCORD_WEBHOOK_URL", ""),
        ses_client=boto3.client("ses"),
        sender_email=os.environ.get("SES_SENDER_EMAIL", ""),
        recipient_email=os.environ.get("RECIPIENT_EMAIL", ""),
    )
