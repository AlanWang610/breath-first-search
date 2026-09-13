"""GTFS summarising, and the two scorers that read it (scope 7.7).

Three layers, tested where each actually decides something.

`core.data.gtfs` collapses a feed into one row per stop, and the decisions worth pinning
are the ones that would silently over-claim: what counts as a weekday, what happens to a
departure at 25:10, and what a stop with no readable calendar becomes.

`transit` and `bailouts` both turn that summary into "is this stop any use at this
moment", and the case that matters in both is the **third** answer — a feed that cannot
say. Reporting an unreadable calendar as "not served" tells a runner to arrange a lift
they may not need; reporting it as served tells them not to.

The feed here is built with `zipfile` in a tmpdir, so this runs in CI with no network and
no `ingest` extra: GTFS is CSV in a zip and the standard library reads both.
"""

from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point

from longrun.core.data.cache import SqliteCache
from longrun.core.data.file_store import LayerNotFound
from longrun.core.data.gtfs import (
    BOARDABLE_LOCATION_TYPES,
    day_types,
    parse_time,
    summaries_to_frame,
    summarise_feed,
)
from longrun.core.models.context import Budget, FrozenClock, ScorerContext
from longrun.core.models.coverage import CoverageManifest
from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.models.profile import PreferenceProfile
from longrun.core.scorers import bailouts as bailouts_mod
from longrun.core.scorers import transit as transit_mod
from longrun.core.scorers._common import ROUTE_SUMMARY_ID

LAT, LON, STEP = 37.7955, -122.4000, 0.0005

#: 2026-09-14 is a Monday, 2026-09-12 a Saturday, 2026-09-13 a Sunday.
MONDAY = datetime(2026, 9, 14, 9, 0)
SATURDAY = datetime(2026, 9, 12, 9, 0)
SUNDAY = datetime(2026, 9, 13, 9, 0)


def _feed(tmp_path: Path, **override: str) -> Path:
    """A one-route, two-stop feed, small enough to reason about entirely."""
    files = {
        "stops.txt": (
            "stop_id,stop_name,stop_lat,stop_lon,location_type\n"
            f"S1,Near Stop,{LAT},{LON},0\n"
            f"S2,Far Stop,{LAT + 0.05},{LON},0\n"
            f"STN,A Station,{LAT},{LON},1\n"
        ),
        "routes.txt": "route_id,route_short_name,route_type\nR1,Line 1,1\n",
        "trips.txt": "trip_id,route_id,service_id\nT1,R1,WEEK\nT2,R1,WEEK\n",
        "calendar.txt": (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday\n"
            "WEEK,1,1,1,1,1,0,0\n"
        ),
        "stop_times.txt": (
            "trip_id,stop_id,arrival_time,departure_time,stop_sequence\n"
            "T1,S1,06:00:00,06:00:00,1\n"
            "T1,S2,06:20:00,06:20:00,2\n"
            "T2,S1,25:10:00,25:10:00,1\n"
        ),
    }
    files.update(override)
    path = tmp_path / "feed.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for member, body in files.items():
            archive.writestr(member, body)
    return path


# --- summarising ------------------------------------------------------------


def test_a_feed_becomes_one_row_per_boardable_stop(tmp_path: Path) -> None:
    """A `location_type=1` station is a parent whose platforms carry the times; counting it
    would put a phantom stop at the same coordinates as the real one."""
    summaries = {s.stop_id: s for s in summarise_feed(_feed(tmp_path), "t")}
    assert set(summaries) == {"S1", "S2"}
    assert "1" not in BOARDABLE_LOCATION_TYPES


def test_departures_are_counted_per_day_type(tmp_path: Path) -> None:
    summaries = {s.stop_id: s for s in summarise_feed(_feed(tmp_path), "t")}
    assert summaries["S1"].departures["weekday"] == 2
    assert summaries["S1"].departures.get("saturday", 0) == 0


