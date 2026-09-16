"""A name the runner wants to stay off becomes an area the router will not enter (scope 6.4).

`PlanRequest.avoid_names` was written by `agent/intent.py` and read by nothing, so "avoid
El Camino" reached the planner, was stored on the request, and changed no route. These are
the tests for the half that was missing, and for the two ways it must decline: no contact
string for the geocoder, and an extent too large to close.

No network: `geocode` is served from the same cache every other external read uses, so a
recorded answer is an ordinary `cache.put`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from longrun.core.data.cache import STATIC_DAY, SqliteCache, args_hash
from longrun.core.data.geocode import (
    SOURCE,
    USER_AGENT_ENV_VAR,
    Place,
    geocode_args,
    parse_places,
)
from longrun.core.models.context import Budget, FrozenClock, ScorerContext
from longrun.core.models.coverage import CoverageManifest
from longrun.core.preferences.store import load_defaults
from longrun.core.routing.avoid import MAX_AREA_KM2, area_for, polygons_for_names

START = datetime(2026, 3, 15, 7, 30)

#: A place name is not date-dependent, so `geocode` keys its day column on `STATIC_DAY` -
#: recording under the plan's date misses, which is how the first draft of these tests
#: failed.
DAY = STATIC_DAY


def _ctx(tmp_path: Path, cache: SqliteCache) -> ScorerContext:
    from longrun.core.data.file_store import FileLayerStore, FileRasterStore

    return ScorerContext(
        layers=FileLayerStore(tmp_path),
        rasters=FileRasterStore(tmp_path),
        cache=cache,
        clock=FrozenClock(START),
        coverage=CoverageManifest(),
        profile=load_defaults(),
        budget=Budget(),
    )


def _record(cache: SqliteCache, query: str, rows: list[dict[str, Any]]) -> None:
    cache.put("nominatim.search", args_hash(geocode_args(query)), DAY, rows)


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "display_name": "El Camino Real",
        "lat": "37.4200",
        "lon": "-122.1400",
        "type": "primary",
        "boundingbox": ["37.4190", "37.4260", "-122.1450", "-122.1380"],
    }
    row.update(overrides)
    return row


# --- the bounding box, which is the point ------------------------------------


def test_a_bounding_box_is_kept_because_a_road_is_a_line() -> None:
    """A geocoder's lat/lon is one arbitrary spot on a road. Avoiding a disc around it
    would avoid two hundred metres of a road somebody asked to stay off entirely."""
    place = parse_places([_row()])[0]

    assert place.bbox == (37.4190, 37.4260, -122.1450, -122.1380)


def test_a_box_of_the_wrong_shape_is_dropped_rather_than_guessed_at() -> None:
    assert parse_places([_row(boundingbox=["37.4", "37.5"])])[0].bbox is None
    assert parse_places([_row(boundingbox="nonsense")])[0].bbox is None


def test_an_area_covers_the_extent_it_was_given() -> None:
    from shapely.geometry import shape

    area = area_for(parse_places([_row()])[0])

    assert area is not None
    bounds = shape(area["geometry"]).bounds
    assert bounds[0] <= -122.1450 + 1e-4 and bounds[2] >= -122.1380 - 1e-4
    assert bounds[1] <= 37.4190 + 1e-4 and bounds[3] >= 37.4260 - 1e-4


def test_a_place_with_no_box_gets_a_disc_and_not_nothing() -> None:
    place = Place(name="a corner", lat=37.42, lon=-122.14)

    area = area_for(place)

    assert area is not None
    assert area["geometry"]["type"] == "Polygon"


def test_an_extent_too_large_to_close_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A box around a long expressway is tens of square kilometres, and closing that much
    takes the parallel streets with it - which are the streets a detour was going to use."""
    monkeypatch.setenv(USER_AGENT_ENV_VAR, "longrun test (nobody@example.invalid)")
    huge = Place(
        name="a whole expressway", lat=37.42, lon=-122.14, bbox=(37.0, 37.6, -122.5, -122.0)
    )

    assert area_for(huge) is None

    with SqliteCache() as cache:
        _record(
            cache,
            "a whole expressway",
            [_row(display_name="x", boundingbox=["37.0", "37.6", "-122.5", "-122.0"])],
        )
        ctx = _ctx(tmp_path, cache)
        areas, notes = polygons_for_names(["a whole expressway"], ctx)

    assert areas == []
    assert notes and f"{MAX_AREA_KM2:g} km2" in notes[0]


# --- what happens when it cannot answer --------------------------------------


def test_without_a_contact_string_nothing_is_fetched_and_the_variable_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The NWS rule, which `geocode` already keeps: no contact string, no call. The avoid
    is then reported as not honoured rather than silently dropped."""
    monkeypatch.delenv(USER_AGENT_ENV_VAR, raising=False)

    with SqliteCache(offline=True) as cache:
        ctx = _ctx(tmp_path, cache)
        areas, notes = polygons_for_names(["El Camino Real"], ctx)

    assert areas == []
    assert notes and USER_AGENT_ENV_VAR in notes[0]
    assert any(entry.source == SOURCE and not entry.checked for entry in ctx.coverage.entries)


def test_a_name_with_no_match_is_said_to_be_unhonoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(USER_AGENT_ENV_VAR, "longrun test (nobody@example.invalid)")
    with SqliteCache() as cache:
        _record(cache, "nowhere at all", [])
        ctx = _ctx(tmp_path, cache)
        areas, notes = polygons_for_names(["nowhere at all"], ctx)

    assert areas == []
    assert notes and "not honoured" in notes[0]


def test_more_names_than_the_lookup_cap_says_how_many_went_unasked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from longrun.core.data.geocode import MAX_LOOKUPS_PER_PLAN

    monkeypatch.setenv(USER_AGENT_ENV_VAR, "longrun test (nobody@example.invalid)")

    names = [f"place {i}" for i in range(MAX_LOOKUPS_PER_PLAN + 3)]
    with SqliteCache() as cache:
        for name in names:
            _record(cache, name, [_row()])
        ctx = _ctx(tmp_path, cache)
        areas, notes = polygons_for_names(names, ctx)

    assert len(areas) == MAX_LOOKUPS_PER_PLAN
    assert any("not looked up" in note for note in notes)


#: The whole way through the loop is `test_loop.py::
#: test_an_avoided_name_reaches_the_router_as_an_area`, where the loop's own fixtures live.
