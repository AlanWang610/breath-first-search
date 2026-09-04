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


def gpx_write(
    route: Route,
    destination: str | Path,
    waypoints: list[tuple[LatLon, str, WaypointKind]] | None = None,
) -> Path:
    """Emit GPX 1.1 with the route as a track, plus typed waypoints (scope 9)."""
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

    path = Path(destination)
    path.write_text(gpx.to_xml(version="1.1"), encoding="utf-8")
    return path