def test_a_departure_after_midnight_keeps_its_hour(tmp_path: Path) -> None:
    """25:10 is the 01:10 service of the same service day. Wrapping it to 3,600 s would
    make it the *first* departure of the morning and shrink the span to nothing."""
    assert parse_time("25:10:00") == 25 * 3600 + 600
    summaries = {s.stop_id: s for s in summarise_feed(_feed(tmp_path), "t")}
    first, last = summaries["S1"].span["weekday"]
    assert first == 6 * 3600
    assert last == 25 * 3600 + 600


def test_a_stop_with_no_readable_calendar_is_dropped(tmp_path: Path) -> None:
    """Not summarised as never served: a confident zero from an unreadable feed is worse
    than an absent stop, because `bailouts` would count it as a checked non-exit."""
    path = _feed(tmp_path, **{"calendar.txt": "service_id,monday\n"})
    assert summarise_feed(path, "t") == []


def test_a_byte_order_mark_does_not_hide_the_first_column(tmp_path: Path) -> None:
    """GTFS files in the wild routinely carry one, and without `utf-8-sig` every lookup of
    `stop_id` misses and the feed summarises to nothing."""
    path = tmp_path / "bom.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "stops.txt",
            f"﻿stop_id,stop_name,stop_lat,stop_lon\nS1,Stop,{LAT},{LON}\n",
        )
        archive.writestr("calendar.txt", "service_id,monday\nW,1\n")
        archive.writestr("trips.txt", "trip_id,route_id,service_id\nT,R,W\n")
        archive.writestr(
            "stop_times.txt",
            "trip_id,stop_id,departure_time,stop_sequence\nT,S1,06:00:00,1\n",
        )
    assert [s.stop_id for s in summarise_feed(path, "t")] == ["S1"]


def test_weekday_means_any_weekday_not_all_five() -> None:
    """A Monday-to-Thursday commute service is weekday service, and a runner asking about
    a Tuesday is not helped by an all-five test."""
    row = dict.fromkeys(("monday", "tuesday", "wednesday", "thursday"), "1") | {
        "friday": "0",
        "saturday": "0",
        "sunday": "0",
    }
    assert day_types(row) == {"weekday"}


def test_a_missing_optional_member_is_an_empty_table_not_an_error(tmp_path: Path) -> None:
    path = tmp_path / "bare.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("stops.txt", f"stop_id,stop_name,stop_lat,stop_lon\nS1,S,{LAT},{LON}\n")
    assert summarise_feed(path, "t") == []


def test_the_frame_carries_the_columns_the_loader_declares(tmp_path: Path) -> None:
    """A column the summary forgets loads as NULL and reads back as "no calendar"."""
    from longrun.core.data.gtfs import GTFS_STOPS

    frame = summaries_to_frame(summarise_feed(_feed(tmp_path), "t"), "t")
    for column in (GTFS_STOPS.key, *GTFS_STOPS.columns):
        assert column in frame.columns, column


# --- the service test both scorers share ------------------------------------


def _stop(**values: Any) -> dict[str, Any]:
    base = {
        "name": "Stop",
        "routes": "Line 1",
        "modes": "subway",
        "weekday_departures": 100,
        "saturday_departures": 0,
        "sunday_departures": 0,
        "weekday_first_s": 6 * 3600,
        "weekday_last_s": 23 * 3600,
        "saturday_first_s": None,
        "saturday_last_s": None,
        "sunday_first_s": None,
        "sunday_last_s": None,
    }
    return base | values


def test_a_stop_is_in_service_inside_its_span_on_the_right_day() -> None:
    assert transit_mod.in_service(_stop(), MONDAY) is True


def test_a_weekday_only_stop_is_not_in_service_on_a_saturday() -> None:
    """The over-claim a single combined span would produce, and the reason the schema
    stores six columns instead of two."""
    assert transit_mod.in_service(_stop(), SATURDAY) is False


def test_a_stop_before_its_first_departure_is_not_in_service() -> None:
    assert transit_mod.in_service(_stop(), MONDAY.replace(hour=4)) is False


