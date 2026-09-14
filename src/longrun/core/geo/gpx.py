"""GPX read and write (scope 7.9).

Both entry modes pass through here: repair mode reads the user's line as initial state
(scope 6.1), and every plan is emitted as GPX 1.1 with waypoints for water, toilets,
bailouts, hazards, gates and distance markers (scope 9).

Reading computes `cum_dist_m` once, in the route's local metric frame, because
everything downstream — pacing, position weighting, distance markers, locked ranges —
is expressed in distance along the route.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import IO, TYPE_CHECKING, Literal

import gpxpy
import gpxpy.gpx

from longrun.core.models.geometry import Route, RoutePoint

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.geometry import LatLon

#: Points closer together than this add no shape and inflate every per-point loop.
DEFAULT_DEDUPE_M = 1.0

_EARTH_RADIUS_M = 6_371_008.8

WaypointKind = Literal["water", "toilet", "food", "bailout", "hazard", "gate", "marker", "crew"]


class GpxError(ValueError):
    """A GPX file could not be parsed, or contained no usable track."""


def haversine_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    """Great-circle distance in metres.

    Used only to build `cum_dist_m` at read time, where its sub-metre disagreement with
    the projected frame is irrelevant and its lack of a CRS dependency is convenient.
    Anything that needs true planar geometry projects first (see `projections`).
    """
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = p2 - p1
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(h))


def _points_from_gpx(gpx: gpxpy.gpx.GPX) -> list[tuple[float, float, float | None]]:
    """Track points if there are any, else route points.

    A file exported as a *route* rather than a *track* is common from planning tools,
    and rejecting it would turn a normal import into a support question.
    """
    for track in gpx.tracks:
        for segment in track.segments:
            if segment.points:
                return [(p.latitude, p.longitude, p.elevation) for p in segment.points]
    for route in gpx.routes:
        if route.points:
            return [(p.latitude, p.longitude, p.elevation) for p in route.points]
    return []


def normalize(
    raw: list[tuple[float, float, float | None]], dedupe_m: float = DEFAULT_DEDUPE_M
) -> list[RoutePoint]:
    """Drop near-duplicate points and attach cumulative distance."""
    if not raw:
        return []

    lat, lon, ele = raw[0]
    out = [RoutePoint(lat=lat, lon=lon, ele_m=ele, cum_dist_m=0.0)]
    total = 0.0
    for lat, lon, ele in raw[1:]:
        step = haversine_m(out[-1].lat, out[-1].lon, lat, lon)
        if step < dedupe_m:
            continue
        total += step
        out.append(RoutePoint(lat=lat, lon=lon, ele_m=ele, cum_dist_m=total))
    return out


#: Longest gap between consecutive route points before `densify` fills it in.
#:
#: Scope 7.4 integrates irradiance at arrival times and a plan samples roughly every 100 m,
#: so 50 m is comfortably under what the scorers assume. A router emits points at junctions
#: only - GraphHopper put 133 m between two of them on a straight San Francisco street -
#: and every per-point measurement in the system is that much coarser without this.
DEFAULT_MAX_SPACING_M = 50.0


def densify(
    points: list[RoutePoint], max_spacing_m: float = DEFAULT_MAX_SPACING_M
) -> list[RoutePoint]:
    """Insert points so no two consecutive ones are more than `max_spacing_m` apart.

    `normalize`'s opposite number, and its complement: that one drops points too close
    together to mean anything, this one fills gaps too large to sample through. Both exist
    because the two sources of a route have opposite defects - a GPS track is dense and
    noisy, and a routed path is sparse and exact.

    Interpolated linearly in degrees. Over 50 m that is indistinguishable from a great
    circle at any latitude this project runs at, and the inserted points are *sampling*
    positions rather than claims about where the path went - the router already said that
    with the two points either side.

    Elevation is not interpolated. It comes from the terrain model (scope 7.1), never from
    the route, and `sample_elevation` reads the new points like any other.
    """
    if len(points) < 2 or max_spacing_m <= 0:
        return list(points)

    out: list[RoutePoint] = [points[0]]
    for previous, current in zip(points[:-1], points[1:], strict=True):
        gap = haversine_m(previous.lat, previous.lon, current.lat, current.lon)
        steps = int(gap // max_spacing_m)
        for step in range(1, steps + 1):
            fraction = step / (steps + 1)
            out.append(
                RoutePoint(
                    lat=previous.lat + (current.lat - previous.lat) * fraction,
                    lon=previous.lon + (current.lon - previous.lon) * fraction,
                )
            )
        out.append(current)

    # Distances are recomputed rather than adjusted: the inserted points change every
    # cumulative distance after them, and `cum_dist_m` is what every scorer keys off.
    return normalize([(p.lat, p.lon, p.ele_m) for p in out], dedupe_m=0.0)


def gpx_read(
    source: str | Path | IO[str],
    route_id: str = "route",
    dedupe_m: float = DEFAULT_DEDUPE_M,
) -> Route:
    """Parse a GPX file into a `Route`.

    Raises `GpxError` rather than letting an XML exception escape, because a malformed
    upload is an ordinary user event and the CLI has to report it as one.
    """
    try:
        if isinstance(source, str | Path):
            with Path(source).open(encoding="utf-8") as fh:
                gpx = gpxpy.parse(fh)
        else:
            gpx = gpxpy.parse(source)
    except Exception as exc:  # gpxpy raises several unrelated types
        raise GpxError(f"could not parse GPX: {exc}") from exc

    points = normalize(_points_from_gpx(gpx), dedupe_m=dedupe_m)
    if len(points) < 2:
        raise GpxError("GPX contains no track or route with at least two points")

    name = next((t.name for t in gpx.tracks if t.name), None)
    return Route(id=route_id, points=points, name=name, source="imported")


def gpx_string(
    route: Route, waypoints: list[tuple[LatLon, str, WaypointKind]] | None = None
) -> str:
    """The same GPX 1.1 `gpx_write` writes, as a string.

    Split out because GraphHopper's `/match` takes a GPX **body** rather than JSON, and
    routing a route through a temporary file to post it would be a second serialiser with
    a filesystem in the middle of it.
    """
    return _build(route, waypoints).to_xml(version="1.1")


def gpx_write(
    route: Route,
    destination: str | Path,
    waypoints: list[tuple[LatLon, str, WaypointKind]] | None = None,
) -> Path:
    """Emit GPX 1.1 with the route as a track, plus typed waypoints (scope 9)."""
    path = Path(destination)
    path.write_text(gpx_string(route, waypoints), encoding="utf-8")
    return path


def _build(
    route: Route, waypoints: list[tuple[LatLon, str, WaypointKind]] | None = None
) -> gpxpy.gpx.GPX:
    gpx = gpxpy.gpx.GPX()
    gpx.creator = "longrun"

    track = gpxpy.gpx.GPXTrack(name=route.name or route.id)
    segment = gpxpy.gpx.GPXTrackSegment()
    segment.points = [
        gpxpy.gpx.GPXTrackPoint(latitude=p.lat, longitude=p.lon, elevation=p.ele_m)
        for p in route.points
    ]
    track.segments.append(segment)
    gpx.tracks.append(track)

    for position, label, kind in waypoints or []:
        gpx.waypoints.append(
            gpxpy.gpx.GPXWaypoint(
                latitude=position.lat, longitude=position.lon, name=label, type=kind
            )
        )

    return gpx
