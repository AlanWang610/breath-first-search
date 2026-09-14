"""Nearest-way snapping (scope 6.1, 7.1).

Repair mode has no router, so this is what lets the way-tag scorers read anything at all.
It is deliberately not map matching, and the tests pin the honesty guarantees rather than
pretending to topological correctness.
"""

from __future__ import annotations

import warnings

import geopandas as gpd
from shapely.geometry import LineString

from longrun.core.geo.matching import DEFAULT_TOLERANCE_M, assign_way_ids
from longrun.core.models.geometry import Route, RoutePoint

LAT, LON = 37.7955, -122.3937


def _route(n: int = 20) -> Route:
    return Route(
        id="r",
        points=[RoutePoint(lat=LAT, lon=LON + i * 0.00045, cum_dist_m=i * 40.0) for i in range(n)],
    )


def _ways(**columns: object) -> gpd.GeoDataFrame:
    """Two collinear ways: the first half of the route, then the second."""
    mid = LON + 10 * 0.00045
    end = LON + 19 * 0.00045
    data = {"way_id": [1, 2], **columns}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return gpd.GeoDataFrame(
            data,
            geometry=[
                LineString([(LON, LAT), (mid, LAT)]),
                LineString([(mid, LAT), (end, LAT)]),
            ],
            crs="EPSG:4326",
        )


def test_points_snap_to_the_way_beneath_them() -> None:
    match = assign_way_ids(_route(), _ways())
    assert match.way_ids[0] == 1
    assert match.way_ids[-1] == 2
    assert match.match_rate == 1.0


def test_distances_are_metric_not_degrees() -> None:
    match = assign_way_ids(_route(), _ways())
    assert all(d < 1.0 for d in match.distances_m)


def test_a_point_beyond_tolerance_is_unmatched_not_least_bad() -> None:
    """Scope 12: no nearest way within reach means unknown, not the closest guess."""
    far = Route(
        id="far",
        points=[
            RoutePoint(lat=LAT + 0.01, lon=LON + i * 0.00045, cum_dist_m=i * 40.0) for i in range(5)
        ],
    )
    match = assign_way_ids(far, _ways())
    assert match.way_ids == [None] * 5
    assert match.match_rate == 0.0


def test_tolerance_is_configurable() -> None:
    offset = Route(
        id="o",
        points=[
            RoutePoint(lat=LAT + 0.0004, lon=LON + i * 0.00045, cum_dist_m=i * 40.0)
            for i in range(5)
        ],
    )
    assert assign_way_ids(offset, _ways(), tolerance_m=10.0).match_rate == 0.0
    assert assign_way_ids(offset, _ways(), tolerance_m=100.0).match_rate == 1.0


def test_no_ways_yields_all_unmatched() -> None:
    empty = gpd.GeoDataFrame({"way_id": []}, geometry=[], crs="EPSG:4326")
    match = assign_way_ids(_route(n=4), empty)
    assert match.way_ids == [None] * 4
    assert match.is_usable is False


def test_missing_id_column_is_handled() -> None:
    ways = _ways().rename(columns={"way_id": "osm_id"})
    assert assign_way_ids(_route(n=4), ways).match_rate == 0.0


def test_usability_threshold_reflects_partial_coverage() -> None:
    """Below half matched, the tag scorers describe a minority of the route."""
    half = Route(
        id="h",
        points=[
            RoutePoint(
                lat=LAT if i < 2 else LAT + 0.01,
                lon=LON + i * 0.00045,
                cum_dist_m=i * 40.0,
            )
            for i in range(10)
        ],
    )
    match = assign_way_ids(half, _ways())
    assert 0.0 < match.match_rate < 0.5
    assert match.is_usable is False


def test_default_tolerance_is_sane_for_a_sidewalk_offset() -> None:
    """Wide enough for a separately-mapped sidewalk, narrow enough not to cross a median."""
    assert 10.0 <= DEFAULT_TOLERANCE_M <= 40.0
