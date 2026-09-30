"""Tests for the sensor Lambda."""

import json
import logging

import pytest

from common import RateLimitedError
from is_player_in_game import (
    SensorConfig,
    build_execution_name,
    cache_puuid,
    check_players,
    ensure_puuid,
    fetch_active_game,
    load_config,
    resolve_puuid,
    scan_players,
)


class FakeDynamo:
    """DynamoDB client double with scripted scan pages and recorded writes."""

    def __init__(
        self,
        pages: list[dict] | None = None,
        update_error: Exception | None = None,
        put_error: Exception | None = None,
    ) -> None:
        self.pages = list(pages) if pages is not None else [{"Items": []}]
        self.update_error = update_error
        self.put_error = put_error
        self.scan_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.put_calls: list[dict] = []
        self._scan_index = 0

    def scan(self, **kwargs: object) -> dict:
        self.scan_calls.append(kwargs)
        page = self.pages[min(self._scan_index, len(self.pages) - 1)]
        self._scan_index += 1
        return page

    def update_item(self, **kwargs: object) -> None:
        if self.update_error is not None:
            raise self.update_error
        self.update_calls.append(kwargs)

    def put_item(self, **kwargs: object) -> None:
        if self.put_error is not None:
            raise self.put_error
        self.put_calls.append(kwargs)


class FakeHttp:
    """HTTP getter double routing canned responses by URL fragment."""

    def __init__(
        self,
        responses: dict[str, tuple[int, dict, str]] | None = None,
        default: tuple[int, dict, str] = (404, {}, ""),
    ) -> None:
        self.responses = responses or {}
        self.default = default
        self.calls: list[dict] = []

    def __call__(self, url: str, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        for fragment, response in self.responses.items():
            if fragment in url:
                return response
        return self.default


def make_config(**overrides: object) -> SensorConfig:
    values: dict = {
        "players_table": "players",
        "stats_table": "game-stats",
        "state_machine_arn": (
            "arn:aws:states:us-east-1:123456789012:"
            "stateMachine:trolling-time-game-lifecycle"
        ),
        "riot_api_key": "test-api-key",
        "riot_region": "na1",
        "match_region": "americas",
        "delay_seconds": 3600,
    }
    values.update(overrides)
    return SensorConfig(**values)


def set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAYERS_TABLE_NAME", "players")
    monkeypatch.setenv("DYNAMO_TABLE_NAME", "game-stats")
    monkeypatch.setenv(
        "STATE_MACHINE_ARN", "arn:aws:states:us-east-1:1:stateMachine:test"
    )
    monkeypatch.setenv("RIOT_API_KEY", "test-api-key")
    monkeypatch.setenv("MATCH_REGION", "americas")


class FakeSfn:
    """Step Functions client double recording start_execution calls."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict] = []

    def start_execution(self, **kwargs: object) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append(kwargs)


class FakeAwsError(Exception):
    """boto3-style error carrying an AWS error code in .response."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


PLAYER = {
    "playerId": "Player#NA1",
    "gameName": "Player",
    "tagLine": "NA1",
    "puuid": "player-puuid",
}


def active_game(puuid: str = "player-puuid") -> dict:
    return {
        "gameId": 12345,
        "platformId": "NA1",
        "gameMode": "CLASSIC",
        "gameStartTime": 1700000000000,
        "participants": [
            {"puuid": "someone-else", "championId": 1},
            {"puuid": puuid, "championId": 99},
        ],
    }


def spectator_active(game: dict) -> dict:
    return {"spectator/v5": (200, {}, json.dumps(game))}


def test_load_config_requires_core_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "PLAYERS_TABLE_NAME",
        "DYNAMO_TABLE_NAME",
        "STATE_MACHINE_ARN",
        "RIOT_API_KEY",
        "MATCH_REGION",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match="PLAYERS_TABLE_NAME"):
        load_config(None)


def test_load_config_applies_region_and_delay_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_required_env(monkeypatch)
    monkeypatch.delenv("RIOT_REGION", raising=False)
    monkeypatch.delenv("GAME_STATS_DELAY_SECONDS", raising=False)

    config = load_config(None)

    assert config.players_table == "players"
    assert config.match_region == "americas"
    assert config.riot_region == "na1"
    assert config.delay_seconds == 3600


