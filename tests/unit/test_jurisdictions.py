"""Resolving who owns the ground under a route (scope 7.10, 13 step 4).

Hand-built rectangles with known answers, no store and no database - which is the point of
`jurisdictions_from_frames` being a pure function of two frames. The cases worth writing are
the ones where a plausible shortcut gives a wrong answer quietly: a GEOID prefix that looks
like containment, a PAD-US code that names a class of manager rather than a manager, a park
on a state line, and a fixture frozen before a column existed.
"""

from __future__ import annotations

from typing import Any

import pytest

from longrun.core.data.jurisdictions import (
    JurisdictionScan,
    from_padus_row,
    from_tiger_row,
    jurisdictions_from_frames,
    route_jurisdictions,
)
from longrun.core.models.geometry import Route, RoutePoint


def _frame(rows: list[dict[str, Any]]) -> Any:
    """A GeoDataFrame with unit-square geometry - nothing here tests geometry, because the
    spatial join is the store's job and is covered against a real database in
    `tests/contract/test_national_load.py`."""
    import geopandas as gpd
    from shapely.geometry import box

    if not rows:
        return gpd.GeoDataFrame({}, geometry=[], crs="EPSG:4326")
    return gpd.GeoDataFrame(rows, geometry=[box(0, 0, 1, 1) for _ in rows], crs="EPSG:4326")


def _boundary(level: str, geoid: str, name: str, statefp: str | None = None) -> dict[str, Any]:
    return {"level": level, "geoid": geoid, "name": name, "statefp": statefp}


# --- TIGER rows -------------------------------------------------------------


def test_a_place_is_told_which_state_it_is_in() -> None:
    """`statefp` is a column on every row, so this is a read. It matters because it is what
    lets a state DOT's adapter answer for a city without anyone doing GEOID arithmetic."""
    kc = from_tiger_row("place", "2938000", "Kansas City", "29")
    assert kc.id == "tiger:place:2938000"
    assert kc.within == ("tiger:state:29",)
    assert "tiger:state:29" in kc.ids


def test_a_state_is_not_inside_itself() -> None:
    missouri = from_tiger_row("state", "29", "Missouri", "29")
    assert missouri.within == ()
    assert missouri.ids == ("tiger:state:29",)


def test_a_place_with_no_statefp_stays_unqualified_rather_than_guessing() -> None:
    """Slicing the state off the front of a place GEOID would work most of the time, and
    "most of the time" is what makes a silent wrong answer rather than a loud one."""
    assert from_tiger_row("place", "2938000", "Kansas City").within == ()


def test_a_county_adapter_does_not_reach_a_place_whose_geoid_extends_it() -> None:
    """The prefix trap, end to end. County `29095` is a lexical prefix of place `2909512`."""
    found = jurisdictions_from_frames(
        _frame(
            [
                _boundary("county", "29095", "Jackson County", "29"),
                _boundary("place", "2909512", "Lookalike", "29"),
            ]
        )
    )
    lookalike = next(j for j in found if j.name == "Lookalike")
    assert "tiger:county:29095" not in lookalike.ids


# --- the state line ---------------------------------------------------------


def test_a_state_line_route_resolves_both_states() -> None:
    """Scope 11 region 4's whole purpose: two DOTs, two adapter sets, one route."""
    found = jurisdictions_from_frames(
        _frame(
            [
                _boundary("state", "29", "Missouri", "29"),
                _boundary("state", "20", "Kansas", "20"),
                _boundary("county", "29095", "Jackson County", "29"),
                _boundary("county", "20209", "Wyandotte County", "20"),
            ]
        )
    )
    assert {j.id for j in found if j.level == "state"} == {"tiger:state:29", "tiger:state:20"}
    jackson = next(j for j in found if j.name == "Jackson County")
    wyandotte = next(j for j in found if j.name == "Wyandotte County")
    assert "tiger:state:29" in jackson.ids and "tiger:state:20" not in jackson.ids
    assert "tiger:state:20" in wyandotte.ids


def test_two_cities_of_the_same_name_stay_distinct() -> None:
    """There is a Kansas City in each state and they share a border."""
    found = jurisdictions_from_frames(
        _frame(
            [
                _boundary("place", "2938000", "Kansas City", "29"),
                _boundary("place", "2036000", "Kansas City", "20"),
            ]
        )
    )
    assert len({j.id for j in found}) == 2


# --- PAD-US rows ------------------------------------------------------------


def test_a_federal_agency_resolves_without_a_state() -> None:
    park = from_padus_row("NPS", "Yosemite", "FED", "06")
    assert park is not None and park.id == "padus:NPS"
    assert park.level == "park" and park.agency == "NPS"


def test_a_city_managed_park_carries_its_state() -> None:
    """`CITY` names a class of manager. Without the state this one id would claim every
    municipal park in America."""
    park = from_padus_row("CITY", "Golden Gate Park", "LOC", "06")
    assert park is not None and park.id == "padus:CITY:06"


def test_a_park_managed_by_nobody_is_not_a_jurisdiction() -> None:
    """PAD-US spells "unknown" several ways. Carrying them would put `padus:UNK` in every
    coverage manifest in the country, next to a reason nobody can act on."""
    for code in ("UNK", "UNKL", "unknown", "", None):
        assert from_padus_row(code, "Somewhere") is None, code


def test_parks_take_the_state_of_the_route_when_there_is_only_one() -> None:
    found = jurisdictions_from_frames(
        _frame([_boundary("state", "06", "California", "06")]),
        _frame([{"agency": "CITY", "name": "Golden Gate Park", "agency_type": "LOC"}]),
    )
    assert any(j.id == "padus:CITY:06" for j in found)


