"""Geometry spine tests (scope 3.4, 4.1, 7.9, 8.2).

Synthetic geometry only: no store, no raster, no network. These pin the segmentation
policy and the CRS rule, both of which everything downstream depends on.
"""

from __future__ import annotations

import io
import math
from pathlib import Path

import pytest

from longrun.core.geo.gpx import GpxError, gpx_read, gpx_write, haversine_m, normalize
from longrun.core.geo.projections import (
    bbox_of,
    centroid,
    local_crs,
    transformer_from,
    transformer_to,
    utm_epsg,
)
from longrun.core.geo.segments import (
    corridor,
    corridor_polygon,
    locked_segment_ids,
    position_fraction,
    segment_route,
)
from longrun.core.models.geometry import LatLon, Route, RoutePoint
from longrun.core.models.request import LockedRange

FERRY_BUILDING = (37.7955, -122.3937)


def _straight_route(n: int = 21, spacing_m: float = 100.0) -> Route:
    """A route with exact, known cumulative distances."""
    return Route(
        id="r",
        points=[
            RoutePoint(lat=37.77 + i * 0.0009, lon=-122.4, cum_dist_m=i * spacing_m)
            for i in range(n)
        ],
    )


# --- distance and projection ------------------------------------------------


def test_one_degree_of_latitude_is_about_111_km() -> None:
    assert haversine_m(37.0, -122.0, 38.0, -122.0) == pytest.approx(111_195, rel=1e-3)


def test_haversine_is_symmetric_and_zero_on_itself() -> None:
    a, b = (37.0, -122.0), (37.5, -122.5)
    assert haversine_m(*a, *b) == pytest.approx(haversine_m(*b, *a))
    assert haversine_m(*a, *a) == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize(
    ("lat", "lon", "expected"),
    [
        (37.77, -122.4, 32610),  # San Francisco, zone 10 north
        (42.36, -71.06, 32619),  # Boston, zone 19 north
        (33.45, -112.07, 32612),  # Phoenix, zone 12 north
        (-33.87, 151.21, 32756),  # southern hemisphere takes the 327xx band
    ],
)
def test_utm_zone_selection(lat: float, lon: float, expected: int) -> None:
    assert utm_epsg(lat, lon) == expected


def test_utm_zone_clamps_at_the_antimeridian() -> None:
    assert utm_epsg(0.0, 180.0) == 32660


def test_local_crs_follows_the_route_centroid() -> None:
    assert local_crs(_straight_route()).to_epsg() == 32610


def test_centroid_is_the_mean_position() -> None:
    c = centroid(_straight_route(n=3, spacing_m=100.0))
    assert c.lon == pytest.approx(-122.4)
    assert c.lat == pytest.approx(37.7709)


def test_projection_round_trips() -> None:
    """always_xy=True, or EPSG:4326's declared lat/lon axis order transposes silently."""
    crs = local_crs(_straight_route())
    fwd, back = transformer_to(crs), transformer_from(crs)
    x, y = fwd.transform(FERRY_BUILDING[1], FERRY_BUILDING[0])
    lon, lat = back.transform(x, y)
    assert (lat, lon) == pytest.approx(FERRY_BUILDING, abs=1e-9)
    assert 400_000 < x < 700_000  # a plausible UTM easting, not a degree


def test_bbox_covers_every_point_and_pads() -> None:
    route = _straight_route()
    tight = bbox_of(route)
    assert tight.min_lat == pytest.approx(min(p.lat for p in route.points))
    assert tight.max_lat == pytest.approx(max(p.lat for p in route.points))
    padded = bbox_of(route, pad_deg=0.5)
    assert padded.min_lat < tight.min_lat
    assert padded.max_lon > tight.max_lon


def test_bbox_padding_cannot_leave_the_globe() -> None:
    route = Route(
        id="r",
        points=[
            RoutePoint(lat=89.9, lon=179.9, cum_dist_m=0.0),
            RoutePoint(lat=89.95, lon=179.95, cum_dist_m=100.0),
        ],
    )
    b = bbox_of(route, pad_deg=1.0)
    assert b.max_lat <= 90.0
    assert b.max_lon <= 180.0


# --- GPX --------------------------------------------------------------------