def test_load_config_clamps_too_small_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    set_required_env(monkeypatch)
    monkeypatch.setenv("GAME_STATS_DELAY_SECONDS", "30")

    assert load_config(None).delay_seconds == 60


def test_scan_players_paginates_until_all_pages_are_read() -> None:
    dynamo = FakeDynamo(
        pages=[
            {
                "Items": [{"playerId": "A#1"}],
                "LastEvaluatedKey": {"playerId": "A#1"},
            },
            {"Items": [{"playerId": "B#2"}]},
        ]
    )

    players = scan_players(dynamo, "players")

    assert players == [{"playerId": "A#1"}, {"playerId": "B#2"}]
    assert dynamo.scan_calls == [
        {"TableName": "players"},
        {"TableName": "players", "ExclusiveStartKey": {"playerId": "A#1"}},
    ]


def test_resolve_puuid_calls_the_account_api() -> None:
    http = FakeHttp(
        responses={"account/v1": (200, {}, json.dumps({"puuid": "resolved-puuid"}))}
    )
    player = {"playerId": "Player#NA1", "gameName": "Player", "tagLine": "NA1"}

    puuid = resolve_puuid(player, make_config(), http_get=http)

    assert puuid == "resolved-puuid"
    assert http.calls[0]["url"] == (
        "https://americas.api.riotgames.com/riot/account/v1/"
        "accounts/by-riot-id/Player/NA1"
    )
    assert http.calls[0]["headers"]["X-Riot-Token"] == "test-api-key"
    assert http.calls[0]["timeout"] == 8


def test_resolve_puuid_percent_encodes_name_and_tag() -> None:
    http = FakeHttp(responses={"account/v1": (200, {}, json.dumps({"puuid": "p"}))})
    player = {"playerId": "Faker#KR1", "gameName": "페이커", "tagLine": "KR 1"}

    resolve_puuid(player, make_config(match_region="asia"), http_get=http)

    assert http.calls[0]["url"] == (
        "https://asia.api.riotgames.com/riot/account/v1/"
        "accounts/by-riot-id/%ED%8E%98%EC%9D%B4%EC%BB%A4/KR%201"
    )


def test_resolve_puuid_non_200_raises() -> None:
    http = FakeHttp(responses={"account/v1": (500, {}, "boom")})
    player = {"playerId": "Player#NA1", "gameName": "Player", "tagLine": "NA1"}

    with pytest.raises(RuntimeError, match="500"):
        resolve_puuid(player, make_config(), http_get=http)


def test_resolve_puuid_requires_game_name_and_tag() -> None:
    with pytest.raises(RuntimeError, match="Player#NA1"):
        resolve_puuid({"playerId": "Player#NA1"}, make_config(), http_get=FakeHttp())


def test_cache_puuid_writes_the_resolved_value() -> None:
    dynamo = FakeDynamo()

    cache_puuid(dynamo, "players", "Player#NA1", "resolved-puuid")

    assert dynamo.update_calls == [
        {
            "TableName": "players",
            "Key": {"playerId": "Player#NA1"},
            "UpdateExpression": "SET puuid = :p",
            "ExpressionAttributeValues": {":p": "resolved-puuid"},
        }
    ]


def test_cache_puuid_failure_is_warning_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dynamo = FakeDynamo(update_error=RuntimeError("dynamo down"))

    with caplog.at_level(logging.WARNING):
        cache_puuid(dynamo, "players", "Player#NA1", "resolved-puuid")

    assert any("cache" in record.message.lower() for record in caplog.records)


def test_ensure_puuid_resolves_and_caches_a_missing_value() -> None:
    http = FakeHttp(
        responses={"account/v1": (200, {}, json.dumps({"puuid": "resolved-puuid"}))}
    )
    dynamo = FakeDynamo()
    player = {"playerId": "Player#NA1", "gameName": "Player", "tagLine": "NA1"}

    puuid = ensure_puuid(player, make_config(), dynamo, http_get=http)

    assert puuid == "resolved-puuid"
    assert dynamo.update_calls[0]["ExpressionAttributeValues"] == {
        ":p": "resolved-puuid"
    }


