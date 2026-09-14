"""`crew_points` and `start_time_optimizer` (scope 7.7).

The two scorers that do not describe a segment. `crew_points` describes places beside the
route and `start_time_optimizer` describes the whole route at a dozen different hours, so
both put something other than an `s00007` in `segment_id` — following the precedent
`ROUTE_SUMMARY_ID` set, and with the same obligation: **the ids have to be unique and
stable**, because a duplicate is a measurement silently overwritten in the sheet and in
the golden content hash.

The sweep's cost is the other thing worth pinning. Risk R5 named this scorer as the one
that could break the ~3-minute budget, and the reason it does not is ADR 0003's choice of
a full horizon profile over a single ray: the skyline does not change when you leave an
hour later. `test_the_sweep_builds_the_horizons_once` is what holds that.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import LineString, Point

from longrun.core.data.cache import SqliteCache
from longrun.core.data.file_store import LayerNotFound
from longrun.core.models.context import Budget, FrozenClock, ScorerContext
from longrun.core.models.coverage import CoverageManifest
from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.models.profile import PreferenceProfile
from longrun.core.scorers import crew_points as crew
from longrun.core.scorers import start_time_optimizer as sweep
from longrun.core.scorers._common import ROUTE_SUMMARY_ID

LAT, LON, STEP = 37.7955, -122.4000, 0.0005
START = datetime(2026, 9, 12, 12, 0)


def _route(points: int = 21) -> Route:
    return Route(
        id="r",
        points=[
            RoutePoint(lat=LAT, lon=LON + i * STEP, cum_dist_m=i * 44.0, ele_m=10.0)
            for i in range(points)
        ],
    )


def _segments(route: Route) -> list[Any]:
    from longrun.core.geo.segments import segment_route

    return segment_route(route, way_ids=[1] * len(route.points))


def _etas(route: Route) -> list[datetime]:
    return [START + timedelta(seconds=i * 30) for i in range(len(route.points))]


class _Layers:
    def __init__(self, **frames: Any) -> None:
        self._frames = frames

    def _get(self, layer: str) -> Any:
        if layer not in self._frames:
            raise LayerNotFound(f"no {layer!r} layer")
        return self._frames[layer]

    def ways_in_corridor(self, corridor: Any) -> Any:
        return self._get("ways")

    def points_in_corridor(self, corridor: Any, kinds: list[str], layer: str = "amenities") -> Any:
        return self._get(layer)

    def polygons_intersecting(self, corridor: Any, layer: str) -> Any:
        return self._get(layer)

    def lines_crossing(self, route: Any, layer: str) -> Any:
        return self._get(layer)

    def has_layer(self, layer: str) -> bool:
        return layer in self._frames

    def vintage(self, layer: str) -> str | None:
        return None


class _Rasters:
    """A store with nothing in it, satisfying the whole `RasterStore` protocol.

    `read_window_meta` is separate from `read_window` because "no canopy layer", "the
    canopy read failed" and "canopy has no coverage here" are three different scope 3.6
    claims, and a stub that answers only `read_window` collapses them back into an
    AttributeError halfway down the DSM builder.
    """

    def has_layer(self, name: str) -> bool:
        return False

    def read_window(self, *args: Any, **kwargs: Any) -> None:
        return None

    def read_window_meta(self, *args: Any, **kwargs: Any) -> None:
        return None


def _ctx(layers: Any) -> ScorerContext:
    return ScorerContext(
        layers=layers,
        rasters=_Rasters(),
        cache=SqliteCache(offline=True),
        clock=FrozenClock(START),
        coverage=CoverageManifest(),
        profile=PreferenceProfile(),
        budget=Budget(),
        utc_offset_hours=-7.0,
    )


def _frame(records: list[dict], geometries: list[Any]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(records, geometry=geometries, crs="EPSG:4326")


def _road() -> gpd.GeoDataFrame:
    return _frame(
        [{"way_id": 1, "highway": "residential"}],
        [LineString([(LON, LAT + 0.0003), (LON + 0.02, LAT + 0.0003)])],
    )


def _run_crew(layers: Any) -> Any:
    route = _route()
    return crew.crew_points(route, _segments(route), _ctx(layers), _etas(route))


def _summary(result: Any) -> dict[str, Any]:
    return next(m for m in result.measurements if m.segment_id == ROUTE_SUMMARY_ID).values


# --- crew points ------------------------------------------------------------


def test_crew_points_is_unavailable_with_no_amenities_layer() -> None:
    result = _run_crew(_Layers())
    assert not result.coverage[0].checked


def test_a_car_park_beside_a_road_is_a_meet_point() -> None:
    parking = _frame(
        [{"kind": "parking", "name": "Trailhead"}], [Point(LON + 5 * STEP, LAT + 0.0002)]
    )
    result = _run_crew(_Layers(amenities=parking, ways=_road()))
    assert _summary(result)["meet_points"] == 1
    row = next(m for m in result.measurements if m.segment_id.startswith(crew.MEET_PREFIX))
    assert row.values["name"] == "Trailhead"
    assert row.values["runner_eta"] is not None


def test_a_car_park_with_no_road_near_it_is_not_a_meet_point() -> None:
    """OSM has plenty of mapped parking an ordinary car cannot reach, and putting a crew on
    one is worse than telling them there is nowhere to wait."""
    parking = _frame([{"kind": "parking"}], [Point(LON + 5 * STEP, LAT + 0.0002)])
    far_road = _frame(
        [{"way_id": 1, "highway": "residential"}],
        [LineString([(LON, LAT + 0.02), (LON + 0.02, LAT + 0.02)])],
    )
    assert _summary(_run_crew(_Layers(amenities=parking, ways=far_road)))["meet_points"] == 0


def test_a_private_car_park_is_not_a_meet_point() -> None:
    assert not crew.is_parking({"kind": "parking", "access": "private"})
    assert not crew.is_parking({"kind": "parking", "access": "customers"})
    assert crew.is_parking({"kind": "parking"})


def test_meet_point_ids_are_unique_when_several_project_to_the_same_distance() -> None:
    """The bug the first real corridor produced: three car parks beside a city start all
    project to 0.0 km along the route, and an id built from the distance was `meet@0.0km`
    three times - two measurements silently lost."""
    parking = _frame(
        [{"kind": "parking", "name": f"P{i}"} for i in range(3)],
        [Point(LON - 0.0002, LAT + 0.0001 * i) for i in range(3)],
    )
    result = _run_crew(_Layers(amenities=parking, ways=_road()))
    ids = [m.segment_id for m in result.measurements]
    assert len(ids) == len(set(ids)), ids


def test_meet_point_order_is_stable_across_runs() -> None:
    """A golden diff is only reviewable if the ids do not shuffle between runs."""
    parking = _frame(
        [{"kind": "parking", "name": f"P{i}"} for i in range(3)],
        [Point(LON - 0.0002, LAT + 0.0001 * i) for i in range(3)],
    )
    first = [m.segment_id for m in _run_crew(_Layers(amenities=parking, ways=_road())).measurements]
    second = [
        m.segment_id for m in _run_crew(_Layers(amenities=parking, ways=_road())).measurements
    ]
    assert first == second


def test_drive_time_is_reported_as_unknown_on_every_run() -> None:
    """Scope 7.7 asks for drive time and repair mode has no router. A crew table with a
    made-up drive time is worse than one that says the drive was not computed."""
    parking = _frame([{"kind": "parking"}], [Point(LON + 5 * STEP, LAT + 0.0002)])
    result = _run_crew(_Layers(amenities=parking, ways=_road()))
    entry = next(c for c in result.coverage if c.source == "router")
    assert not entry.checked
    assert "no router" in (entry.reason or "")


def test_road_access_unchecked_lowers_confidence_rather_than_dropping_the_point() -> None:
    parking = _frame([{"kind": "parking"}], [Point(LON + 5 * STEP, LAT + 0.0002)])
    result = _run_crew(_Layers(amenities=parking))
    rows = [m for m in result.measurements if m.segment_id.startswith(crew.MEET_PREFIX)]
    assert rows and all(m.confidence == crew.NO_ROADS_CONFIDENCE for m in rows)
    assert _summary(result)["road_access_checked"] is False


def test_a_route_with_no_parking_is_not_flagged() -> None:
    """Most runs are unsupported. Flagging every one of them for having no crew parking
    would put a soft finding on the whole system."""
    result = _run_crew(_Layers(amenities=_frame([], []), ways=_road()))
    assert not result.flags


# --- the start-time sweep ---------------------------------------------------


def test_the_window_stays_on_the_calendar_day() -> None:
    """A forecast is fetched per day; a candidate that crossed midnight would compare one
    day's weather against another's without saying so."""
    late = datetime(2026, 9, 12, 23, 0)
    starts = sweep.candidate_starts(late)
    assert all(s.date() == late.date() for s in starts)
    assert late in starts


def test_the_window_is_symmetric_around_the_request_when_the_day_allows() -> None:
    starts = sweep.candidate_starts(datetime(2026, 9, 12, 12, 0))
    assert len(starts) == int(2 * sweep.WINDOW_HOURS * 60 / sweep.STEP_MINUTES) + 1
    assert starts[0].hour == 9
    assert starts[-1].hour == 15


def test_a_zero_window_is_the_requested_start_alone() -> None:
    assert sweep.candidate_starts(START, window_hours=0.0) == [START]


def test_the_sweep_is_unavailable_without_a_surface_model() -> None:
    route = _route()
    result = sweep.start_time_optimizer(route, _segments(route), _ctx(_Layers()), _etas(route))
    assert not result.coverage[0].checked
    assert "surface model" in (result.coverage[0].reason or "")


def test_the_sweep_is_unavailable_without_etas() -> None:
    route = _route()
    result = sweep.start_time_optimizer(route, _segments(route), _ctx(_Layers()), None)
    assert not result.coverage[0].checked
    assert "pacing model" in (result.coverage[0].reason or "")


def test_the_sweep_builds_the_horizons_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The property that makes thirteen candidates cost what one does, and the one risk R5
    named this scorer for. ADR 0003 chose a horizon *profile* over a single ray partly
    because it is reusable across start times; if a refactor moved the call inside the
    loop, the sweep would cost thirteen ray-casts and nothing else would look different.
    """
    calls = {"n": 0}

    class _Horizons:
        horizons = np.zeros((21, 8))
        svf = np.ones(21)

        class coverage:  # noqa: N801 - a stand-in for the real manifest
            @staticmethod
            def answered() -> bool:  # pragma: no cover - replaced below
                return True

            @staticmethod
            def entries() -> list[Any]:
                return []

    horizons = _Horizons()
    horizons.coverage.answered = True  # type: ignore[attr-defined]

    def fake(route: Any, ctx: Any) -> Any:
        calls["n"] += 1
        return horizons

    monkeypatch.setattr(sweep, "corridor_horizons", fake)
    route = _route()
    result = sweep.start_time_optimizer(route, _segments(route), _ctx(_Layers()), _etas(route))

    assert calls["n"] == 1, f"the sweep built the horizons {calls['n']} times"
    rows = [m for m in result.measurements if m.segment_id.startswith(sweep.CANDIDATE_PREFIX)]
    assert len(rows) == len(sweep.candidate_starts(START))