def test_a_stop_whose_feed_has_no_calendar_is_unknown_not_unserved() -> None:
    """Three states. "Not served" would tell a runner to arrange a lift they may not need."""
    assert transit_mod.in_service(_stop(weekday_first_s=None, weekday_last_s=None), MONDAY) is None


def test_a_service_running_past_midnight_still_covers_the_early_hours() -> None:
    """A 25:10 last departure is reachable at 01:10 on the same service day."""
    late = _stop(weekday_last_s=25 * 3600 + 600)
    assert transit_mod.in_service(late, MONDAY.replace(hour=1, minute=5)) is True


# --- the scorers ------------------------------------------------------------


def _route(points: int = 61) -> Route:
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
    def has_layer(self, name: str) -> bool:
        return False

    def read_window(self, *args: Any, **kwargs: Any) -> None:
        return None


def _ctx(layers: Any) -> ScorerContext:
    return ScorerContext(
        layers=layers,
        rasters=_Rasters(),
        cache=SqliteCache(offline=True),
        clock=FrozenClock(MONDAY),
        coverage=CoverageManifest(),
        profile=PreferenceProfile(),
        budget=Budget(),
    )


def _stops_frame(records: list[dict], points: list[Point]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(records, geometry=points, crs="EPSG:4326")


def _summary(result: Any) -> dict[str, Any]:
    return next(m for m in result.measurements if m.segment_id == ROUTE_SUMMARY_ID).values


def _run_transit(layers: Any, when: datetime = MONDAY) -> Any:
    route = _route()
    return transit_mod.transit(route, _segments(route), _ctx(layers), [when] * len(route.points))


def test_transit_is_unavailable_with_no_stop_layer() -> None:
    result = _run_transit(_Layers())
    assert not result.coverage[0].checked
    assert "not established" in (result.coverage[0].reason or "")


def test_a_station_beside_the_start_serves_it() -> None:
    frame = _stops_frame([_stop() | {"stop_id": "S1"}], [Point(LON, LAT)])
    values = _summary(_run_transit(_Layers(transit_stops=frame)))
    assert values["start_served"] is True
    assert values["start_stop_m"] < 5.0


def test_a_finish_far_from_any_stop_is_flagged_with_the_real_distance() -> None:
    """ "The closest station is 5.2 km away" is information; "none found" is not."""
    frame = _stops_frame([_stop() | {"stop_id": "S1"}], [Point(LON, LAT)])
    result = _run_transit(_Layers(transit_stops=frame))
    codes = [f.reason_code for f in result.flags]
    assert "finish_unserved" in codes
    assert _summary(result)["finish_stop_m"] > transit_mod.ENDPOINT_REACH_M


def test_a_finish_at_a_closed_station_is_a_different_flag_from_a_distant_one() -> None:
    """The two failures need different fixes - one is a lift home, the other a later start."""
    at_finish = Point(LON + 60 * STEP, LAT)
    frame = _stops_frame([_stop() | {"stop_id": "S1"}], [at_finish])
    result = _run_transit(_Layers(transit_stops=frame), when=SATURDAY)
    codes = [f.reason_code for f in result.flags]
    # The start is 2.6 km from the same single stop, so it is flagged too. Asserting
    # membership rather than the whole list: the point is which flag the *finish* gets,
    # and pinning the exact set would make this test fail for the other end's reasons.
    assert "finish_after_last_departure" in codes
    assert "finish_unserved" not in codes


def test_transit_flags_are_recorded_against_the_route() -> None:
    frame = _stops_frame([_stop() | {"stop_id": "S1"}], [Point(LON, LAT)])
    result = _run_transit(_Layers(transit_stops=frame))
    assert all(f.segment_id == ROUTE_SUMMARY_ID for f in result.flags)


# --- bailouts ---------------------------------------------------------------


def _run_bailouts(layers: Any, when: datetime = MONDAY) -> Any:
    route = _route()
    return bailouts_mod.bailouts(route, _segments(route), _ctx(layers), [when] * len(route.points))


def _ways(records: list[dict], geometries: list[Any]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(records, geometry=geometries, crs="EPSG:4326")


def test_bailouts_is_unavailable_with_neither_source() -> None:
    result = _run_bailouts(_Layers())
    assert not result.coverage[0].checked


def test_a_road_beside_the_route_is_an_exit() -> None:
    road = LineString([(LON, LAT + 0.0002), (LON + 0.04, LAT + 0.0002)])
    result = _run_bailouts(_Layers(ways=_ways([{"way_id": 1, "highway": "residential"}], [road])))
    assert _summary(result)["segments_out_of_reach"] == 0
    assert _summary(result)["worst_exit_m"] < 50.0


def test_a_route_far_from_any_road_or_stop_is_flagged() -> None:
    road = LineString([(LON, LAT + 0.2), (LON + 0.01, LAT + 0.2)])
    result = _run_bailouts(_Layers(ways=_ways([{"way_id": 1, "highway": "residential"}], [road])))
    assert _summary(result)["segments_out_of_reach"] > 0
    assert {f.reason_code for f in result.flags} == {"no_bailout_in_reach"}


def test_a_track_is_not_a_pickup_point() -> None:
    """Routinely a farm or fire road behind a locked gate. A bailout you cannot be met on
    is worse than one you know you do not have."""
    assert not bailouts_mod.is_drivable({"highway": "track"})
    assert bailouts_mod.is_drivable({"highway": "residential"})


def test_a_private_road_is_not_a_pickup_point() -> None:
    assert not bailouts_mod.is_drivable({"highway": "service", "access": "private"})


def test_a_stop_out_of_service_at_the_eta_is_not_an_exit() -> None:
    """The plausibility scope 7.7 asks for: a station 40 m away with no Saturday service
    is not somewhere a runner can leave from on a Saturday."""
    frame = _stops_frame([_stop() | {"stop_id": "S1"}], [Point(LON + 30 * STEP, LAT + 0.0002)])
    weekday = _summary(_run_bailouts(_Layers(transit_stops=frame), when=MONDAY))
    saturday = _summary(_run_bailouts(_Layers(transit_stops=frame), when=SATURDAY))
    assert weekday["segments_out_of_reach"] < saturday["segments_out_of_reach"]


def test_an_unreadable_calendar_is_reported_rather_than_counted_either_way() -> None:
    """Counting it as an exit is the failure this scorer exists to avoid; counting it as no
    exit is the opposite one. So it is skipped, and the manifest says it happened."""
    blind = _stop(weekday_first_s=None, weekday_last_s=None, weekday_departures=None)
    frame = _stops_frame([blind | {"stop_id": "S1"}], [Point(LON + 30 * STEP, LAT)])
    result = _run_bailouts(_Layers(transit_stops=frame))
    entry = next(c for c in result.coverage if c.source == transit_mod.STOPS_LAYER)
    assert not entry.checked
    assert "no calendar" in (entry.reason or "")


def test_confidence_drops_when_only_one_source_answered() -> None:
    road = LineString([(LON, LAT + 0.0002), (LON + 0.04, LAT + 0.0002)])
    result = _run_bailouts(_Layers(ways=_ways([{"way_id": 1, "highway": "residential"}], [road])))
    per_segment = [m for m in result.measurements if m.segment_id != ROUTE_SUMMARY_ID]
    assert per_segment
    assert all(m.confidence == bailouts_mod.ONE_SOURCE_CONFIDENCE for m in per_segment)


@pytest.mark.parametrize("source", ["transit_stops", "ways"])
def test_each_missing_source_is_named_separately(source: str) -> None:
    """Two sources, two entries: one absence must not be hidden by the other's presence."""
    road = LineString([(LON, LAT + 0.0002), (LON + 0.04, LAT + 0.0002)])
    frame = _stops_frame([_stop() | {"stop_id": "S1"}], [Point(LON, LAT)])
    both = {
        "ways": _ways([{"way_id": 1, "highway": "residential"}], [road]),
        "transit_stops": frame,
    }
    del both[source]
    result = _run_bailouts(_Layers(**both))
    entry = next(c for c in result.coverage if c.source == source)
    assert not entry.checked
