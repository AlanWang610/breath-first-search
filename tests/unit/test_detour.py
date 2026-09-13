"""Asking for a different route around one flagged span (scope 8.1 step 6).

No server. What is tested is the geometry of the request - where the pins go and what
shape the avoid-area is - because those are the two things that decide whether a detour
happens at all, and both were measured against a live GraphHopper before being written.

The measurements, on the Ferry Building to de Young pair over the Bay Area LTS graph:

* pins alone, no avoid-area: **7,695 m, 0 m divergence.** The pins sit on the route, so
  the router honours them by going exactly where it already went.
* pins plus the avoid-area: 8,152 m (+5.9%), 221 m divergence.

So the tests below are about the avoid-area's *shape*, which is what the live run turned
into a correction: a bounding box around a span that turns a corner closes a quarter of a
square kilometre of city, including the parallel street the detour was going to use.
"""

from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError
from shapely.geometry import Point, shape

from longrun.core.data.cache import args_hash
from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.routing.base import NullRouter
from longrun.core.routing.detour import (
    AREA_PRECISION,
    detour_area,
    detour_waypoints,
    index_at_distance,
    round_coordinates,
)


def _straight(n: int = 41, spacing_m: float = 100.0) -> Route:
    """A run due east from downtown San Francisco, 100 m between points."""
    return Route(
        id="straight",
        points=[
            RoutePoint(lat=37.7749, lon=-122.4194 + i * 0.001139, cum_dist_m=i * spacing_m)
            for i in range(n)
        ],
    )


def _corner(n: int = 21, spacing_m: float = 100.0) -> Route:
    """Half east, then half north: a span that turns a corner."""
    points: list[RoutePoint] = []
    half = n // 2
    for i in range(n):
        if i <= half:
            lat, lon = 37.7749, -122.4194 + i * 0.001139
        else:
            lat, lon = 37.7749 + (i - half) * 0.0009, -122.4194 + half * 0.001139
        points.append(RoutePoint(lat=lat, lon=lon, cum_dist_m=i * spacing_m))
    return Route(id="corner", points=points)


# --- finding a place on the route -------------------------------------------


def test_a_distance_past_the_finish_is_the_finish() -> None:
    """Clamped rather than raised: a flagged span at the very end of a route is ordinary,
    and the caller has nothing useful to do with an IndexError."""
    route = _straight()
    assert index_at_distance(route, 99_999.0) == len(route.points) - 1
    assert index_at_distance(route, -5.0) == 0


def test_a_distance_lands_on_the_point_at_or_before_it() -> None:
    route = _straight()
    assert index_at_distance(route, 1000.0) == 10
    assert index_at_distance(route, 1050.0) == 10
    assert index_at_distance(route, 1100.0) == 11


# --- the pins ----------------------------------------------------------------


def test_a_span_in_the_middle_is_pinned_on_both_sides() -> None:
    """Four waypoints: start, a pin before, a pin after, finish. The pins are what keep
    the rest of the route, so a candidate can be arbitrated against the original."""
    route = _straight()
    waypoints = detour_waypoints(route, 1800.0, 2200.0, margin_m=300.0)

    assert len(waypoints) == 4
    assert (waypoints[0].lat, waypoints[0].lon) == (route.points[0].lat, route.points[0].lon)
    assert (waypoints[-1].lat, waypoints[-1].lon) == (route.points[-1].lat, route.points[-1].lon)
    # 1800 - 300 = 1500 m, and 2200 + 300 = 2500 m.
    assert waypoints[1].lon == route.points[15].lon
    assert waypoints[2].lon == route.points[25].lon


def test_a_span_at_the_start_has_nothing_to_pin_before_it() -> None:
    """Collapsing to the endpoints is the honest answer - the request becomes a
    whole-route one - rather than pinning the start twice, which the router reads as a
    zero-length leg."""
    route = _straight()
    waypoints = detour_waypoints(route, 0.0, 200.0, margin_m=300.0)

    assert len(waypoints) == 3
    assert (waypoints[0].lat, waypoints[0].lon) == (route.points[0].lat, route.points[0].lon)


def test_a_route_too_short_to_detour_cannot_be_built_at_all() -> None:
    """So nothing downstream needs a guard for it, and the one written here was removed.

    A defensive check that cannot fire reads as a safety net and is not one.
    """
    with pytest.raises(ValidationError, match="at least two points"):
        Route(id="dot", points=[RoutePoint(lat=37.0, lon=-122.0, cum_dist_m=0.0)])


# --- the avoid-area ----------------------------------------------------------


def test_the_avoid_area_covers_the_span_and_not_the_pins() -> None:
    """If the area swallows a pin, the request is unsatisfiable and the router refuses -
    which `alternatives` would then swallow into an empty list, so the detour would
    silently never happen."""
    route = _straight()
    area = shape(detour_area(route, 1800.0, 2200.0, pad_m=60.0)["geometry"])
    waypoints = detour_waypoints(route, 1800.0, 2200.0, margin_m=300.0)

    assert area.contains(Point(route.points[20].lon, route.points[20].lat))
    for pin in waypoints:
        assert not area.contains(Point(pin.lon, pin.lat))


