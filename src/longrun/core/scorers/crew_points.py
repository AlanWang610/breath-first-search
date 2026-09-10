"""Where a support crew can meet the runner (scope 7.7).

Scope's wording: *"for supported runs: road-accessible meet points with parking, drive time
from the previous point, runner ETA"*. Three things, and only two of them can be measured
from data a region build holds:

* **Where.** A car park within reach of the route, and near enough to a drivable road that
  a car can actually get to it. Both halves are checked: `amenity=parking` on an island
  with no road to it is not a meet point, and OSM has plenty of those.
* **When.** The runner's ETA there, straight off the pacing model.
* **Drive time from the previous point.** This needs a router, and repair mode has none —
  scope 6.1 lets the user supply the geometry precisely so that no router is required.

So drive time is reported as **unknown**, in the coverage manifest, with the reason. That
is `routing/null.py`'s rule applied to a scorer: a `NullRouter` degrades the plan to
flag-but-don't-fix rather than pretending, and a crew table with a made-up drive time is
worse than one that says the drive was not computed. Straight-line distance between
consecutive points is reported instead, clearly labelled as such, because it is a real
lower bound and a crew chief can read a map.

**Only for supported runs**, so nothing here flags. A route with no parking near it is not
a defect — most are — and turning that into a flag would put a soft finding on every
unsupported run in the system. The measurement is there for the runs that want it.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from longrun.core.data.file_store import LayerNotFound
from longrun.core.geo.segments import corridor
from longrun.core.models.coverage import CoverageEntry
from longrun.core.models.geometry import Route, Segment
from longrun.core.models.measurement import ScorerResult, SegmentMeasurement
from longrun.core.scorers._common import ROUTE_SUMMARY_ID, WAYS_LAYER, RouteFrame, row_tags
from longrun.core.scorers.bailouts import is_drivable
from longrun.core.scorers.base import record_coverage, unavailable

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext

name = "crew_points"

AMENITIES_LAYER = "amenities"

#: `amenity` values that are somewhere a car can wait.
PARKING_KINDS = frozenset({"parking", "parking_space", "parking_entrance"})

#: How far off the route a meet point can be. 400 m is the corridor buffer: further than
#: that and the runner is leaving the route to reach the crew, which is a different plan.
MEET_REACH_M = 400.0

#: How far a car park may be from a drivable way and still be reachable by car. Generous,
#: because a large car park's centroid is properly inside it and away from the road.
ROAD_REACH_M = 150.0

#: Prefix for a meet point's measurement id, following `ROUTE_SUMMARY_ID`'s precedent that
#: a `segment_id` may name something that is not a segment.
#:
#: The suffix is an ordinal rather than the distance. Distance reads better and does not
#: work: a city start has three car parks that all project to 0.0 km along the route, and
#: `meet@0.0km` three times is two measurements lost. The distance is in the values.
MEET_PREFIX = "meet#"

#: Confidence for a meet point whose road access could not be checked.
NO_ROADS_CONFIDENCE = 0.5


def is_parking(tags: dict[str, Any]) -> bool:
    """Whether a point is somewhere a support vehicle can stop.

    `access=private` and `customers` are excluded: a hotel's car park is not a meet point
    for a crew that is not staying there, and a barrier that says so is information.
    """
    if str(tags.get("access", "")).strip().lower() in ("private", "no", "customers"):
        return False
    kind = str(tags.get("kind", "")).strip().lower()
    amenity = str(tags.get("amenity", "")).strip().lower()
    return kind in PARKING_KINDS or amenity in PARKING_KINDS


def crew_points(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
) -> ScorerResult:
    """Road-accessible parking near the route, with the runner's ETA at each (scope 7.7)."""
    try:
        frame = ctx.layers.points_in_corridor(
            corridor(route, buffer_m=MEET_REACH_M), sorted(PARKING_KINDS), layer=AMENITIES_LAYER
        )
    except (LayerNotFound, FileNotFoundError):
        return unavailable(name, "no amenities layer: meet points not established")

    from shapely.geometry import Point

    from longrun.core.geo.projections import local_crs, transformer_to

    crs = local_crs(route)
    to_local = transformer_to(crs)
    positions = RouteFrame(route)
    result = ScorerResult(name=name)

    roads = _drivable(route, ctx, crs)
    found: list[dict[str, Any]] = []

    for _, row in frame.iterrows():
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        tags = row_tags(row)
        if not is_parking(tags):
            continue
        cum_m, offset_m = positions.locate(geometry.x, geometry.y)
        if offset_m > MEET_REACH_M:
            continue

        road_m: float | None = None
        if roads is not None:
            here = Point(*to_local.transform(geometry.x, geometry.y))
            road_m = _nearest(roads, here)
            if road_m is not None and road_m > ROAD_REACH_M:
                # Mapped parking with no road within 150 m is an artefact often enough
                # that including it would put a crew on a track they cannot drive.
                continue

        found.append(
            {
                "cum_dist_m": round(cum_m, 1),
                "offset_m": round(offset_m, 1),
                "road_m": None if road_m is None else round(road_m, 1),
                "name": str(tags.get("name") or "unnamed parking"),
                "eta": _eta_at(route, etas, cum_m),
            }
        )

    # Sorted on three keys, not one, because the id is derived from the position and two
    # ids that collide would overwrite each other in the sheet and in the golden digest.
    # A city start has several car parks projecting to 0.0 km along the route; distance
    # alone cannot tell them apart, and the order has to be the same on every run.
    found.sort(key=lambda item: (item["cum_dist_m"], item["offset_m"], item["name"]))
    previous: float | None = None
    for index, item in enumerate(found):
        gap = None if previous is None else round(item["cum_dist_m"] - previous, 1)
        previous = item["cum_dist_m"]
        result.measurements.append(
            SegmentMeasurement(
                segment_id=f"{MEET_PREFIX}{index:02d}",
                values={
                    "cum_dist_m": item["cum_dist_m"],
                    "offset_m": item["offset_m"],
                    "road_m": item["road_m"],
                    "name": item["name"],
                    "runner_eta": item["eta"],
                    # Along-route metres from the previous meet point. **Not** a drive
                    # time and not a road distance: no router is available in repair mode,
                    # and this is the lower bound a crew chief can read off a map.
                    "since_previous_m": gap,
                },
                confidence=1.0 if roads is not None else NO_ROADS_CONFIDENCE,
            )
        )

    result.measurements.append(
        SegmentMeasurement(
            segment_id=ROUTE_SUMMARY_ID,
            values={
                "meet_points": len(found),
                "reach_m": MEET_REACH_M,
                "road_access_checked": roads is not None,
                "largest_gap_m": _largest_gap(route, found),
            },
            confidence=1.0,
        )
    )

    record_coverage(
        result,
        source=AMENITIES_LAYER,
        kind="crew_parking",
        vintage=ctx.layers.vintage(AMENITIES_LAYER),
    )
    if roads is None:
        result.coverage.append(
            CoverageEntry(
                source=WAYS_LAYER,
                kind="crew_road_access",
                checked=False,
                reason="no ways layer: meet points not checked for road access",
            )
        )
    else:
        record_coverage(
            result,
            source=WAYS_LAYER,
            kind="crew_road_access",
            vintage=ctx.layers.vintage(WAYS_LAYER),
        )
    # Stated on every run, not only when a router is missing, because a router that could
    # answer this does not exist yet in any mode - scope 6.1's repair path has none by
    # design, and `routing/null.py` is what stands in for one.
    result.coverage.append(
        CoverageEntry(
            source="router",
            kind="crew_drive_time",
            checked=False,
            reason=(
                "no router in repair mode: distances between meet points are along the "
                "route, not driving times"
            ),
        )
    )
    return result