def test_gpx_round_trips_through_a_file(tmp_path: Path) -> None:
    route = _straight_route(n=5)
    out = gpx_write(route, tmp_path / "r.gpx")
    back = gpx_read(out)
    assert len(back.points) == len(route.points)
    assert back.points[0].lat == pytest.approx(route.points[0].lat)
    assert back.length_m == pytest.approx(route.length_m, rel=0.02)


def test_gpx_write_emits_typed_waypoints(tmp_path: Path) -> None:
    """Scope 9: water, toilets, bailouts and hazards ride along in the GPX."""
    out = gpx_write(
        _straight_route(n=3),
        tmp_path / "r.gpx",
        waypoints=[(LatLon(lat=37.77, lon=-122.4), "Fountain", "water")],
    )
    text = out.read_text(encoding="utf-8")
    assert "Fountain" in text
    assert "water" in text


def test_gpx_reads_a_route_when_there_is_no_track() -> None:
    """Planning tools commonly export <rte>; rejecting it makes normal imports fail."""
    xml = """<?xml version="1.0"?>
    <gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
      <rte><name>Planned</name>
        <rtept lat="37.770" lon="-122.400"/>
        <rtept lat="37.780" lon="-122.400"/>
      </rte>
    </gpx>"""
    route = gpx_read(io.StringIO(xml))
    assert len(route.points) == 2
    assert route.points[1].cum_dist_m > 1000.0


def test_gpx_rejects_malformed_xml_as_a_user_error() -> None:
    with pytest.raises(GpxError, match="could not parse"):
        gpx_read(io.StringIO("<gpx><unclosed>"))


def test_gpx_rejects_a_track_too_short_to_be_a_route() -> None:
    xml = """<?xml version="1.0"?>
    <gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
      <trk><trkseg><trkpt lat="37.77" lon="-122.4"/></trkseg></trk>
    </gpx>"""
    with pytest.raises(GpxError, match="at least two points"):
        gpx_read(io.StringIO(xml))


def test_normalize_drops_near_duplicate_points() -> None:
    raw = [(37.77, -122.4, None), (37.77, -122.4, None), (37.78, -122.4, None)]
    assert len(normalize(raw, dedupe_m=1.0)) == 2


def test_normalize_starts_at_zero_and_increases() -> None:
    pts = normalize([(37.77, -122.4, None), (37.78, -122.4, None)])
    assert pts[0].cum_dist_m == 0.0
    assert pts[1].cum_dist_m > pts[0].cum_dist_m


def test_normalize_handles_an_empty_track() -> None:
    assert normalize([]) == []


# --- segmentation -----------------------------------------------------------


@pytest.mark.parametrize(
    ("spacing_m", "max_len_m"), [(100.0, 250.0), (100.0, 1000.0), (50.0, 120.0)]
)
def test_segments_partition_the_route_exactly(spacing_m: float, max_len_m: float) -> None:
    """No gaps, no overlaps, and the ends are the route's ends."""
    route = _straight_route(n=21, spacing_m=spacing_m)
    segments = segment_route(route, max_len_m=max_len_m)
    assert segments[0].start_idx == 0
    assert segments[-1].end_idx == len(route.points) - 1
    for a, b in zip(segments[:-1], segments[1:], strict=True):
        assert a.end_idx == b.start_idx
    assert sum(s.length_m for s in segments) == pytest.approx(route.length_m)


@pytest.mark.parametrize("max_len_m", [120.0, 250.0, 999.0])
def test_no_segment_exceeds_the_maximum_length(max_len_m: float) -> None:
    """The limit is an upper bound, not a target it may overrun by one point."""
    route = _straight_route(n=41, spacing_m=100.0)
    for segment in segment_route(route, max_len_m=max_len_m):
        assert segment.length_m <= max_len_m


def test_a_single_step_longer_than_the_maximum_is_kept_whole() -> None:
    """It cannot be subdivided without inventing geometry; documented exception."""
    route = _straight_route(n=5, spacing_m=400.0)
    segments = segment_route(route, max_len_m=250.0)
    assert all(s.length_m == pytest.approx(400.0) for s in segments)
    assert sum(s.length_m for s in segments) == pytest.approx(route.length_m)


def test_segments_split_where_the_way_changes() -> None:
    route = _straight_route(n=10, spacing_m=50.0)
    segments = segment_route(route, way_ids=[1, 1, 1, 2, 2, 2, 2, 3, 3, 3], max_len_m=10_000.0)
    assert [s.way_id for s in segments] == [1, 2, 3]
    assert [(s.start_idx, s.end_idx) for s in segments] == [(0, 3), (3, 7), (7, 9)]


