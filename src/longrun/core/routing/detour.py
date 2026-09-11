"""Turning a flagged span of a route into a request for a different one (scope 8.1 step 6).

Measured against a live GraphHopper on the Bay Area LTS graph before this was written, and
the **control** is what settled the design. Ferry Building to the de Young, 7,695 m:

* via points pinned either side of the span, and nothing else: **7,695 m, diverging from
  the original by 0 m.** The via points sit *on* the route, so the router honours them by
  going exactly where it already went. They move nothing.
* the same via points plus an avoid-area over the span: 8,126 m (+5.6%), diverging by up
  to 325 m, and **no point of the answer inside the avoided box**.

So the avoid-area is the whole mechanism and the via points are the whole constraint: the
area is what makes the route different, and the pins are what stop it from being different
everywhere. Either alone is useless - one returns the original, the other re-plans a route
that was only wrong for 200 m of its length.

Both pieces are plain geometry over a `Route`, so they are testable with no router at all,
which is the same reason `arbitrate.py` takes scored candidates rather than a proposer.
"""

from __future__ import annotations

from typing import Any

from longrun.core.models.geometry import LatLon, Route

#: How far beyond the flagged span the avoid-area reaches. Wide enough that a router
#: cannot clip its corner and call that a detour; narrow enough that it does not also
#: close the parallel street, which is usually the answer.
DETOUR_PAD_M = 60.0

#: How far either side of the span the route is pinned. The pins hold the approach and
#: the exit, so the reroute is local; too small and the avoid-area swallows the pin
#: itself, which makes the request unsatisfiable.
DETOUR_MARGIN_M = 250.0


def index_at_distance(route: Route, distance_m: float) -> int:
    """The index of the last route point at or before `distance_m`.

    Clamped at both ends: a distance past the finish is the finish, not an error. Route
    points carry `cum_dist_m`, so this is a scan over a sorted list rather than a
    re-measurement of the geometry.
    """
    if distance_m <= 0:
        return 0
    last = len(route.points) - 1
    for index, point in enumerate(route.points):
        if point.cum_dist_m > distance_m:
            return max(0, index - 1)
    return last


def detour_waypoints(
    route: Route,
    start_m: float,
    end_m: float,
    *,
    margin_m: float = DETOUR_MARGIN_M,
) -> list[LatLon]:
    """Start, a pin before the span, a pin after it, finish.

    The pins are what keep the rest of the route. Without them the router is free to
    re-plan the whole thing, and a candidate that differs everywhere cannot be arbitrated
    against the original segment by segment.

    Degenerate cases collapse to the endpoints rather than raising: a span that reaches
    the start of the route has nothing to pin before it, and the honest answer is a
    whole-route request, which is what an empty middle produces.

    There is no guard for a route too short to have a middle, because `Route` will not
    validate with fewer than two points - the check belongs where it already is.
    """
    points = route.points
    first = LatLon(lat=points[0].lat, lon=points[0].lon)
    last = LatLon(lat=points[-1].lat, lon=points[-1].lon)

    before_index = index_at_distance(route, start_m - margin_m)
    after_index = index_at_distance(route, end_m + margin_m)

    middle: list[LatLon] = []
    if before_index > 0:
        middle.append(LatLon(lat=points[before_index].lat, lon=points[before_index].lon))
    if after_index < len(points) - 1:
        middle.append(LatLon(lat=points[after_index].lat, lon=points[after_index].lon))

    return [first, *middle, last]


def detour_area(
    route: Route,
    start_m: float,
    end_m: float,
    *,
    pad_m: float = DETOUR_PAD_M,
    area_id: str = "detour",
) -> dict[str, Any]:
    """A GeoJSON feature covering the flagged span, for `route_body`'s `areas`.

    Buffered in the route's local metric CRS and transformed back, never buffered in
    degrees - the rule `corridor_polygon` states and for the same reason: 60 m of
    longitude is not 60 m of latitude, and the error grows with latitude.
    """
    from pyproj import CRS
    from shapely.geometry import LineString, Point, mapping
    from shapely.ops import transform as shapely_transform

    from longrun.core.geo.projections import transformer_from, transformer_to, utm_epsg

    start_index = index_at_distance(route, start_m)
    end_index = min(len(route.points) - 1, index_at_distance(route, end_m) + 1)
    span = route.points[start_index : end_index + 1] or route.points[start_index : start_index + 1]

    lats = [point.lat for point in span]
    lons = [point.lon for point in span]
    crs = CRS.from_epsg(utm_epsg(sum(lats) / len(lats), sum(lons) / len(lons)))
    to_local, to_wgs = transformer_to(crs), transformer_from(crs)

    xs, ys = to_local.transform(lons, lats)
    coords = list(zip(xs, ys, strict=True))
    # A buffered line, not the span's bounding box. A box around a diagonal 800 m span is
    # a quarter of a square kilometre of closed city - it takes the parallel street with
    # it, which is the street the detour was going to use.
    local = (LineString(coords) if len(coords) > 1 else Point(coords[0])).buffer(pad_m)

    return {
        "type": "Feature",
        "id": area_id,
        "properties": {},
        "geometry": mapping(shapely_transform(lambda x, y: to_wgs.transform(x, y), local)),
    }


__all__ = [
    "DETOUR_MARGIN_M",
    "DETOUR_PAD_M",
    "detour_area",
    "detour_waypoints",
    "index_at_distance",
]