def test_the_avoid_area_follows_the_span_rather_than_boxing_it() -> None:
    """The correction a live run forced.

    A bounding box around a span that turns a corner also closes the inside of the corner
    - a quarter of a square kilometre of city on the measured route - and the street it
    takes with it is usually the one the detour was going to use. The area is a buffered
    line, so the inside of the corner stays open.
    """
    route = _corner()
    area = shape(detour_area(route, 300.0, 1700.0, pad_m=60.0)["geometry"])

    corner = route.points[len(route.points) // 2]
    # Well inside the bounding box of the span, and well away from the line itself.
    inside_the_elbow = Point(corner.lon - 0.004, corner.lat + 0.003)
    assert area.bounds[0] < inside_the_elbow.x < area.bounds[2]
    assert area.bounds[1] < inside_the_elbow.y < area.bounds[3]
    assert not area.contains(inside_the_elbow)


def test_the_avoid_area_is_metres_wide_at_any_latitude() -> None:
    """Buffered in the local metric CRS, never in degrees - `corridor_polygon`'s rule.

    60 m of longitude is not 60 m of latitude, and the error grows with latitude, so a
    degree-buffered area would be the wrong width everywhere except one parallel.
    """
    route = _straight()
    area = shape(detour_area(route, 1000.0, 1100.0, pad_m=60.0)["geometry"])
    on_the_line = route.points[10]

    # 50 m north of the span is inside; 120 m north is outside. One degree of latitude is
    # ~111 km, so those are 0.00045 and 0.00108 degrees.
    assert area.contains(Point(on_the_line.lon, on_the_line.lat + 0.00045))
    assert not area.contains(Point(on_the_line.lon, on_the_line.lat + 0.00108))


# --- the protocol ------------------------------------------------------------


def test_a_router_that_is_not_there_still_answers_the_new_signature() -> None:
    """`alternatives` grew a span and three costing arguments in M5.4; `NullRouter` is
    what repair mode runs on, and it must keep degrading rather than raising."""
    route = _straight()
    assert NullRouter().alternatives(route, (100.0, 200.0), k=3) == []
    assert NullRouter().alternatives(route, None, k=3, custom_model={"priority": []}) == []


# --- the cache key, which is where this bit ----------------------------------


def test_the_avoid_area_is_rounded_so_two_platforms_key_it_the_same() -> None:
    """That the contract holds at the configured precision.

    A proxy, and a weak one on its own - at a large enough `AREA_PRECISION` every double
    round-trips and this passes vacuously, which a sabotage run confirmed. The test below
    is the one that actually holds the property; this one says where it comes from.

    GEOS builds this buffer from trigonometry and PROJ transforms every vertex back, at 17
    significant digits. The polygon ends up inside `custom_model`, and `CachedRouter`
    hashes `custom_model` into the cache key - so a last-bit disagreement between the Linux
    and Windows builds of either library gave one detour request two keys, and the
    `loop-bayarea` golden replayed on Windows and missed its own cassette on Linux.
    """
    ring = detour_area(_straight(), 1000.0, 1100.0)["geometry"]["coordinates"][0]

    for lon, lat in ring:
        assert round(lon, AREA_PRECISION) == lon, f"{lon} carries more than {AREA_PRECISION} dp"
        assert round(lat, AREA_PRECISION) == lat


def test_noise_below_the_rounding_threshold_does_not_move_the_key() -> None:
    """The real regression test: the property asserted directly against `args_hash`.

    1e-12 degrees is ~0.1 micrometre on the ground - far below anything a router could act
    on, and far above nothing, which is what the key used to require.
    """
    area = detour_area(_straight(), 1000.0, 1100.0)
    nudged = deepcopy(area)
    ring = [[lon + 1e-12, lat] for lon, lat in nudged["geometry"]["coordinates"][0]]
    nudged["geometry"]["coordinates"] = [ring]

    assert args_hash(round_coordinates(area)) == args_hash(round_coordinates(nudged))


def test_rounding_a_polygon_keeps_its_shape() -> None:
    """0.11 m of vertex movement on a 60 m buffer: the fix must not deform the area it is
    protecting, or it trades a cache bug for a routing one."""
    route = _straight()
    area = shape(detour_area(route, 1000.0, 1100.0, pad_m=60.0)["geometry"])
    on_the_line = route.points[10]

    assert area.contains(Point(on_the_line.lon, on_the_line.lat + 0.00045))
    assert not area.contains(Point(on_the_line.lon, on_the_line.lat + 0.00108))


def test_it_rounds_a_polygon_it_did_not_build() -> None:
    """`round_coordinates` is public because an avoid-polygon a user drew, or one geocoded
    from `PlanRequest.avoid_names`, carries the same risk and has no other owner."""
    drawn = {"type": "Polygon", "coordinates": [[(1.123456789, 2.987654321)]]}

    assert round_coordinates(drawn)["coordinates"] == [[[1.123457, 2.987654]]]