def test_ensure_puuid_uses_the_cached_value() -> None:
    http = FakeHttp()
    dynamo = FakeDynamo()
    player = {
        "playerId": "Player#NA1",
        "gameName": "Player",
        "tagLine": "NA1",
        "puuid": "cached-puuid",
    }

    puuid = ensure_puuid(player, make_config(), dynamo, http_get=http)

    assert puuid == "cached-puuid"
    assert http.calls == []
    assert dynamo.update_calls == []


def test_player_not_in_game_starts_no_execution() -> None:
    dynamo = FakeDynamo(pages=[{"Items": [PLAYER]}])
    sfn = FakeSfn()

    check_players(make_config(), dynamo, sfn, http_get=FakeHttp())

    assert sfn.calls == []
    assert dynamo.put_calls == []


def test_detection_starts_execution_with_expected_name_and_input() -> None:
    dynamo = FakeDynamo(pages=[{"Items": [PLAYER]}])
    sfn = FakeSfn()
    http = FakeHttp(responses=spectator_active(active_game()))

    check_players(make_config(delay_seconds=1800), dynamo, sfn, http_get=http)

    assert len(sfn.calls) == 1
    call = sfn.calls[0]
    assert call["stateMachineArn"] == (
        "arn:aws:states:us-east-1:123456789012:"
        "stateMachine:trolling-time-game-lifecycle"
    )
    assert call["name"] == "game-NA1_12345-player-p"
    assert json.loads(call["input"]) == {
        "matchId": "NA1_12345",
        "puuid": "player-puuid",
        "delaySeconds": 1800,
        "notification": {
            "playerName": "Player",
            "gameMode": "CLASSIC",
            "championId": 99,
            "gameStartTime": 1700000000000,
        },
    }


def test_detection_writes_a_conditional_placeholder() -> None:
    dynamo = FakeDynamo(pages=[{"Items": [PLAYER]}])
    http = FakeHttp(responses=spectator_active(active_game()))

    check_players(make_config(), dynamo, FakeSfn(), http_get=http)

    assert dynamo.put_calls == [
        {
            "TableName": "game-stats",
            "Item": {"matchId": "NA1_12345", "puuid": "player-puuid"},
            "ConditionExpression": (
                "attribute_not_exists(matchId) AND attribute_not_exists(puuid)"
            ),
        }
    ]


def test_notification_defaults_champion_id_when_player_is_absent() -> None:
    game = active_game()
    game["participants"] = [{"puuid": "someone-else", "championId": 1}]
    dynamo = FakeDynamo(pages=[{"Items": [PLAYER]}])
    sfn = FakeSfn()

    check_players(
        make_config(), dynamo, sfn, http_get=FakeHttp(responses=spectator_active(game))
    )

    notification = json.loads(sfn.calls[0]["input"])["notification"]
    assert notification["championId"] == 0


def test_delay_seconds_from_environment_reaches_execution_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_required_env(monkeypatch)
    monkeypatch.setenv("GAME_STATS_DELAY_SECONDS", "1800")
    dynamo = FakeDynamo(pages=[{"Items": [PLAYER]}])
    sfn = FakeSfn()
    http = FakeHttp(responses=spectator_active(active_game()))

    check_players(load_config(None), dynamo, sfn, http_get=http)

    assert json.loads(sfn.calls[0]["input"])["delaySeconds"] == 1800


def test_execution_name_sanitizes_special_characters() -> None:
    name = build_execution_name("NA1 1/2#x", "abcdefgh-rest")

    assert name == "game-NA1-1-2-x-abcdefgh"


def test_execution_name_is_truncated_to_80_characters() -> None:
    name = build_execution_name("x" * 200, "abcdefgh")

    assert len(name) == 80
    assert name.startswith("game-")


def test_already_tracked_is_success_and_skips_placeholder() -> None:
    dynamo = FakeDynamo(pages=[{"Items": [PLAYER]}])
    sfn = FakeSfn(error=FakeAwsError("ExecutionAlreadyExists"))
    http = FakeHttp(responses=spectator_active(active_game()))

    check_players(make_config(), dynamo, sfn, http_get=http)

    assert dynamo.put_calls == []


def test_conditional_check_failure_is_success() -> None:
    dynamo = FakeDynamo(
        pages=[{"Items": [PLAYER]}],
        put_error=FakeAwsError("ConditionalCheckFailedException"),
    )
    sfn = FakeSfn()
    http = FakeHttp(responses=spectator_active(active_game()))

    check_players(make_config(), dynamo, sfn, http_get=http)

    assert len(sfn.calls) == 1