def test_every_candidate_gets_its_own_row_and_the_ids_are_unique(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_horizons(monkeypatch)
    route = _route()
    result = sweep.start_time_optimizer(route, _segments(route), _ctx(_Layers()), _etas(route))
    ids = [m.segment_id for m in result.measurements]
    assert len(ids) == len(set(ids)), ids


def test_a_later_start_in_the_afternoon_is_shadier(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sweep has to actually vary with the hour, or it is a table of one number.

    A flat horizon and a midday start: the sun is highest at noon, so the clear-sky
    irradiance a candidate sees must fall away from it in both directions.
    """
    _patch_horizons(monkeypatch)
    route = _route()
    result = sweep.start_time_optimizer(route, _segments(route), _ctx(_Layers()), _etas(route))
    rows = {
        m.segment_id: m.values
        for m in result.measurements
        if m.segment_id.startswith(sweep.CANDIDATE_PREFIX)
    }
    noon = rows[f"{sweep.CANDIDATE_PREFIX}12:00"]["mean_clear_sky_w_m2"]
    evening = rows[f"{sweep.CANDIDATE_PREFIX}15:00"]["mean_clear_sky_w_m2"]
    morning = rows[f"{sweep.CANDIDATE_PREFIX}09:00"]["mean_clear_sky_w_m2"]
    assert noon > evening and noon > morning


def test_the_sweep_names_what_it_could_not_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    """Five columns, five sources, and an absent one has to reach the manifest."""
    _patch_horizons(monkeypatch)
    route = _route()
    result = sweep.start_time_optimizer(route, _segments(route), _ctx(_Layers()), _etas(route))
    missing = {c.kind for c in result.coverage if not c.checked}
    assert {"start_time_services", "start_time_transit"} <= missing, missing


def _patch_horizons(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Coverage:
        answered = True

        @staticmethod
        def entries() -> list[Any]:
            return []

    class _Horizons:
        horizons = np.zeros((21, 8))
        svf = np.ones(21)
        coverage = _Coverage()

    monkeypatch.setattr(sweep, "corridor_horizons", lambda route, ctx: _Horizons())


# --- cell coverage ----------------------------------------------------------


def _run_cell(layers: Any, points: int = 41) -> Any:
    from longrun.core.scorers import cell_coverage as cells

    route = _route(points)
    return cells.cell_coverage(route, _segments(route), _ctx(layers), _etas(route))


def test_cell_coverage_names_the_blocker_rather_than_the_absence() -> None:
    """A source nobody can load and a source nobody has loaded read the same on a sheet
    unless the reason says which. The FCC bulk download is behind an account."""
    result = _run_cell(_Layers())
    entry = result.coverage[0]
    assert not entry.checked
    assert "account" in (entry.reason or "")


def test_a_route_inside_coverage_reports_no_gap() -> None:
    from shapely.geometry import box

    covered = _frame([{"provider": "Carrier A"}], [box(LON - 1, LAT - 1, LON + 1, LAT + 1)])
    result = _run_cell(_Layers(cell_coverage=covered))
    summary = _summary(result)
    assert summary["dark_fraction"] == 0.0
    assert summary["carriers"] == "Carrier A"
    assert not result.flags


def test_coverage_is_reported_as_a_claim_not_a_measurement() -> None:
    """Carrier self-report. A route inside a filed polygon has not been shown to have
    signal, so no measurement here may carry full confidence."""
    from shapely.geometry import box

    from longrun.core.scorers import cell_coverage as cells

    covered = _frame([{"provider": "A"}], [box(LON - 1, LAT - 1, LON + 1, LAT + 1)])
    result = _run_cell(_Layers(cell_coverage=covered))
    assert all(m.confidence == cells.SELF_REPORT_CONFIDENCE for m in result.measurements)
    assert any("filed by carriers" in (c.reason or "") for c in result.coverage)


def test_a_long_dark_stretch_is_flagged_and_a_short_one_is_not() -> None:
    """The route is 40 steps of ~44 m, so an uncovered half is ~880 m - well under the
    3 km threshold. A route with no coverage at all is 1.76 km and also under it, which is
    why the flag needs a route long enough to exceed the threshold to fire at all."""

    from longrun.core.scorers import cell_coverage as cells

    nothing = _frame([], [])
    short = _run_cell(_Layers(cell_coverage=nothing), points=21)
    assert _summary(short)["dark_fraction"] == 1.0
    assert not short.flags, "880 m is under the gap threshold"

    long_route = _run_cell(_Layers(cell_coverage=nothing), points=121)
    assert long_route.flags
    assert {f.reason_code for f in long_route.flags} == {"no_signal_gap"}
    assert _summary(long_route)["longest_gap_m"] >= cells.GAP_FLAG_M


def test_a_gap_is_measured_across_segment_boundaries() -> None:
    """Two adjacent segments each half-dark are one gap, not two shorter ones - and
    measuring per segment would report two and flag neither."""
    from shapely.geometry import box

    from longrun.core.scorers import cell_coverage as cells

    # Covered only at the very start, so everything after it is one continuous dark run.
    covered = _frame([{"provider": "A"}], [box(LON - 0.001, LAT - 0.001, LON + 0.001, LAT + 0.001)])
    result = _run_cell(_Layers(cell_coverage=covered), points=121)
    gaps = [f for f in result.flags if f.reason_code == "no_signal_gap"]
    assert len(gaps) == 1, [f.detail for f in gaps]
    assert _summary(result)["longest_gap_m"] > cells.GAP_FLAG_M
