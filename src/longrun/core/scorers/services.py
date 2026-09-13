"""Water, toilets, food and parks along the route (scope 7.5).

`services_along` from scope 7.5, and only that. **`resupply_schedule` is deliberately not
here**: "which services are open at arrival" and "max dry gap in minutes" need opening
hours and an ETA vector, and until the pacing model is wired in, a gap in minutes would be
a gap in metres with a guessed pace stapled to it. Distance is what the layers actually
support, so distance is what this returns.

That is also why this scorer raises **no flags**. Every services threshold scope 8.3 states
is in minutes and belongs to the profile's `water_gap_max_min` / `toilet_gap_max_min`;
converting metres to minutes here would be inventing the very number that is missing. The
measurements — per-segment distance to the next service of each kind, and the largest gap
on the route — are the honest half, and they are exactly what `resupply_schedule` will
divide by pace.

Two details worth stating:

* **The buffer is a real distance from the line**, not the corridor bounding box. A
  fountain 350 m off the route is a 700 m round trip, which is not a refill a runner takes.
* **A missing parks layer degrades one value, not the scorer.** `in_park` becomes unknown,
  confidence drops, and an unchecked coverage entry says so — while water and toilets carry
  on being reported (scope 3.6).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from shapely.geometry import Point

from longrun.core.data.file_store import LayerNotFound
from longrun.core.geo.segments import corridor
from longrun.core.models.coverage import CoverageEntry
from longrun.core.models.geometry import Route, Segment
from longrun.core.models.measurement import ScorerResult, SegmentMeasurement
from longrun.core.scorers._common import ROUTE_SUMMARY_ID, RouteFrame, row_tags
from longrun.core.scorers.base import record_coverage, unavailable
from longrun.core.scorers.crossings import NODES_LAYER

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext

name = "services_along"

#: Categories the plan sheet reports, and the raw amenity kinds that feed each. OSM and
#: Overture disagree about singular and plural, so both are accepted.
SERVICE_CATEGORIES: dict[str, tuple[str, ...]] = {
    "water": ("water", "drinking_water", "water_point", "fountain"),
    "toilet": ("toilet", "toilets"),
    "food": ("food", "cafe", "convenience", "supermarket", "fast_food", "restaurant", "deli"),
}

#: Scope 7.5's `buffer_m`. 200 m off the line is a 400 m round trip: a detour a runner
#: will take for water and would not take for a view.
DEFAULT_BUFFER_M = 200.0

PARKS_LAYER = "parks"

#: Confidence for a segment measured without park boundaries.
PARKS_UNKNOWN_CONFIDENCE = 0.8


def category_of(tags: dict[str, Any]) -> str | None:
    """Which service category a point belongs to, or None if it is not a service."""
    kind = str(tags.get("kind", "")).strip().lower()
    amenity = str(tags.get("amenity", "")).strip().lower()
    for category, kinds in SERVICE_CATEGORIES.items():
        if kind in kinds or amenity in kinds:
            return category
    return None


def all_kinds() -> list[str]:
    """Every raw kind worth asking the store for."""
    return [kind for kinds in SERVICE_CATEGORIES.values() for kind in kinds]


def max_gap_m(positions: list[float], length_m: float) -> float:
    """The longest stretch with nothing ahead of you, start and finish included.

    An empty list is a gap of the whole route, which is the correct answer and not a
    missing one: it is what a route with no water on it looks like.
    """
    marks = [0.0, *sorted(positions), length_m]
    return max(b - a for a, b in zip(marks[:-1], marks[1:], strict=True))


def _next_ahead(positions: list[float], cum_m: float) -> float | None:
    ahead = [position for position in positions if position >= cum_m]
    return min(ahead) - cum_m if ahead else None


def _park_shapes(route: Route, ctx: ScorerContext) -> list[Any] | None:
    """Park polygons meeting the corridor, or None when no park layer exists."""
    try:
        frame = ctx.layers.polygons_intersecting(corridor(route), PARKS_LAYER)
    except LayerNotFound:
        return None
    return [row.geometry for _, row in frame.iterrows() if row.geometry is not None]


def services_along(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
    buffer_m: float = DEFAULT_BUFFER_M,
) -> ScorerResult:
    """Distance to the next water, toilet and food along the route (scope 7.5).

    `etas` is accepted for contract conformance and unused: opening hours are
    `resupply_schedule`'s problem, and nothing measured here changes with the hour.
    """
    frame = RouteFrame(route)
    try:
        points = ctx.layers.points_in_corridor(corridor(route, buffer_m=buffer_m), all_kinds())
    except LayerNotFound:
        return unavailable(name, "no amenities layer: services not established")

    positions: dict[str, list[float]] = {category: [] for category in SERVICE_CATEGORIES}
    for _, row in points.iterrows():
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        category = category_of(row_tags(row))
        if category is None:
            continue
        cum_m, offset_m = frame.locate(geometry.x, geometry.y)
        if offset_m <= buffer_m:
            positions[category].append(cum_m)
    for found in positions.values():
        found.sort()

    parks = _park_shapes(route, ctx)

    result = ScorerResult(name=name)
    for segment in segments:
        values: dict[str, float | int | str | bool | None] = {}
        for category, found in positions.items():
            values[f"dist_to_{category}_m"] = _next_ahead(found, segment.cum_start_m)
        values["in_park"] = None if parks is None else _in_park(route, segment, parks)
        result.measurements.append(
            SegmentMeasurement(
                segment_id=segment.id,
                values=values,
                confidence=PARKS_UNKNOWN_CONFIDENCE if parks is None else 1.0,
            )
        )

    summary: dict[str, float | int | str | bool | None] = {"buffer_m": buffer_m}
    for category, found in positions.items():
        summary[f"{category}_count"] = len(found)
        summary[f"max_{category}_gap_m"] = max_gap_m(found, route.length_m)
    summary["parks_checked"] = parks is not None
    result.measurements.append(SegmentMeasurement(segment_id=ROUTE_SUMMARY_ID, values=summary))

    record_coverage(
        result, source=NODES_LAYER, kind="services", vintage=ctx.layers.vintage(NODES_LAYER)
    )
    if parks is None:
        result.coverage.append(
            CoverageEntry(
                source=PARKS_LAYER,
                kind="park_boundaries",
                checked=False,
                reason="no park layer: park containment not established",
            )
        )
    else:
        record_coverage(
            result,
            source=PARKS_LAYER,
            kind="park_boundaries",
            vintage=ctx.layers.vintage(PARKS_LAYER),
        )
    return result


def _in_park(route: Route, segment: Segment, shapes: list[Any]) -> bool:
    """Whether the segment's midpoint lies inside any park polygon.

    The midpoint is taken as the route point nearest the segment's mid-distance rather
    than by interpolating the WGS84 line: interpolating in degrees would place the point
    by longitude-stretched arithmetic, and the whole question is which polygon it is in.
    """
    target = segment.cum_start_m + segment.length_m / 2.0
    index = min(
        range(segment.start_idx, segment.end_idx + 1),
        key=lambda i: abs(route.points[i].cum_dist_m - target),
    )
    midpoint = Point(route.points[index].lon, route.points[index].lat)
    return any(shape.intersects(midpoint) for shape in shapes)


#: The name the scorer registry loads. Aliased so a caller can dispatch on
#: `module.score` without knowing which scope tool this module implements.
score = services_along