def _drivable(route: Route, ctx: ScorerContext, crs: Any) -> Any | None:
    try:
        frame = ctx.layers.ways_in_corridor(corridor(route, buffer_m=MEET_REACH_M * 3))
    except (LayerNotFound, FileNotFoundError):
        return None
    from shapely.strtree import STRtree

    keep = [index for index, row in frame.iterrows() if is_drivable(row_tags(row))]
    geometries = list(frame.loc[keep].to_crs(crs.to_epsg()).geometry)
    return STRtree(geometries) if geometries else None


def _nearest(tree: Any, where: Any) -> float | None:
    index = tree.nearest(where)
    if index is None:
        return None
    return float(tree.geometries[int(index)].distance(where))


def _eta_at(route: Route, etas: list[datetime] | None, cum_m: float) -> str | None:
    if not etas:
        return None
    best, arrival = None, etas[0]
    for index, point in enumerate(route.points):
        gap = abs(point.cum_dist_m - cum_m)
        if best is None or gap < best:
            best, arrival = gap, etas[min(index, len(etas) - 1)]
    return arrival.strftime("%H:%M")


def _largest_gap(route: Route, found: list[dict[str, Any]]) -> float:
    """The longest stretch with no meet point, start and finish included.

    Empty is a gap of the whole route, which is the correct answer for an unsupported
    stretch and not a missing one — the same rule `services.max_gap_m` follows.
    """
    marks = [0.0, *sorted(item["cum_dist_m"] for item in found), route.length_m]
    return round(max(b - a for a, b in zip(marks[:-1], marks[1:], strict=True)), 1)


score = crew_points

__all__ = [
    "MEET_PREFIX",
    "MEET_REACH_M",
    "PARKING_KINDS",
    "ROAD_REACH_M",
    "crew_points",
    "is_parking",
    "name",
    "score",
]