def test_segment_ids_are_stable_across_reruns() -> None:
    """A golden diff is unreviewable if ids shift when an unrelated scorer changes."""
    route = _straight_route()
    first = [s.id for s in segment_route(route, max_len_m=250.0)]
    second = [s.id for s in segment_route(route, max_len_m=250.0)]
    assert first == second
    assert first == sorted(first)


def test_way_ids_must_align_with_route_points() -> None:
    with pytest.raises(ValueError, match="way_ids has"):
        segment_route(_straight_route(n=5), way_ids=[1, 2])


def test_max_len_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        segment_route(_straight_route(), max_len_m=0.0)


def test_the_corridor_polygon_is_buffered_in_metres_not_degrees() -> None:
    """Shared by both `LayerStore` implementations, so getting this wrong is doubly wrong.

    At 37.8 degrees N a degree of longitude spans ~88 km against ~111 km for a degree of
    latitude. Buffer in degree space and the corridor comes out ~1.26x wider east-west than
    north-south; buffer in metres and the two grow together, which is what this asserts.

    Both come out slightly over 400 m because the polygon is bounded twice in different
    CRSs - an axis-aligned box in UTM is not axis-aligned in WGS84, so re-bounding inflates
    it. That errs toward fetching a few extra ways rather than missing one, which is the
    right direction for a query window.
    """
    window = corridor(_straight_route(), buffer_m=400.0)
    minx, miny, maxx, maxy = corridor_polygon(window).bounds

    grown_ew_deg = ((maxx - minx) - (window.bbox.max_lon - window.bbox.min_lon)) / 2
    grown_ns_deg = ((maxy - miny) - (window.bbox.max_lat - window.bbox.min_lat)) / 2
    grown_ew_m = grown_ew_deg * 111_320 * math.cos(math.radians(37.8))
    grown_ns_m = grown_ns_deg * 110_540

    # Degree-space buffering would put this ratio at ~1.27 (111.32 / 88.0); the double
    # re-bounding leaves it at ~1.04, so the margin between right and wrong is wide.
    assert grown_ew_m / grown_ns_m < 1.10, "east-west skew suggests a buffer applied in degrees"
    assert 400 <= grown_ew_m < 460
    assert 400 <= grown_ns_m < 460


def test_corridor_uses_the_scope_default_buffer() -> None:
    c = corridor(_straight_route())
    assert 300.0 <= c.buffer_m <= 500.0
    assert c.route_id == "r"


# --- position weighting inputs ---------------------------------------------


def test_position_fraction_uses_the_segment_midpoint() -> None:
    route = _straight_route(n=21, spacing_m=100.0)
    segments = segment_route(route, max_len_m=200.0)
    assert position_fraction(segments[0], route.length_m) == pytest.approx(0.05)
    assert position_fraction(segments[-1], route.length_m) == pytest.approx(0.95)


def test_position_fraction_is_bounded_and_safe_on_a_zero_length_route() -> None:
    route = _straight_route(n=21)
    segments = segment_route(route)
    assert all(0.0 <= position_fraction(s, route.length_m) <= 1.0 for s in segments)
    assert position_fraction(segments[0], 0.0) == 0.0


# --- locks ------------------------------------------------------------------


def test_lock_claims_every_overlapping_segment_not_only_contained_ones() -> None:
    """A half-covered segment still may not be rerouted (scope 6.4)."""
    route = _straight_route(n=21, spacing_m=100.0)
    segments = segment_route(route, max_len_m=200.0)
    locked = locked_segment_ids(segments, [LockedRange(start_m=350.0, end_m=450.0)])
    covered = [s for s in segments if s.id in locked]
    assert covered
    assert any(s.cum_start_m < 350.0 for s in covered)


def test_no_locks_means_no_locked_segments() -> None:
    segments = segment_route(_straight_route())
    assert locked_segment_ids(segments, []) == frozenset()


def test_lock_touching_a_boundary_does_not_claim_the_neighbour() -> None:
    """Zero-width overlap is not overlap, or every lock would spill by one segment."""
    route = _straight_route(n=21, spacing_m=100.0)
    segments = segment_route(route, max_len_m=200.0)
    locked = locked_segment_ids(segments, [LockedRange(start_m=0.0, end_m=200.0)])
    assert locked == frozenset({segments[0].id})
