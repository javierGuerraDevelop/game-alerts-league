"""Sensor Lambda: detect tracked players entering a game and start the lifecycle."""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import boto3

from common import configure_logging, http_get, parse_delay_seconds, require_env

_LOG = logging.getLogger(__name__)

HttpGet = Callable[..., tuple[int, Mapping[str, str], str]]


@dataclass(frozen=True)
class SensorConfig:
    """Runtime configuration for the sensor."""

    players_table: str
    stats_table: str
    state_machine_arn: str
    riot_api_key: str
    riot_region: str
    match_region: str
    delay_seconds: int


def load_config() -> SensorConfig:
    """Load and validate the sensor configuration from the environment."""
    return SensorConfig(
        players_table=require_env("PLAYERS_TABLE_NAME"),
        stats_table=require_env("DYNAMO_TABLE_NAME"),
        state_machine_arn=require_env("STATE_MACHINE_ARN"),
        riot_api_key=require_env("RIOT_API_KEY"),
        riot_region=os.environ.get("RIOT_REGION", "").strip() or "na1",
        match_region=require_env("MATCH_REGION"),
        delay_seconds=parse_delay_seconds(os.environ.get("GAME_STATS_DELAY_SECONDS")),
    )


def scan_players(dynamo_client: Any, table_name: str) -> list[Mapping[str, Any]]:
    """Scan the players table, following pagination until it is exhausted."""
    players: list[Mapping[str, Any]] = []
    start_key: Mapping[str, Any] | None = None
    while True:
        kwargs: dict[str, Any] = {"TableName": table_name}
        if start_key is not None:
            kwargs["ExclusiveStartKey"] = start_key
        page = dynamo_client.scan(**kwargs)
        players.extend(page.get("Items", []))
        start_key = page.get("LastEvaluatedKey")
        if not start_key:
            return players


def resolve_puuid(
    player: Mapping[str, Any], config: SensorConfig, *, http_get: HttpGet = http_get
) -> str:
    """Resolve a player's PUUID through the Account-V1 API."""
    player_id = str(player.get("playerId", ""))
    game_name = str(player.get("gameName", "")).strip()
    tag_line = str(player.get("tagLine", "")).strip()
    if not game_name or not tag_line:
        raise RuntimeError(f"player {player_id} is missing gameName or tagLine")

    name = urllib.parse.quote(game_name, safe="")
    tag = urllib.parse.quote(tag_line, safe="")
    url = (
        f"https://{config.match_region}.api.riotgames.com"
        f"/riot/account/v1/accounts/by-riot-id/{name}/{tag}"
    )
    status, _headers, body = http_get(url, {"X-Riot-Token": config.riot_api_key})
    if status != 200:
        raise RuntimeError(f"account lookup for {player_id} returned {status}: {body}")

    puuid = json.loads(body).get("puuid")
    if not puuid:
        raise RuntimeError(f"account lookup for {player_id} returned no puuid")
    return str(puuid)


def cache_puuid(
    dynamo_client: Any, table_name: str, player_id: str, puuid: str
) -> None:
    """Cache the resolved PUUID; a failed write is a warning only."""
    try:
        dynamo_client.update_item(
            TableName=table_name,
            Key={"playerId": player_id},
            UpdateExpression="SET puuid = :p",
            ExpressionAttributeValues={":p": puuid},
        )
    except Exception as err:  # noqa: BLE001 - caching is best-effort
        _LOG.warning("failed to cache puuid for player %s: %s", player_id, err)


def ensure_puuid(
    player: Mapping[str, Any],
    config: SensorConfig,
    dynamo_client: Any,
    *,
    http_get: HttpGet = http_get,
) -> str:
    """Return the player's cached PUUID, resolving and caching it when missing."""
    puuid = str(player.get("puuid") or "")
    if puuid:
        return puuid

    puuid = resolve_puuid(player, config, http_get=http_get)
    cache_puuid(
        dynamo_client, config.players_table, str(player.get("playerId", "")), puuid
    )
    return puuid


def process_player(
    player: Mapping[str, Any],
    config: SensorConfig,
    dynamo_client: Any,
    sfn_client: Any,
    *,
    http_get: HttpGet = http_get,
) -> None:
    """Check a single tracked player."""
    player_id = str(player.get("playerId", ""))
    ensure_puuid(player, config, dynamo_client, http_get=http_get)
    _LOG.info("checked player %s", player_id)


def check_players(
    config: SensorConfig,
    dynamo_client: Any,
    sfn_client: Any,
    *,
    http_get: HttpGet = http_get,
) -> None:
    """Scan tracked players; a failing player must not stop the others."""
    players = scan_players(dynamo_client, config.players_table)
    if not players:
        _LOG.info("no tracked players to check")
        return

    failures = 0
    for player in players:
        player_id = str(player.get("playerId", ""))
        try:
            process_player(player, config, dynamo_client, sfn_client, http_get=http_get)
        except Exception as err:  # noqa: BLE001 - players are processed independently
            failures += 1
            _LOG.error("player %s failed: %s", player_id, err)

    if failures == len(players):
        raise RuntimeError(f"all {len(players)} tracked player(s) failed")


def lambda_handler(event, context):
    """Lambda entry point: build real clients and configuration."""
    configure_logging()
    config = load_config()
    check_players(config, boto3.client("dynamodb"), boto3.client("stepfunctions"))