def test_placeholder_write_failure_is_warning_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dynamo = FakeDynamo(
        pages=[{"Items": [PLAYER]}], put_error=RuntimeError("dynamo down")
    )
    sfn = FakeSfn()
    http = FakeHttp(responses=spectator_active(active_game()))

    with caplog.at_level(logging.WARNING):
        check_players(make_config(), dynamo, sfn, http_get=http)

    assert len(sfn.calls) == 1
    assert any("placeholder" in record.message.lower() for record in caplog.records)


def test_start_execution_failure_fails_the_only_player() -> None:
    dynamo = FakeDynamo(pages=[{"Items": [PLAYER]}])
    sfn = FakeSfn(error=RuntimeError("step functions down"))
    http = FakeHttp(responses=spectator_active(active_game()))

    with pytest.raises(RuntimeError, match="all 1 tracked player"):
        check_players(make_config(), dynamo, sfn, http_get=http)


def test_spectator_non_200_fails_the_only_player(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dynamo = FakeDynamo(pages=[{"Items": [PLAYER]}])
    http = FakeHttp(responses={"spectator/v5": (500, {}, "boom")})

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(RuntimeError, match="all 1 tracked player"),
    ):
        check_players(make_config(), dynamo, FakeSfn(), http_get=http)

    assert any("500" in record.message for record in caplog.records)


def test_partial_failure_succeeds_when_one_player_works() -> None:
    bad_player = {
        "playerId": "Bad#1",
        "gameName": "Bad",
        "tagLine": "1",
        "puuid": "bad-puuid",
    }
    dynamo = FakeDynamo(pages=[{"Items": [bad_player, PLAYER]}])
    sfn = FakeSfn()
    http = FakeHttp(
        responses={
            "spectator/v5/active-games/by-summoner/bad-puuid": (500, {}, "boom"),
            "spectator/v5": (200, {}, json.dumps(active_game())),
        }
    )

    check_players(make_config(), dynamo, sfn, http_get=http)

    assert len(sfn.calls) == 1
    assert json.loads(sfn.calls[0]["input"])["puuid"] == "player-puuid"


def test_account_403_mentions_rotating_the_secret() -> None:
    http = FakeHttp(responses={"account/v1": (403, {}, "forbidden")})
    player = {"playerId": "Player#NA1", "gameName": "Player", "tagLine": "NA1"}

    with pytest.raises(RuntimeError, match="rotate"):
        resolve_puuid(player, make_config(), http_get=http)


def test_account_429_is_rate_limited_with_retry_after() -> None:
    http = FakeHttp(responses={"account/v1": (429, {"Retry-After": "17"}, "slow")})
    player = {"playerId": "Player#NA1", "gameName": "Player", "tagLine": "NA1"}

    with pytest.raises(RateLimitedError, match="retry-after=17"):
        resolve_puuid(player, make_config(), http_get=http)


def test_spectator_404_returns_none() -> None:
    http = FakeHttp(responses={"spectator/v5": (404, {}, "")})

    assert fetch_active_game("player-puuid", make_config(), http_get=http) is None


def test_spectator_request_uses_the_platform_region_and_timeout() -> None:
    http = FakeHttp(responses=spectator_active(active_game()))

    fetch_active_game("player-puuid", make_config(riot_region="euw1"), http_get=http)

    assert http.calls[0]["url"] == (
        "https://euw1.api.riotgames.com/lol/spectator/v5/"
        "active-games/by-summoner/player-puuid"
    )
    assert http.calls[0]["timeout"] == 8


def test_spectator_403_mentions_rotating_the_secret() -> None:
    http = FakeHttp(responses={"spectator/v5": (403, {}, "forbidden")})

    with pytest.raises(RuntimeError, match="rotate"):
        fetch_active_game("player-puuid", make_config(), http_get=http)


def test_spectator_429_is_rate_limited_with_retry_after() -> None:
    http = FakeHttp(responses={"spectator/v5": (429, {"Retry-After": "3"}, "slow")})

    with pytest.raises(RateLimitedError, match="retry-after=3"):
        fetch_active_game("player-puuid", make_config(), http_get=http)
