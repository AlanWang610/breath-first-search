"""`hazards` and the NWS alert client (scope 7.6, 3.6, 12).

The scorer has more inputs than any other — way tags, railways, flowlines, nodes, the DEM
and an API — and every one of them can be absent. So most of what is worth testing is not
"does it find the creek" but **"what does it say when it could not look"**: a route scored
with no railway layer is not a route with no rail crossings, and the difference has to
survive all the way to the coverage manifest.

The positive cases are pinned by construction in `tests/golden/routes/synthetic-hazards`,
where a rail line crosses at 573 m and a creek at 2,946 m because that is where they were
drawn. What is here is the semantics those numbers rest on.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point

from longrun.core.data.alerts import (
    ALERT_HORIZON_DAYS,
    Alert,
    RouteAlerts,
    SiteAlerts,
    alert_args,
    covers_day,
    parse_alerts,
)
from longrun.core.data.cache import SqliteCache
from longrun.core.data.file_store import LayerNotFound
from longrun.core.models.context import Budget, FrozenClock, ScorerContext
from longrun.core.models.coverage import CoverageManifest
from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.models.measurement import FlagKind, Tier
from longrun.core.models.profile import PreferenceProfile
from longrun.core.scorers import hazards as mod
from longrun.core.scorers._common import ROUTE_SUMMARY_ID

LAT, LON, STEP = 37.7955, -122.4000, 0.0005


def _route(points: int = 11, elevation: float | None = 100.0) -> Route:
    return Route(
        id="r",
        points=[
            RoutePoint(lat=LAT, lon=LON + i * STEP, cum_dist_m=i * 44.0, ele_m=elevation)
            for i in range(points)
        ],
    )


def _segments(route: Route, way_id: int | None = 1) -> list[Any]:
    from longrun.core.geo.segments import segment_route

    return segment_route(route, way_ids=[way_id] * len(route.points))


class _Layers:
    """A store answering only what a test gives it; everything else is `LayerNotFound`.

    Deliberately not a mock of the whole protocol: the point of most of these tests is that
    a *missing* layer produces an honest coverage entry, so the default has to be absence.
    """

    def __init__(self, **frames: Any) -> None:
        self._frames = frames

    def _get(self, layer: str) -> Any:
        if layer not in self._frames:
            raise LayerNotFound(f"no {layer!r} layer")
        return self._frames[layer]

    def ways_in_corridor(self, corridor: Any) -> Any:
        return self._get("ways")

    def points_in_corridor(self, corridor: Any, kinds: list[str], layer: str = "amenities") -> Any:
        frame = self._get(layer)
        if kinds and "kind" in frame.columns:
            return frame[frame["kind"].isin(kinds)]
        return frame

    def polygons_intersecting(self, corridor: Any, layer: str) -> Any:
        return self._get(layer)

    def lines_crossing(self, route: Any, layer: str) -> Any:
        return self._get(layer)

    def has_layer(self, layer: str) -> bool:
        return layer in self._frames

    def vintage(self, layer: str) -> str | None:
        return None


class _Rasters:
    def has_layer(self, name: str) -> bool:
        return False

    def read_window(self, *args: Any, **kwargs: Any) -> None:
        return None


def _ctx(layers: Any, when: datetime | None = None) -> ScorerContext:
    return ScorerContext(
        layers=layers,
        rasters=_Rasters(),
        cache=SqliteCache(offline=True),
        clock=FrozenClock(when or datetime(2026, 9, 12, 7, 0)),
        coverage=CoverageManifest(),
        profile=PreferenceProfile(),
        budget=Budget(),
    )


def _lines(records: list[dict], geometries: list[Any]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(records, geometry=geometries, crs="EPSG:4326")


def _crossing_at(index: float) -> LineString:
    lon = LON + index * STEP
    return LineString([(lon, LAT - 0.002), (lon, LAT + 0.002)])


def _run(layers: Any, route: Route | None = None, when: datetime | None = None) -> Any:
    route = route or _route()
    return mod.hazards(
        route, _segments(route), _ctx(layers, when), [when or datetime(2026, 9, 12, 7, 0)]
    )


def _codes(result: Any) -> list[str]:
    return [f.reason_code for f in result.flags]


def _entry(result: Any, source: str) -> Any:
    return next((c for c in result.coverage if c.source == source), None)


# --- absence is not evidence ------------------------------------------------


def test_every_missing_source_is_named_rather_than_passed_over() -> None:
    """The whole reason this scorer is shaped as one pass per source.

    With nothing available it must report six things it could not check, not zero hazards.
    """
    result = _run(_Layers())
    missing = {c.source for c in result.coverage if not c.checked}
    assert {"ways", "railways", "flowlines", "nodes", "nws_alerts"} <= missing, missing
    assert not result.flags


def test_a_missing_railway_layer_does_not_read_as_no_rail_crossings() -> None:
    result = _run(_Layers(ways=_lines([{"way_id": 1}], [_crossing_at(5)])))
    entry = _entry(result, "railways")
    assert entry is not None and not entry.checked
    assert "not established" in (entry.reason or "")


def test_a_present_but_empty_layer_reads_as_checked_and_nothing_found() -> None:
    """The distinction the whole coverage design turns on: an empty corridor is evidence."""
    empty = _lines([], [])
    result = _run(_Layers(railways=empty, flowlines=empty))
    for source in ("railways", "flowlines"):
        entry = _entry(result, source)
        assert entry is not None and entry.checked, source
    assert "rail_at_grade" not in _codes(result)


# --- rail -------------------------------------------------------------------


def test_a_railway_across_the_route_is_flagged_at_grade() -> None:
    result = _run(_Layers(railways=_lines([{"way_id": 9}], [_crossing_at(4.5)])))
    assert _codes(result) == ["rail_at_grade"]
    flag = result.flags[0]
    assert flag.kind is FlagKind.SOFT
    # ADR 0011: lexicographic tiers mean SAFETY would outrank an entire route of heat.
    assert flag.tier is Tier.COMFORT


def test_a_railway_running_alongside_the_route_is_not_a_crossing() -> None:
    """The negative control the synthetic golden also carries."""
    alongside = LineString([(LON, LAT + 0.002), (LON + 0.005, LAT + 0.002)])
    result = _run(_Layers(railways=_lines([{"way_id": 9}], [alongside])))
    assert "rail_at_grade" not in _codes(result)


def test_a_railway_the_route_runs_along_is_not_a_crossing() -> None:
    """Collinear, not merely parallel - and the distinction is the whole test.

    A parallel line never touches the route, so it exercises nothing: `intersection` is
    empty and the run-along filter is never reached. Deleting that filter left the
    parallel test green, which made it a test of shapely rather than of this scorer. A
    tram running *down* the path shares a LineString with it, and only that reaches the
    branch which drops non-Point components.
    """
    along = LineString([(LON, LAT), (LON + 0.003, LAT)])
    result = _run(_Layers(railways=_lines([{"way_id": 9}], [along])))
    assert "rail_at_grade" not in _codes(result)


def test_a_route_that_joins_a_railway_and_then_leaves_it_still_reports_no_crossing() -> None:
    """The shape a real street tramway makes: a shared run with endpoints on the line.

    Shapely returns a GeometryCollection of the shared LineString plus, sometimes, the
    touching points. Counting those endpoints as crossings would put two rail hazards on
    every tram street in a city."""
    along = LineString([(LON + 0.001, LAT), (LON + 0.003, LAT)])
    result = _run(_Layers(railways=_lines([{"way_id": 9}], [along])))
    assert "rail_at_grade" not in _codes(result)


def test_a_railway_under_a_bridge_the_route_is_on_is_not_at_grade() -> None:
    """Two lines meeting in plan view have not met on the ground."""
    ways = _lines([{"way_id": 1, "highway": "footway", "bridge": "yes"}], [_crossing_at(0)])
    result = _run(_Layers(ways=ways, railways=_lines([{"way_id": 9}], [_crossing_at(4.5)])))
    assert "rail_at_grade" not in _codes(result)


# --- water ------------------------------------------------------------------


def test_a_watercourse_with_no_bridge_is_a_possible_crossing_at_low_confidence() -> None:
    """Flagged, but as evidence about the map rather than about the ground."""
    flow = _lines([{"permanent_identifier": "c", "gnis_name": "Creek"}], [_crossing_at(6.5)])
    result = _run(_Layers(flowlines=flow))
    assert "possible_water_crossing" in _codes(result)
    assert "separately digitised" in result.flags[0].detail
    entry = _entry(result, "flowlines")
    assert entry is not None and entry.confidence == mod.CONFLATION_CONFIDENCE


def test_a_tagged_ford_is_a_different_and_stronger_claim() -> None:
    """`ford=yes` is somebody saying they have been there; the inferred crossing is not."""
    ways = _lines([{"way_id": 1, "highway": "path", "ford": "yes"}], [_crossing_at(0)])
    flow = _lines([{"permanent_identifier": "c", "gnis_name": "Creek"}], [_crossing_at(6.5)])
    result = _run(_Layers(ways=ways, flowlines=flow))
    assert "confirmed_ford" in _codes(result)
    assert mod.SEVERITY_BY_CODE["confirmed_ford"] > mod.SEVERITY_BY_CODE["possible_water_crossing"]


def test_a_bridged_watercourse_is_not_a_crossing() -> None:
    ways = _lines([{"way_id": 1, "highway": "footway", "bridge": "yes"}], [_crossing_at(0)])
    flow = _lines([{"permanent_identifier": "c"}], [_crossing_at(6.5)])
    result = _run(_Layers(ways=ways, flowlines=flow))
    assert "possible_water_crossing" not in _codes(result)


# --- way tags ---------------------------------------------------------------


def test_a_tunnel_on_the_route_is_flagged() -> None:
    ways = _lines([{"way_id": 1, "highway": "footway", "tunnel": "yes"}], [_crossing_at(0)])
    assert "tunnel" in _codes(_run(_Layers(ways=ways)))


def test_a_trunk_bridge_with_no_footway_is_flagged() -> None:
    ways = _lines(
        [{"way_id": 1, "highway": "trunk", "bridge": "yes", "sidewalk": "no"}], [_crossing_at(0)]
    )
    assert "bridge_no_walkway" in _codes(_run(_Layers(ways=ways)))


def test_a_residential_bridge_with_no_footway_is_not_flagged() -> None:
    """Below the reported classes a bridge is a street over a creek shared with nobody."""
    ways = _lines(
        [{"way_id": 1, "highway": "residential", "bridge": "yes", "sidewalk": "no"}],
        [_crossing_at(0)],
    )
    assert "bridge_no_walkway" not in _codes(_run(_Layers(ways=ways)))


def test_a_bridge_whose_tags_do_not_say_is_not_flagged_either_way() -> None:
    """Scope 12: unknown is not absent, and it is also not a flag."""
    ways = _lines([{"way_id": 1, "highway": "trunk", "bridge": "yes"}], [_crossing_at(0)])
    assert mod.has_walkway({"highway": "trunk", "bridge": "yes"}) is None
    assert "bridge_no_walkway" not in _codes(_run(_Layers(ways=ways)))


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"sidewalk": "both"}, True),
        ({"sidewalk": "no"}, False),
        ({"highway": "footway"}, True),
        ({"highway": "trunk"}, None),
    ],
)
def test_walkway_detection_has_three_answers(tags: dict, expected: bool | None) -> None:
    assert mod.has_walkway(tags) is expected


# --- cattle grids -----------------------------------------------------------


def test_a_cattle_grid_on_the_line_is_flagged_and_one_well_off_it_is_not() -> None:
    """A grid twenty metres away is on a different path, which is why the radius is tight."""
    on_line = Point(LON + 3 * STEP, LAT)
    far = Point(LON + 3 * STEP, LAT + 0.002)
    nodes = _lines(
        [{"node_id": 1, "kind": "cattle_grid"}, {"node_id": 2, "kind": "cattle_grid"}],
        [on_line, far],
    )
    result = _run(_Layers(nodes=nodes))
    assert _codes(result).count("cattle_grid") == 1


# --- seasonal snow ----------------------------------------------------------


@pytest.mark.parametrize(
    ("elevation", "month", "expected"),
    [
        (2500.0, 2, True),
        (2500.0, 7, False),
        (500.0, 2, False),
        (None, 2, None),
    ],
)
def test_snow_is_three_valued(elevation: float | None, month: int, expected: bool | None) -> None:
    """None is the one that matters: a route that loses its terrain over a pass is exactly
    the route the question is for, and "no elevation" is not "no snow"."""
    assert mod.snow_plausible(elevation, date(2026, month, 15)) is expected


def test_a_route_with_no_elevation_reports_snow_as_unchecked() -> None:
    route = _route(elevation=None)
    result = mod.hazards(route, _segments(route), _ctx(_Layers()), [datetime(2026, 2, 1, 7, 0)])
    entry = _entry(result, "dem")
    assert entry is not None and not entry.checked
    assert "no elevation" in (entry.reason or "")


def test_a_high_route_in_winter_is_flagged() -> None:
    route = _route(elevation=2600.0)
    result = mod.hazards(route, _segments(route), _ctx(_Layers()), [datetime(2026, 2, 1, 7, 0)])
    assert "seasonal_snow" in _codes(result)


# --- NWS alerts -------------------------------------------------------------


def test_only_the_alert_families_the_scope_names_are_kept() -> None:
    payload = {
        "features": [
            {"properties": {"event": "Flood Warning", "severity": "Severe", "headline": "h"}},
            {"properties": {"event": "Red Flag Warning", "severity": "Severe", "headline": "h"}},
            {"properties": {"event": "Air Quality Alert", "severity": "Minor", "headline": "h"}},
        ]
    }
    events = {a.event for a in parse_alerts(payload)}
    assert events == {"Flood Warning", "Red Flag Warning"}


def test_an_alert_is_sorted_into_one_of_the_two_families() -> None:
    flood = Alert("Flash Flood Watch", "Severe", "h", None, None)
    fire = Alert("Red Flag Warning", "Severe", "h", None, None)
    assert flood.family == "flood"
    assert fire.family == "fire_weather"


def test_an_alert_window_that_cannot_be_read_is_unknown_not_true() -> None:
    """Placing it anyway would put a flood warning on a day it may not touch."""
    assert covers_day(Alert("Flood Warning", "Severe", "h", None, None), date(2026, 9, 12)) is None
    assert (
        covers_day(Alert("Flood Warning", "Severe", "h", "nonsense", None), date(2026, 9, 12))
        is None
    )


def test_an_alert_is_placed_against_the_day_it_covers() -> None:
    alert = Alert(
        "Flood Warning", "Severe", "h", "2026-09-11T00:00:00+00:00", "2026-09-13T00:00:00+00:00"
    )
    assert covers_day(alert, date(2026, 9, 12)) is True
    assert covers_day(alert, date(2026, 9, 20)) is False


def test_a_run_beyond_the_alert_horizon_is_unknown_not_an_all_clear() -> None:
    """NWS publishes active alerts, not forecasts of them. Silence at three weeks out is
    not the same claim as "no flood warning is in force"."""
    from longrun.core.data.alerts import route_alerts

    far = date(2026, 9, 12).replace(day=12).toordinal() + ALERT_HORIZON_DAYS + 5
    result = route_alerts(_route(), _ctx(_Layers()), date.fromordinal(far))
    assert result.beyond_horizon
    assert not result.sites


def test_alerts_are_deduplicated_across_sites() -> None:
    """Adjacent sample points share a warning zone, so the same alert comes back twice."""
    alert = Alert("Flood Warning", "Severe", "same headline", None, None)
    from longrun.core.data.forecast import ForecastSite

    site = ForecastSite(index=0, route_index=0, lat=LAT, lon=LON, cum_dist_m=0.0)
    route_result = RouteAlerts(
        sites=[SiteAlerts(site=site, alerts=[alert]), SiteAlerts(site=site, alerts=[alert])]
    )
    assert len(route_result.all_alerts) == 1


def test_the_alert_cache_key_rounds_so_nearby_sites_share_it() -> None:
    a = alert_args(37.795512, -122.400031, date(2026, 9, 12))
    b = alert_args(37.795489, -122.399977, date(2026, 9, 12))
    assert a == b


def test_alerts_are_recorded_against_the_route_not_a_segment(monkeypatch: Any) -> None:
    """An alert is issued over a county, so it has no position. `arbitrate` gives a flag
    whose segment it does not recognise the base weight of 1.0, which is the right answer
    for a condition that is nowhere in particular."""
    from longrun.core.data import alerts as alerts_mod

    alert = Alert(
        "Flood Warning",
        "Severe",
        "Flooding",
        "2026-09-11T00:00:00+00:00",
        "2026-09-13T00:00:00+00:00",
    )
    site = alerts_mod.ForecastSite(index=0, route_index=0, lat=LAT, lon=LON, cum_dist_m=0.0)
    monkeypatch.setattr(
        alerts_mod,
        "route_alerts",
        lambda *a, **k: RouteAlerts(sites=[SiteAlerts(site=site, alerts=[alert])]),
    )
    result = _run(_Layers())
    flood = [f for f in result.flags if f.reason_code == "flood_warning"]
    assert flood and flood[0].segment_id == ROUTE_SUMMARY_ID


# --- the summary ------------------------------------------------------------


def test_the_summary_counts_the_sources_that_answered() -> None:
    """A reader has to be able to tell a clean route from an unexamined one."""
    empty = _lines([], [])
    result = _run(_Layers(railways=empty, flowlines=empty, nodes=empty))
    summary = next(m for m in result.measurements if m.segment_id == ROUTE_SUMMARY_ID)
    assert summary.values["sources_checked"] >= 3
    assert summary.values["sources_missing"] >= 1
