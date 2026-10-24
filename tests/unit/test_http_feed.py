"""A feed read over HTTP: streamed, with a ceiling, on the plan's clock (M18, ADR 0056)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx
import pytest
import respx

from longrun.adapters.base import AdapterContext, describe
from longrun.adapters.http import FeedTooLarge, get_json
from longrun.core.data.cache import SqliteCache
from longrun.core.models.context import (
    DEADLINE_REASON,
    Budget,
    BudgetExceeded,
    DeadlineExceeded,
)

URL = "https://feed.example.invalid/wzdx"


def _ctx(cache: Any, budget: Budget | None = None) -> AdapterContext:
    return AdapterContext(cache=cache, budget=budget or Budget(), offline=False)


def _chunks(body: bytes, size: int = 1024) -> Iterator[bytes]:
    for start in range(0, len(body), size):
        yield body[start : start + size]


@respx.mock
def test_a_feed_is_read_whole_when_it_fits() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={"features": [1, 2]}))
    with SqliteCache() as cache:
        assert get_json(URL, _ctx(cache)) == {"features": [1, 2]}


@respx.mock
def test_a_feed_that_declares_too_much_is_refused_before_it_is_read() -> None:
    body = json.dumps({"features": ["x" * 4000]}).encode()
    respx.get(URL).mock(return_value=httpx.Response(200, content=body))
    with SqliteCache() as cache, pytest.raises(FeedTooLarge, match="declares"):
        get_json(URL, _ctx(cache), max_bytes=1000)


@respx.mock
def test_a_feed_that_runs_past_its_ceiling_is_refused_not_truncated() -> None:
    """No `Content-Length` (chunked), so the bytes are counted as they arrive. A feed cut off
    at the ceiling would read exactly like a state with fewer work zones."""
    body = json.dumps({"features": ["x" * 4000]}).encode()
    respx.get(URL).mock(return_value=httpx.Response(200, content=_chunks(body)))
    with SqliteCache() as cache, pytest.raises(FeedTooLarge, match="ran past"):
        get_json(URL, _ctx(cache), max_bytes=1000)


@respx.mock
def test_a_feed_that_outlasts_the_plan_is_cut_off_by_its_clock() -> None:
    body = json.dumps({"features": []}).encode()
    respx.get(URL).mock(return_value=httpx.Response(200, content=_chunks(body, 4)))
    spent = Budget(latency_budget_s=0.0)
    with SqliteCache() as cache, pytest.raises(DeadlineExceeded):
        get_json(URL, _ctx(cache, spent))


def test_a_deadline_is_said_as_a_deadline_and_still_is_a_budget() -> None:
    """It read "API call budget exhausted", which sent a reader looking for a call count
    nowhere near its limit."""
    exc = DeadlineExceeded(f"{DEADLINE_REASON} (180 s); 181 s elapsed")
    assert isinstance(exc, BudgetExceeded)
    assert describe(exc).startswith(DEADLINE_REASON)
    assert describe(BudgetExceeded("calls")) == "API call budget exhausted"


@respx.mock
def test_a_wzdx_feed_whose_records_all_fail_to_parse_is_a_reason_not_an_empty_answer() -> None:
    """ADR 0039's hole: a CWZ feed read as WZDx carried every work zone in the state and
    answered "checked, nothing here"."""
    from longrun.adapters.wzdx.client import fetch_feed

    cwz = {
        "feed_info": {"publisher": "somebody", "version": "1.0"},
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {"core_details": {}}, "geometry": None}],
    }
    respx.get(URL).mock(return_value=httpx.Response(200, json=cwz))
    with SqliteCache() as cache:
        result = fetch_feed(URL, "wzdx.example", date(2026, 9, 15), _ctx(cache))
    assert not result.answered
    assert result.reason == "the feed carried 1 record(s) and none read as WZDx"