def test_a_park_on_a_two_state_route_stays_unqualified_rather_than_picking_one() -> None:
    """The honest degradation. With Missouri and Kansas both in play, a `CITY` park whose
    own state is unknown cannot be assigned to either, and guessing would file a Kansas
    park under a Missouri adapter."""
    found = jurisdictions_from_frames(
        _frame(
            [
                _boundary("state", "29", "Missouri", "29"),
                _boundary("state", "20", "Kansas", "20"),
            ]
        ),
        _frame([{"agency": "CITY", "name": "Riverfront Park"}]),
    )
    assert any(j.id == "padus:CITY" for j in found)
    assert not any(j.id in ("padus:CITY:29", "padus:CITY:20") for j in found)


def test_a_fixture_frozen_before_the_agency_column_existed_reads_as_unknown() -> None:
    """`synthetic-hazards/fixtures/parks.geojson` has `unit_id` and `name` and nothing else,
    because it was hand-written before PAD-US loaded. It must not crash the scorer."""
    found = jurisdictions_from_frames(None, _frame([{"unit_id": 501, "name": "Synthetic Commons"}]))
    assert found == []


def test_one_agency_managing_many_parks_is_one_jurisdiction() -> None:
    """PAD-US explodes aggregates into thousands of parts, and a corridor meeting forty NPS
    polygons is still one body to ask. Deduplication here is what keeps the fan-out from
    becoming forty fetches."""
    found = jurisdictions_from_frames(
        None,
        _frame([{"agency": "NPS", "name": f"Unit {i}", "agency_type": "FED"} for i in range(40)]),
    )
    assert [j.id for j in found] == ["padus:NPS"]


# --- absence ----------------------------------------------------------------


def test_no_frames_at_all_is_no_jurisdictions_rather_than_an_error() -> None:
    assert jurisdictions_from_frames(None, None) == []


def test_empty_frames_resolve_to_nothing() -> None:
    assert jurisdictions_from_frames(_frame([]), _frame([])) == []


def test_a_row_missing_its_level_or_geoid_is_skipped_not_guessed() -> None:
    found = jurisdictions_from_frames(
        _frame(
            [
                {"level": "county", "geoid": None, "name": "Nameless", "statefp": "29"},
                {
                    "level": "borough",
                    "geoid": "12345",
                    "name": "Not a TIGER level",
                    "statefp": "02",
                },
                _boundary("county", "29095", "Jackson County", "29"),
            ]
        )
    )
    assert [j.id for j in found] == ["tiger:county:29095"]


def test_a_scan_that_could_ask_nobody_is_not_a_scan_that_found_nobody() -> None:
    """The distinction the whole coverage manifest rests on, at this level."""
    assert not JurisdictionScan().answered
    assert JurisdictionScan(boundaries_checked=True).answered


# --- through a store --------------------------------------------------------


def _route() -> Route:
    return Route(
        id="r",
        points=[
            RoutePoint(lat=37.79, lon=-122.40, cum_dist_m=0.0),
            RoutePoint(lat=37.80, lon=-122.40, cum_dist_m=1112.0),
        ],
    )


def test_a_fixture_with_no_boundaries_layer_produces_a_reason_not_an_exception(
    tmp_path: Any,
) -> None:
    """Every golden route committed before M4 is in exactly this state, so this is the
    ordinary path rather than an edge case."""
    from datetime import datetime

    from longrun.core.data.cache import SqliteCache
    from longrun.core.data.file_store import FileLayerStore, FileRasterStore
    from longrun.core.models.context import FrozenClock, ScorerContext
    from longrun.core.models.coverage import CoverageManifest
    from longrun.core.models.profile import PreferenceProfile

    with SqliteCache() as cache:
        ctx = ScorerContext(
            layers=FileLayerStore(tmp_path),
            rasters=FileRasterStore(tmp_path),
            cache=cache,
            clock=FrozenClock(datetime(2026, 9, 15, 7, 0)),
            coverage=CoverageManifest(),
            profile=PreferenceProfile(),
        )
        scan = route_jurisdictions(_route(), ctx)

    assert scan.jurisdictions == []
    assert not scan.answered
    assert scan.reasons, "an absent layer has to say so"
    # M2 shipped a coverage reason carrying a Windows drive letter into a golden
    # expectation, which passed locally and failed on CI. `LayerNotFound` interpolates an
    # absolute path and must never be formatted into a reason verbatim.
    for reason in scan.reasons:
        assert "\\" not in reason and "/" not in reason, reason
        assert not any(f"{d}:" in reason for d in "CDEF"), reason


@pytest.mark.parametrize("reason", ["no boundaries layer", "no parks layer"])
def test_both_layers_are_reported_separately(reason: str, tmp_path: Any) -> None:
    """Boundaries and parks answer different questions - who governs, and who manages - so
    a route that resolved one and not the other must not read as having resolved neither."""
    from datetime import datetime

    from longrun.core.data.cache import SqliteCache
    from longrun.core.data.file_store import FileLayerStore, FileRasterStore
    from longrun.core.models.context import FrozenClock, ScorerContext
    from longrun.core.models.coverage import CoverageManifest
    from longrun.core.models.profile import PreferenceProfile

    with SqliteCache() as cache:
        ctx = ScorerContext(
            layers=FileLayerStore(tmp_path),
            rasters=FileRasterStore(tmp_path),
            cache=cache,
            clock=FrozenClock(datetime(2026, 9, 15, 7, 0)),
            coverage=CoverageManifest(),
            profile=PreferenceProfile(),
        )
        scan = route_jurisdictions(_route(), ctx)
    assert any(r.startswith(reason) for r in scan.reasons), scan.reasons
