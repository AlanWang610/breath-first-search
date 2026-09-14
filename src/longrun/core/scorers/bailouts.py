"""Where you can get off the route, and where you cannot (scope 7.7).

Scope's wording is *"transit stops and rideshare-plausible pickup points within reach of
each segment, with plausibility flags"*, and the word doing the work is **plausible**. A
stop 400 m away that has no service until Monday is not a bailout. A road on the map that
is a private fire track is not a pickup point. So each segment gets two independent
measurements and a flag only where both fail:

* **metres to the nearest transit stop that is in service at that segment's ETA** —
  service tested per day type against what `core.data.gtfs` precomputed, so a Sunday
  afternoon run is not told about a commuter-peak station;
* **metres to the nearest road a car could reach you on** — a drivable OSM way, which is
  the honest proxy for "a rideshare can stop here". It is a proxy and the scorer says so:
  OSM cannot tell us whether a driver will accept the fare.

**A segment with neither is the finding.** On a 20 km run it rarely matters; on a 100 km
run, a 9 km stretch with no exit is precisely the thing a plan should surface before
someone is standing in it with a hamstring gone.

Soft flags in COMFORT (ADR 0011). Being far from an exit is a logistics fact, and putting
it in the safety tier would make one remote segment outrank every heat and traffic finding
on the route — which is the failure ADR 0002 described.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from longrun.core.data.file_store import LayerNotFound
from longrun.core.geo.segments import corridor
from longrun.core.models.coverage import CoverageEntry
from longrun.core.models.geometry import Route, Segment
from longrun.core.models.measurement import (
    Flag,
    FlagKind,
    ScorerResult,
    SegmentMeasurement,
    Tier,
)
from longrun.core.scorers._common import ROUTE_SUMMARY_ID, WAYS_LAYER, row_tags
from longrun.core.scorers.base import record_coverage, unavailable
from longrun.core.scorers.transit import STOPS_LAYER, in_service

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext

name = "bailouts"

#: How far a runner in trouble will walk to an exit. Beyond this the segment is flagged.
#:
#: 2 km is roughly twenty-five minutes at a limp. Not a safety floor and not
#: profile-adjustable: scope 8.3 sets neither, and this is a reporting threshold rather
#: than a limit — the measured distance goes in the sheet whatever the flag says.
BAILOUT_REACH_M = 2000.0

#: How far out to look. Wider than the reach, so an out-of-reach segment can still report
#: how far the nearest exit actually is: "6.2 km to the nearest road" is information and
#: "beyond 2 km" is not.
SEARCH_BUFFER_M = 8000.0

#: OSM highway classes a car can reach you on. `track` is excluded deliberately: it is
#: routinely a farm or fire road behind a locked gate, and a bailout you cannot be met on
#: is worse than one you know you do not have.
DRIVABLE_CLASSES = frozenset(
    {
        "motorway",
        "trunk",
        "primary",
        "secondary",
        "tertiary",
        "unclassified",
        "residential",
        "living_street",
        "service",
        "motorway_link",
        "trunk_link",
        "primary_link",
        "secondary_link",
        "tertiary_link",
    }
)

SEVERITY_BY_CODE: dict[str, float] = {"no_bailout_in_reach": 0.6}

#: Confidence for a segment measured with only one of the two sources.
ONE_SOURCE_CONFIDENCE = 0.6


def is_drivable(tags: dict[str, Any]) -> bool:
    """Whether a way is one a car could reach a runner on."""
    if str(tags.get("motor_vehicle", "")).strip().lower() == "no":
        return False
    if str(tags.get("access", "")).strip().lower() in ("private", "no"):
        return False
    return str(tags.get("highway", "")).strip().lower() in DRIVABLE_CLASSES


def _served_stops(route: Route, ctx: ScorerContext) -> Any | None:
    """Stops in the wide corridor, or None when there is no layer to ask.

    None and an empty frame are different answers and both reach the sheet: no layer means
    exits were measured from roads alone, an empty frame means the corridor was searched
    and holds no stop.
    """
    try:
        return ctx.layers.points_in_corridor(
            corridor(route, buffer_m=SEARCH_BUFFER_M), [], layer=STOPS_LAYER
        )
    except (LayerNotFound, FileNotFoundError):
        return None


def _drivable_ways(route: Route, ctx: ScorerContext) -> Any | None:
    try:
        frame = ctx.layers.ways_in_corridor(corridor(route, buffer_m=SEARCH_BUFFER_M))
    except (LayerNotFound, FileNotFoundError):
        return None
    keep = [index for index, row in frame.iterrows() if is_drivable(row_tags(row))]
    return frame.loc[keep]


def bailouts(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
) -> ScorerResult:
    """Distance from each segment to the nearest usable exit (scope 7.7)."""
    stops = _served_stops(route, ctx)
    ways = _drivable_ways(route, ctx)
    if stops is None and ways is None:
        return unavailable(name, "no transit stops and no ways layer: bailouts not established")

    from shapely.geometry import Point
    from shapely.strtree import STRtree

    from longrun.core.geo.projections import local_crs, transformer_to

    crs = local_crs(route)
    to_local = transformer_to(crs)
    result = ScorerResult(name=name)

    road_tree: STRtree | None = None
    if ways is not None and len(ways) > 0:
        geometries = list(ways.to_crs(crs.to_epsg()).geometry)
        road_tree = STRtree(geometries) if geometries else None

    # Stops are tested for service at the segment's own ETA, so the set is rebuilt per
    # segment rather than once: a 90 km run finishing after the last train has a different
    # set of exits at 8 km than it does at 85 km, and that difference is the measurement.
    stop_points: list[tuple[Point, dict[str, Any]]] = []
    if stops is not None:
        for _, row in stops.iterrows():
            geometry = row.geometry
            if geometry is None or geometry.is_empty:
                continue
            stop_points.append(
                (
                    Point(*to_local.transform(geometry.x, geometry.y)),
                    dict(row.drop(labels=[stops.geometry.name])),
                )
            )

    worst_m = 0.0
    flagged = 0
    unknown_service = 0

    for index, segment in enumerate(segments):
        midpoint = _midpoint(route, segment, to_local)
        eta = _eta_for(etas, segment, route, index)

        stop_m, unreadable = _nearest_served_stop(stop_points, midpoint, eta)
        unknown_service += 1 if unreadable else 0
        road_m = _nearest_road(road_tree, midpoint)

        candidates = [d for d in (stop_m, road_m) if d is not None]
        nearest = min(candidates) if candidates else None
        sources = sum(1 for source in (stops, ways) if source is not None)

        if nearest is not None:
            worst_m = max(worst_m, nearest)
        if nearest is None or nearest > BAILOUT_REACH_M:
            flagged += 1
            result.flags.append(
                Flag(
                    scorer=name,
                    segment_id=segment.id,
                    kind=FlagKind.SOFT,
                    tier=Tier.COMFORT,
                    severity=SEVERITY_BY_CODE["no_bailout_in_reach"],
                    reason_code="no_bailout_in_reach",
                    detail=(
                        f"nothing within {BAILOUT_REACH_M / 1000:.0f} km at "
                        f"{segment.cum_start_m / 1000:.1f} km"
                        + ("" if nearest is None else f"; nearest exit {nearest / 1000:.1f} km")
                    ),
                )
            )

        result.measurements.append(
            SegmentMeasurement(
                segment_id=segment.id,
                values={
                    "transit_stop_m": None if stop_m is None else round(stop_m, 1),
                    "drivable_road_m": None if road_m is None else round(road_m, 1),
                    "nearest_exit_m": None if nearest is None else round(nearest, 1),
                },
                confidence=1.0 if sources == 2 else ONE_SOURCE_CONFIDENCE,
            )
        )

    result.measurements.append(
        SegmentMeasurement(
            segment_id=ROUTE_SUMMARY_ID,
            values={
                "reach_m": BAILOUT_REACH_M,
                "segments_out_of_reach": flagged,
                "worst_exit_m": round(worst_m, 1),
                "transit_checked": stops is not None,
                "roads_checked": ways is not None,
            },
            confidence=1.0,
        )
    )

    _record(result, ctx, stops, ways, unknown_service)
    return result


def _record(
    result: ScorerResult,
    ctx: ScorerContext,
    stops: Any,
    ways: Any,
    unknown_service: int,
) -> None:
    """Two sources, two coverage entries, and neither absence hidden by the other."""
    if stops is None:
        result.coverage.append(
            CoverageEntry(
                source=STOPS_LAYER,
                kind="bailout_transit",
                checked=False,
                reason="no transit stops layer: exits measured from roads only",
            )
        )
    elif unknown_service:
        result.coverage.append(
            CoverageEntry(
                source=STOPS_LAYER,
                kind="bailout_transit",
                checked=False,
                reason=(
                    f"{unknown_service} segment(s) had stops whose feed carried no calendar; "
                    f"service at the ETA not established"
                ),
            )
        )
    else:
        record_coverage(
            result,
            source=STOPS_LAYER,
            kind="bailout_transit",
            vintage=ctx.layers.vintage(STOPS_LAYER),
        )

    if ways is None:
        result.coverage.append(
            CoverageEntry(
                source=WAYS_LAYER,
                kind="bailout_roads",
                checked=False,
                reason="no ways layer: pickup points not established",
            )
        )
    else:
        record_coverage(
            result,
            source=WAYS_LAYER,
            kind="bailout_roads",
            vintage=ctx.layers.vintage(WAYS_LAYER),
        )


def _midpoint(route: Route, segment: Segment, to_local: Any) -> Any:
    """The segment's middle point, projected. Middle rather than start: a 250 m segment's
    start can be 250 m nearer an exit than the place you actually stop."""
    from shapely.geometry import Point

    index = (segment.start_idx + segment.end_idx) // 2
    point = route.points[min(index, len(route.points) - 1)]
    return Point(*to_local.transform(point.lon, point.lat))


def _eta_for(
    etas: list[datetime] | None, segment: Segment, route: Route, index: int
) -> datetime | None:
    if not etas:
        return None
    return etas[min(segment.start_idx, len(etas) - 1)]


def _nearest_served_stop(
    stops: list[tuple[Any, dict[str, Any]]], where: Any, when: datetime | None
) -> tuple[float | None, bool]:
    """Distance to the nearest stop in service, and whether any stop could not be judged.

    A stop whose service is unreadable is skipped rather than counted either way, and the
    caller is told it happened — reporting an unknown as an exit is the failure this scorer
    exists to avoid, and reporting it as no exit is the opposite one.
    """
    best: float | None = None
    unreadable = False
    for point, attributes in stops:
        if when is not None:
            served = in_service(attributes, when)
            if served is None:
                unreadable = True
                continue
            if not served:
                continue
        distance = where.distance(point)
        if best is None or distance < best:
            best = distance
    return best, unreadable


def _nearest_road(tree: Any, where: Any) -> float | None:
    if tree is None:
        return None
    index = tree.nearest(where)
    if index is None:
        return None
    return float(tree.geometries[int(index)].distance(where))


score = bailouts

__all__ = [
    "BAILOUT_REACH_M",
    "DRIVABLE_CLASSES",
    "ONE_SOURCE_CONFIDENCE",
    "SEARCH_BUFFER_M",
    "SEVERITY_BY_CODE",
    "bailouts",
    "is_drivable",
    "name",
    "score",
]
