"""Sensor Lambda: detect tracked players entering a game and start the lifecycle."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import boto3

from common import (
    RIOT_TIMEOUT_SECONDS,
    aws_error_code,
    configure_logging,
    http_get,
    parse_delay_seconds,
    require_env,
    resolve_riot_api_key,
    riot_api_error,
)

_LOG = logging.getLogger(__name__)

HttpGet = Callable[..., tuple[int, Mapping[str, str], str]]

MAX_EXECUTION_NAME_LENGTH = 80

_UNSAFE_EXECUTION_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")


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


def load_config(secrets_client: Any) -> SensorConfig:
    """Load and validate the sensor configuration from the environment."""
    return SensorConfig(
        players_table=require_env("PLAYERS_TABLE_NAME"),
        stats_table=require_env("DYNAMO_TABLE_NAME"),
        state_machine_arn=require_env("STATE_MACHINE_ARN"),
        riot_api_key=resolve_riot_api_key(
            secrets_client, os.environ.get("RIOT_API_KEY_SECRET_ARN", "").strip()
        ),
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
    status, headers, body = http_get(
        url, {"X-Riot-Token": config.riot_api_key}, timeout=RIOT_TIMEOUT_SECONDS
    )
    if status != 200:
        raise riot_api_error(f"account lookup for {player_id}", status, headers, body)

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


def fetch_active_game(
    puuid: str, config: SensorConfig, *, http_get: HttpGet = http_get
) -> Mapping[str, Any] | None:
    """Return the player's active game; 404 means the player is not in a game."""
    url = (
        f"https://{config.riot_region}.api.riotgames.com"
        f"/lol/spectator/v5/active-games/by-summoner/{puuid}"
    )
    status, headers, body = http_get(
        url, {"X-Riot-Token": config.riot_api_key}, timeout=RIOT_TIMEOUT_SECONDS
    )
    if status == 404:
        return None
    if status != 200:
        raise riot_api_error(f"spectator lookup for {puuid}", status, headers, body)
    return json.loads(body)


def build_match_id(active_game: Mapping[str, Any]) -> str:
    """Build the match id from the Spectator game identifiers."""
    platform_id = str(active_game.get("platformId") or "")
    game_id = active_game.get("gameId")
    if not platform_id or game_id is None:
        raise RuntimeError("spectator response is missing platformId or gameId")
    return f"{platform_id}_{game_id}"


def build_notification(
    player_name: str, active_game: Mapping[str, Any], puuid: str
) -> dict[str, Any]:
    """Build the alert message for a detected game."""
    champion_id = 0
    for participant in active_game.get("participants", []):
        if participant.get("puuid") == puuid:
            champion_id = participant.get("championId", 0)
            break
    return {
        "playerName": player_name,
        "gameMode": active_game.get("gameMode", ""),
        "championId": champion_id,
        "gameStartTime": active_game.get("gameStartTime", 0),
    }


def build_execution_name(match_id: str, puuid: str) -> str:
    """Build the deterministic, sanitized Step Functions execution name."""
    raw_name = f"game-{match_id}-{puuid[:8]}"
    sanitized = _UNSAFE_EXECUTION_NAME_CHARS.sub("-", raw_name)
    return sanitized[:MAX_EXECUTION_NAME_LENGTH]


def start_game_execution(
    sfn_client: Any,
    config: SensorConfig,
    execution_name: str,
    execution_input: Mapping[str, Any],
) -> bool:
    """Start the lifecycle execution; return False when it already exists."""
    try:
        sfn_client.start_execution(
            stateMachineArn=config.state_machine_arn,
            name=execution_name,
            input=json.dumps(execution_input),
        )
    except Exception as err:
        if aws_error_code(err) == "ExecutionAlreadyExists":
            return False
        raise
    return True


def write_placeholder(
    dynamo_client: Any, table_name: str, match_id: str, puuid: str
) -> None:
    """Write the best-effort placeholder item; write failures are warnings only."""
    try:
        dynamo_client.put_item(
            TableName=table_name,
            Item={"matchId": match_id, "puuid": puuid},
            ConditionExpression=(
                "attribute_not_exists(matchId) AND attribute_not_exists(puuid)"
            ),
        )
    except Exception as err:  # noqa: BLE001 - placeholder writes are best-effort
        if aws_error_code(err) != "ConditionalCheckFailedException":
            _LOG.warning("placeholder write failed for %s: %s", match_id, err)


def process_player(
    player: Mapping[str, Any],
    config: SensorConfig,
    dynamo_client: Any,
    sfn_client: Any,
    *,
    http_get: HttpGet = http_get,
) -> None:
    """Check a tracked player and start the lifecycle when a game is active."""
    player_id = str(player.get("playerId", ""))
    puuid = ensure_puuid(player, config, dynamo_client, http_get=http_get)

    active_game = fetch_active_game(puuid, config, http_get=http_get)
    if active_game is None:
        _LOG.info("player %s is not in a game", player_id)
        return

    match_id = build_match_id(active_game)
    execution_name = build_execution_name(match_id, puuid)
    execution_input = {
        "matchId": match_id,
        "puuid": puuid,
        "delaySeconds": config.delay_seconds,
        "notification": build_notification(
            str(player.get("gameName", "")), active_game, puuid
        ),
    }
    if not start_game_execution(sfn_client, config, execution_name, execution_input):
        _LOG.info("game %s already tracked for player %s", match_id, player_id)
        return

    write_placeholder(dynamo_client, config.stats_table, match_id, puuid)
    _LOG.info("started lifecycle for match %s", match_id)


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
    config = load_config(boto3.client("secretsmanager"))
    check_players(config, boto3.client("dynamodb"), boto3.client("stepfunctions"))
