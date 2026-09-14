"""Turning a place name into a point (scope 8.1 step 1; ADR 0018).

Nothing here reaches Nominatim. What is tested is the four things a cached external client
in this codebase has to get right, because each fails silently when it is wrong: the key
is stable, the contact string gates the call, a replay costs nothing, and a failure is a
reason rather than an exception.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from longrun.core.data.cache import STATIC_DAY, SqliteCache, args_hash
from longrun.core.data.geocode import (
    SOURCE,
    USER_AGENT_ENV_VAR,
    geocode,
    geocode_args,
    parse_places,
    user_agent,
)
from longrun.core.export.attribution import licence_for, unattributed
from longrun.core.models.context import Budget, FrozenClock, ScorerContext
from longrun.core.models.coverage import CoverageManifest
from longrun.core.preferences.store import load_defaults

START = datetime(2026, 3, 15, 7, 30)

PAYLOAD = [
    {
        "display_name": "Ferry Building, San Francisco",
        "lat": "37.7955",
        "lon": "-122.3937",
        "type": "attraction",
    },
    {
        "display_name": "Ferry Building Marketplace",
        "lat": "37.7956",
        "lon": "-122.3936",
        "type": "shop",
    },
]


def _ctx(tmp_path: Path, offline: bool = False) -> ScorerContext:
    from longrun.core.data.file_store import FileLayerStore, FileRasterStore

    return ScorerContext(
        layers=FileLayerStore(tmp_path),
        rasters=FileRasterStore(tmp_path),
        cache=SqliteCache(tmp_path / "cache.sqlite", offline=offline),
        clock=FrozenClock(START),
        coverage=CoverageManifest(),
        profile=load_defaults(),
        budget=Budget(),
    )


# --- the contact string ------------------------------------------------------


def test_without_a_contact_string_nothing_is_called_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nominatim's policy asks for one, and it is never invented or borrowed - the rule
    NWS gets. The reason names the variable, because "geocoding unavailable" tells a
    reader nothing they can act on."""
    monkeypatch.delenv(USER_AGENT_ENV_VAR, raising=False)
    ctx = _ctx(tmp_path)

    found = geocode("Ferry Building", ctx)

    assert not found.answered
    assert USER_AGENT_ENV_VAR in (found.reason or "")
    assert ctx.budget.api_calls_used == 0, "and no budget was spent deciding not to call"


def test_a_blank_contact_string_counts_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(USER_AGENT_ENV_VAR, "   ")
    assert user_agent() is None


# --- the key -----------------------------------------------------------------


def test_the_same_question_asked_differently_is_one_key() -> None:
    """Case and surrounding space are not part of the question."""
    assert args_hash(geocode_args("Ferry Building")) == args_hash(geocode_args("  ferry building "))


def test_a_viewbox_is_rounded_before_it_reaches_the_key() -> None:
    """`args_hash` does no rounding of its own; every key builder does it, or a box
    recomputed from a route's bbox misses its own recording."""
    a = geocode_args("x", viewbox=(-122.51234567, 37.70123456, -122.35123456, 37.83123456))
    b = geocode_args("x", viewbox=(-122.51234999, 37.70123999, -122.35123999, 37.83123999))
    assert args_hash(a) == args_hash(b)


def test_a_place_name_is_not_keyed_by_the_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`STATIC_DAY`, the convention `forecast.py` set for a date-free lookup. Keying a
    place name by the plan date would refetch it daily for nothing."""
    monkeypatch.setenv(USER_AGENT_ENV_VAR, "test (nobody@example.com)")
    ctx = _ctx(tmp_path)
    ctx.cache.put(
        "nominatim.search", args_hash(geocode_args("Ferry Building")), STATIC_DAY, PAYLOAD
    )

    found = geocode("Ferry Building", ctx)

    assert found.answered
    assert [(p.lat, p.lon) for p in found.places][0] == (37.7955, -122.3937)


# --- replay and budget -------------------------------------------------------


def test_a_recorded_answer_costs_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spent inside the producer, so a cassette replay is not an external call."""
    monkeypatch.setenv(USER_AGENT_ENV_VAR, "test (nobody@example.com)")
    ctx = _ctx(tmp_path)
    ctx.cache.put(
        "nominatim.search", args_hash(geocode_args("Ferry Building")), STATIC_DAY, PAYLOAD
    )

    geocode("Ferry Building", ctx)

    assert ctx.budget.api_calls_used == 0


def test_an_unrecorded_lookup_offline_is_a_reason_and_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(USER_AGENT_ENV_VAR, "test (nobody@example.com)")
    ctx = _ctx(tmp_path, offline=True)

    found = geocode("somewhere unrecorded", ctx)

    assert not found.answered
    assert found.reason == "not in the cassette"


# --- what it says it did -----------------------------------------------------


def test_a_row_that_cannot_be_read_is_dropped_rather_than_guessed() -> None:
    places = parse_places([{"display_name": "ok", "lat": "1.0", "lon": "2.0"}, {"lat": "nope"}, 7])
    assert [p.name for p in places] == ["ok"]


def test_both_answers_resolve_to_a_licence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug `forecast.py` has: its *unanswered* entry uses a source name with no licence
    row, so a plan on which every site failed would print `LICENCE NOT RECORDED` and fail
    the golden that forbids it. Checked here for both branches, not just the happy one.
    """
    monkeypatch.delenv(USER_AGENT_ENV_VAR, raising=False)
    unanswered = geocode("Ferry Building", _ctx(tmp_path)).coverage()

    monkeypatch.setenv(USER_AGENT_ENV_VAR, "test (nobody@example.com)")
    ctx = _ctx(tmp_path)
    ctx.cache.put(
        "nominatim.search", args_hash(geocode_args("Ferry Building")), STATIC_DAY, PAYLOAD
    )
    answered = geocode("Ferry Building", ctx).coverage()

    for entries in (answered, unanswered):
        assert [e.source for e in entries] == [SOURCE]
        assert licence_for(entries[0].source) is not None
        assert unattributed([e.source for e in entries]) == []


def test_the_forecast_sources_own_failure_entry_now_has_a_licence() -> None:
    """Found while checking what a new source owes, and fixed here: `forecast.py` records
    unanswered sites as `source="forecast"`, which resolved to nothing."""
    assert licence_for("forecast") is not None


def test_the_geocoder_is_share_alike_because_it_serves_osm() -> None:
    """ODbL flows through Nominatim, so the sheet's share-alike trailer applies."""
    licence = licence_for(SOURCE)
    assert licence is not None
    assert licence.share_alike is True
