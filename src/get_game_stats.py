"""Stats collector Lambda: fetch a completed match and persist player stats."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import boto3

from common import (
    configure_logging,
    http_get,
    require_env,
    resolve_riot_api_key,
)

_LOG = logging.getLogger(__name__)

HttpGet = Callable[..., tuple[int, Mapping[str, str], str]]


@dataclass(frozen=True)
class StatsConfig:
    """Runtime configuration for the stats collector."""

    match_region: str
    table_name: str
    riot_api_key: str


def load_config(secrets_client: Any) -> StatsConfig:
    """Load and validate the collector configuration from the environment."""
    return StatsConfig(
        match_region=require_env("MATCH_REGION"),
        table_name=require_env("DYNAMO_TABLE_NAME"),
        riot_api_key=resolve_riot_api_key(
            secrets_client, os.environ.get("RIOT_API_KEY_SECRET_ARN", "").strip()
        ),
    )


def fetch_match(
    match_id: str,
    match_region: str,
    api_key: str,
    *,
    http_get: HttpGet = http_get,
) -> Mapping[str, Any]:
    """Fetch a match from Match-V5; any non-200 response is an error."""
    url = f"https://{match_region}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    status, _headers, body = http_get(url, {"X-Riot-Token": api_key})
    if status != 200:
        raise RuntimeError(f"match {match_id} fetch returned {status}: {body}")
    return json.loads(body)


def find_participant(
    match: Mapping[str, Any], match_id: str, puuid: str
) -> Mapping[str, Any]:
    """Return the match participant with the given PUUID, or raise."""
    for participant in match.get("info", {}).get("participants", []):
        if participant.get("puuid") == puuid:
            return participant
    raise RuntimeError(f"puuid {puuid} not found in match {match_id}")


def build_stats_record(
    match_id: str,
    puuid: str,
    match: Mapping[str, Any],
    participant: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the game-stats item written to DynamoDB."""
    info = match.get("info", {})
    return {
        "matchId": match_id,
        "puuid": puuid,
        "championName": participant.get("championName", ""),
        "gameMode": info.get("gameMode", ""),
        "win": participant.get("win", False),
        "kills": participant.get("kills", 0),
        "deaths": participant.get("deaths", 0),
        "assists": participant.get("assists", 0),
        "totalCS": participant.get("totalMinionsKilled", 0)
        + participant.get("neutralMinionsKilled", 0),
        "goldEarned": participant.get("goldEarned", 0),
        "totalDamageDealtToChampions": participant.get(
            "totalDamageDealtToChampions", 0
        ),
        "visionScore": participant.get("visionScore", 0),
        "champLevel": participant.get("champLevel", 0),
        "teamPosition": participant.get("teamPosition", ""),
        "gameDuration": info.get("gameDuration", 0),
        "item0": participant.get("item0", 0),
        "item1": participant.get("item1", 0),
        "item2": participant.get("item2", 0),
        "item3": participant.get("item3", 0),
        "item4": participant.get("item4", 0),
        "item5": participant.get("item5", 0),
        "item6": participant.get("item6", 0),
    }


def store_stats(dynamo_client: Any, table_name: str, record: Mapping[str, Any]) -> None:
    """Write the stats item, overwriting any placeholder with the same key."""
    dynamo_client.put_item(TableName=table_name, Item=dict(record))


def collect_stats(
    event: Mapping[str, Any],
    config: StatsConfig,
    dynamo_client: Any,
    *,
    http_get: HttpGet = http_get,
) -> None:
    """Validate the event, fetch the match, and persist the player's stats."""
    match_id = str(event.get("matchId") or "").strip()
    puuid = str(event.get("puuid") or "").strip()
    if not match_id or not puuid:
        raise ValueError("matchId and puuid are required")

    match = fetch_match(
        match_id, config.match_region, config.riot_api_key, http_get=http_get
    )
    participant = find_participant(match, match_id, puuid)
    record = build_stats_record(match_id, puuid, match, participant)
    store_stats(dynamo_client, config.table_name, record)
    _LOG.info("stored stats for match %s", match_id)


def lambda_handler(event, context):
    """Lambda entry point: build real clients and configuration."""
    configure_logging()
    config = load_config(boto3.client("secretsmanager"))
    collect_stats(event, config, boto3.client("dynamodb"))
