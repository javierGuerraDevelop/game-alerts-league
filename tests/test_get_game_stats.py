"""Tests for the stats collector Lambda."""

import json

import pytest

from common import RateLimitedError
from get_game_stats import StatsConfig, collect_stats

MATCH = {
    "metadata": {"matchId": "NA1_12345"},
    "info": {
        "gameMode": "CLASSIC",
        "gameDuration": 1830,
        "participants": [
            {
                "puuid": "other-puuid",
                "championName": "Lux",
                "teamPosition": "TOP",
                "win": False,
                "kills": 0,
                "deaths": 9,
                "assists": 1,
                "totalMinionsKilled": 50,
                "neutralMinionsKilled": 2,
                "goldEarned": 4000,
                "totalDamageDealtToChampions": 5000,
                "visionScore": 3,
                "champLevel": 9,
                "item0": 1001,
                "item1": 0,
                "item2": 0,
                "item3": 0,
                "item4": 0,
                "item5": 0,
                "item6": 0,
            },
            {
                "puuid": "test-puuid",
                "championName": "Ahri",
                "teamPosition": "MIDDLE",
                "win": True,
                "kills": 5,
                "deaths": 2,
                "assists": 7,
                "totalMinionsKilled": 150,
                "neutralMinionsKilled": 10,
                "goldEarned": 12345,
                "totalDamageDealtToChampions": 25000,
                "visionScore": 21,
                "champLevel": 16,
                "item0": 3157,
                "item1": 6653,
                "item2": 3020,
                "item3": 4645,
                "item4": 3089,
                "item5": 3135,
                "item6": 3340,
            },
        ],
    },
}

EXPECTED_ITEM = {
    "matchId": "NA1_12345",
    "puuid": "test-puuid",
    "championName": "Ahri",
    "gameMode": "CLASSIC",
    "win": True,
    "kills": 5,
    "deaths": 2,
    "assists": 7,
    "totalCS": 160,
    "goldEarned": 12345,
    "totalDamageDealtToChampions": 25000,
    "visionScore": 21,
    "champLevel": 16,
    "teamPosition": "MIDDLE",
    "gameDuration": 1830,
    "item0": 3157,
    "item1": 6653,
    "item2": 3020,
    "item3": 4645,
    "item4": 3089,
    "item5": 3135,
    "item6": 3340,
}


class FakeDynamo:
    """Minimal DynamoDB client double that records put_item calls."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.put_calls: list[dict] = []

    def put_item(self, **kwargs: object) -> None:
        if self.error is not None:
            raise self.error
        self.put_calls.append(kwargs)


class FakeHttp:
    """HTTP getter double returning a canned (status, headers, body) result."""

    def __init__(
        self,
        status: int = 200,
        body: str = "",
        response_headers: dict | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.response_headers = response_headers or {}
        self.calls: list[dict] = []

    def __call__(self, url: str, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        return self.status, self.response_headers, self.body


def make_config() -> StatsConfig:
    return StatsConfig(
        match_region="americas",
        table_name="game-stats",
        riot_api_key="test-api-key",
    )


def test_success_persists_all_fields() -> None:
    http = FakeHttp(body=json.dumps(MATCH))
    dynamo = FakeDynamo()

    collect_stats(
        {"matchId": "NA1_12345", "puuid": "test-puuid"},
        make_config(),
        dynamo,
        http_get=http,
    )

    assert len(dynamo.put_calls) == 1
    call = dynamo.put_calls[0]
    assert call["TableName"] == "game-stats"
    assert call["Item"] == EXPECTED_ITEM


def test_match_request_targets_the_regional_route() -> None:
    http = FakeHttp(body=json.dumps(MATCH))

    collect_stats(
        {"matchId": "NA1_12345", "puuid": "test-puuid"},
        make_config(),
        FakeDynamo(),
        http_get=http,
    )

    assert http.calls[0]["url"] == (
        "https://americas.api.riotgames.com/lol/match/v5/matches/NA1_12345"
    )
    assert http.calls[0]["headers"]["X-Riot-Token"] == "test-api-key"
    assert http.calls[0]["timeout"] == 8


def test_unknown_event_fields_are_ignored() -> None:
    http = FakeHttp(body=json.dumps(MATCH))
    dynamo = FakeDynamo()

    collect_stats(
        {
            "matchId": "NA1_12345",
            "puuid": "test-puuid",
            "delaySeconds": 3600,
            "notification": {"playerName": "Player"},
            "executionName": "game-NA1_12345-abcdef12",
            "error": {"Cause": "boom"},
        },
        make_config(),
        dynamo,
        http_get=http,
    )

    assert len(dynamo.put_calls) == 1


def test_unknown_puuid_raises() -> None:
    http = FakeHttp(body=json.dumps(MATCH))

    with pytest.raises(RuntimeError, match="ghost-puuid"):
        collect_stats(
            {"matchId": "NA1_12345", "puuid": "ghost-puuid"},
            make_config(),
            FakeDynamo(),
            http_get=http,
        )


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"matchId": "NA1_12345"},
        {"puuid": "test-puuid"},
        {"matchId": "  ", "puuid": "test-puuid"},
    ],
)
def test_missing_event_fields_raise(event: dict) -> None:
    with pytest.raises(ValueError):
        collect_stats(event, make_config(), FakeDynamo(), http_get=FakeHttp())


@pytest.mark.parametrize("status", [404, 500])
def test_non_200_match_response_raises(status: int) -> None:
    http = FakeHttp(status=status, body="boom")

    with pytest.raises(RuntimeError, match=str(status)):
        collect_stats(
            {"matchId": "NA1_12345", "puuid": "test-puuid"},
            make_config(),
            FakeDynamo(),
            http_get=http,
        )


def test_match_403_mentions_rotating_the_secret() -> None:
    http = FakeHttp(status=403, body="forbidden")

    with pytest.raises(RuntimeError, match="rotate"):
        collect_stats(
            {"matchId": "NA1_12345", "puuid": "test-puuid"},
            make_config(),
            FakeDynamo(),
            http_get=http,
        )


def test_match_429_is_rate_limited_with_retry_after() -> None:
    http = FakeHttp(status=429, body="slow down", response_headers={"Retry-After": "5"})

    with pytest.raises(RateLimitedError, match="retry-after=5"):
        collect_stats(
            {"matchId": "NA1_12345", "puuid": "test-puuid"},
            make_config(),
            FakeDynamo(),
            http_get=http,
        )


def test_dynamo_write_failure_propagates() -> None:
    http = FakeHttp(body=json.dumps(MATCH))
    dynamo = FakeDynamo(error=RuntimeError("table is unavailable"))

    with pytest.raises(RuntimeError, match="table is unavailable"):
        collect_stats(
            {"matchId": "NA1_12345", "puuid": "test-puuid"},
            make_config(),
            dynamo,
            http_get=http,
        )
