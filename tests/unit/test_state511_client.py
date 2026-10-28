"""A 511 platform's event list, fetched through the cache (M20).

What is pinned is the part a keyed source gets wrong silently: the key stays out of the
cache key and out of every reason, a missing key replays before it refuses, and a refused
key is said to be refused rather than reported as a platform that is down.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest
import respx

from longrun.adapters.base import AdapterContext
from longrun.adapters.http import FeedRefused, get_json
from longrun.adapters.keys import ApiKey
from longrun.adapters.state511.client import EVENTS_PATH, events_args, fetch_events
from longrun.core.data.cache import SqliteCache, args_hash
from longrun.core.models.context import Budget

DAY = date(2026, 10, 28)
BASE = "https://511.example.invalid"
URL = BASE + EVENTS_PATH
KEY = ApiKey(
    env_var="LONGRUN_TEST511_API_KEY",
    service="a test 511 platform",
    register_at="https://511.example.invalid/developers/doc",
)
SECRET = "s3cret-value-never-printed"
EVENT = {"ID": 1, "EventType": "roadwork", "Latitude": 33.7, "Longitude": -84.4}


def _ctx(cache: Any, *, offline: bool = False) -> AdapterContext:
    return AdapterContext(cache=cache, budget=Budget(), offline=offline)


def test_a_missing_key_with_nothing_recorded_is_a_rung_nobody_climbed() -> None:
    with SqliteCache() as cache:
        ctx = _ctx(cache, offline=True)
        result = fetch_events(BASE, "state511.test", DAY, ctx, KEY)
    assert not isinstance(result, list)
    assert result.key_missing
    assert KEY.env_var in (result.reason or "") and KEY.register_at in (result.reason or "")
    assert ctx.budget.api_calls_used == 0


def test_a_missing_key_replays_what_somebody_with_one_recorded() -> None:
    with SqliteCache() as cache:
        cache.put(
            "adapter.state511.test",
            args_hash(events_args("state511.test", DAY)),
            DAY.isoformat(),
            [EVENT],
        )
        ctx = _ctx(cache, offline=True)
        result = fetch_events(BASE, "state511.test", DAY, ctx, KEY)
    assert result == [EVENT]
    assert ctx.budget.api_calls_used == 0


@respx.mock
def test_the_key_rides_on_the_request_and_never_in_the_cache_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(KEY.env_var, SECRET)
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=[EVENT]))
    with SqliteCache() as cache:
        ctx = _ctx(cache)
        first = fetch_events(BASE, "state511.test", DAY, ctx, KEY)
        again = fetch_events(BASE, "state511.test", DAY, ctx, KEY)
    assert first == again == [EVENT]
    assert route.call_count == 1, "the second ask is the cache's"
    assert ctx.budget.api_calls_used == 1
    sent = route.calls[0].request.url
    assert sent.params["key"] == SECRET and sent.params["format"] == "json"
    assert SECRET not in repr(events_args("state511.test", DAY))


def test_the_cache_key_is_the_adapter_and_the_day_and_nothing_else() -> None:
    assert args_hash(events_args("state511.ga", DAY)) == args_hash(
        {"adapter": "state511.ga", "day": "2026-10-28"}
    )


@respx.mock
def test_a_refused_key_is_said_to_be_refused_and_never_printed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What every IBI platform answered with no key on 2026-10-28: a 400 carrying XML,
    although `format=json` was asked."""
    monkeypatch.setenv(KEY.env_var, SECRET)
    respx.get(URL).mock(
        return_value=httpx.Response(400, text="<Error><Message>Invalid Key</Message></Error>")
    )
    with SqliteCache() as cache:
        result = fetch_events(BASE, "state511.test", DAY, _ctx(cache), KEY)
    assert not isinstance(result, list)
    assert result.reason == (
        f"511.example.invalid{EVENTS_PATH} refused LONGRUN_TEST511_API_KEY (HTTP 400)"
    )
    assert SECRET not in result.reason
    assert not result.key_missing, "a key was set and tried; this rung was climbed"


@respx.mock
def test_a_platform_that_is_down_is_a_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY.env_var, SECRET)
    respx.get(URL).mock(return_value=httpx.Response(503, text="no healthy upstream"))
    with SqliteCache() as cache:
        result = fetch_events(BASE, "state511.test", DAY, _ctx(cache), KEY)
    assert not isinstance(result, list)
    assert result.reason == f"FeedRefused: HTTP 503 from 511.example.invalid{EVENTS_PATH}"


@respx.mock
def test_an_answer_that_is_not_an_event_list_is_a_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY.env_var, SECRET)
    respx.get(URL).mock(return_value=httpx.Response(200, json={"Message": "throttled"}))
    with SqliteCache() as cache:
        result = fetch_events(BASE, "state511.test", DAY, _ctx(cache), KEY)
    assert not isinstance(result, list)
    assert result.reason == "the platform did not return an event list"


@respx.mock
def test_a_refusal_names_the_host_and_path_and_nothing_in_the_query() -> None:
    """httpx's own message is the whole URL. 511 SF Bay takes its key as `api_key=`, so a
    refused key put the key into the coverage reason - and onto the plan sheet."""
    respx.get("https://api.example.invalid/traffic/wzdx").mock(return_value=httpx.Response(401))
    with SqliteCache() as cache, pytest.raises(FeedRefused) as raised:
        get_json(
            "https://api.example.invalid/traffic/wzdx", _ctx(cache), params={"api_key": SECRET}
        )
    assert str(raised.value) == "HTTP 401 from api.example.invalid/traffic/wzdx"
    assert raised.value.status == 401
