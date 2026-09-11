"""Street and lane closures overlapping the route (scope 7.6, 7.10).

The first scorer whose data comes from an adapter rather than a layer, and the only one of
M4's three that can fail a route. [ADR 0013](../../../docs/decisions/0013-closures-may-hard-fail.md)
is why: a scorer emits hard flags exactly when §7.9 gives it a check, `check_6` exists and
reads hard flags only, so a flagless `closures` would make check 6 pass vacuously wherever
an adapter answered - which is worse than skipping, because it reports a verified pass that
verified nothing.

**Five gates stand between a work zone and a hard flag**, and each of them is here because
of something in real data. Maricopa County's live feed carries 115 work zones; most are lane
closures on highways no pedestrian is on, and one of them runs from 2024 to 2028. A naive
"any work zone in the corridor" rule would hard-flag almost every Phoenix route, which is
indistinguishable from flagging none.

The gate that does the most work is the second: **along the route, not across it.** You can
get across a work zone on a cross-street; you cannot get through four hundred metres of
closed sidewalk. Bearing agreement is what separates the two, and geometry is the only thing
that knows the difference.

**Coverage is per jurisdiction**, one entry each, all of `kind="closures"`. That is what
lets `check_6` tell a route where every jurisdiction answered from one where two of four
did - and it is the first use of `CoverageEntry.jurisdiction`, which has existed since M1.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from longrun.core.data.jurisdictions import route_jurisdictions, unqualified_reason
from longrun.core.models.coverage import CoverageEntry
from longrun.core.models.measurement import Flag, FlagKind, ScorerResult, SegmentMeasurement, Tier
from longrun.core.scorers._common import ROUTE_SUMMARY_ID, RouteFrame, segment_at

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext
    from longrun.core.models.features import Feature, FeatureKind, FeatureSet
    from longrun.core.models.geometry import Route, Segment

#: Annotated as a `FeatureKind` rather than a bare `str` because it is both the scorer's
#: name and the adapter kind asked for - the two are deliberately the same word, and the
#: annotation is what stops them drifting apart silently.
name: FeatureKind = "closures"

#: ADR 0013 gate 5. Below this tier a closure is real information and not a reason to fail a
#: route: `MAX_EXTRACTION_CONFIDENCE` is 0.5, so a tier-4 extraction provably cannot clear
#: `HARD_FLAG_CONFIDENCE` and provably cannot fail a route. That is the point of both
#: numbers - a verification gate trippable by a model reading a PDF is worse than no gate.
MIN_HARD_FLAG_TIER = 2
HARD_FLAG_CONFIDENCE = 0.8

#: ADR 0013 gate 2. How close a closure has to be to count as being on the route at all.
CLOSURE_BUFFER_M = 20.0

#: ADR 0013 gate 2. How nearly parallel it has to run. A closure crossing the route at right
#: angles is a cross-street, and a runner walks around the end of it.
PARALLEL_BEARING_DEG = 30.0

#: ADR 0013 gate 4. Longer than this and a "closure" is a standing condition - stale, or a
#: permanent reconfiguration OSM has already absorbed - not an event worth rerouting around.
STANDING_CONDITION_DAYS = 180

#: WZDx `vehicle_impact` values, and the categories, that mean the way is actually shut.
IMPASSABLE_IMPACTS = frozenset({"all-lanes-closed", "all-lanes-closed-merge-left"})
IMPASSABLE_CATEGORIES = frozenset({"full-closure", "road-closure", "sidewalk-closed", "closure"})

#: Severity for a closure that does not clear all five gates. Scaled by confidence.
SOFT_SEVERITY = 0.5


def _bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Compass bearing from one (lon, lat) to another, in degrees."""
    lon1, lat1 = math.radians(a[0]), math.radians(a[1])
    lon2, lat2 = math.radians(b[0]), math.radians(b[1])
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.degrees(math.atan2(y, x)) % 360.0


def _bearing_difference(one: float, other: float) -> float:
    """The acute angle between two bearings, treating a line as undirected.

    Undirected on purpose: a closure digitised in the opposite direction to the route is the
    same piece of ground, and a 180-degree difference would otherwise read as perpendicular.
    """
    delta = abs(one - other) % 180.0
    return min(delta, 180.0 - delta)


def _coordinates(geometry: dict[str, Any]) -> list[tuple[float, float]]:
    """Every (lon, lat) in a GeoJSON geometry, flattened. Type-agnostic by design: a feed
    that switches LineString for MultiLineString between releases must not go silent."""
    out: list[tuple[float, float]] = []

    def walk(node: Any) -> None:
        if isinstance(node, (list, tuple)):
            if len(node) >= 2 and all(isinstance(v, (int, float)) for v in node[:2]):
                out.append((float(node[0]), float(node[1])))
                return
            for child in node:
                walk(child)

    walk(geometry.get("coordinates"))
    return out


def runs_along(feature: Feature, frame: RouteFrame, route: Route) -> tuple[bool, float, float]:
    """ADR 0013 gate 2: does this closure lie along the route, or merely meet it?

    Returns `(parallel, nearest_offset_m, cum_dist_m)`. A closure with a single coordinate
    has no bearing, so it is placed but never counted parallel - a point cannot block a
    corridor lengthwise, and treating it as if it could is how a single reported incident
    would shut a route down.
    """
    coords = _coordinates(feature.geometry)
    if not coords:
        return False, math.inf, 0.0

    cum_m, offset_m = frame.locate_path(coords)
    if len(coords) < 2:
        return False, offset_m, cum_m

    # Measured over the part of the closure that is *near* the route where possible. A long
    # feed geometry that touches the route at one end and runs away for a kilometre has an
    # end-to-end bearing that describes the part nobody is running on.
    near_points = [
        (lon, lat) for (lon, lat) in coords if frame.locate(lon, lat)[1] <= CLOSURE_BUFFER_M * 3
    ]
    reference = near_points if len(near_points) >= 2 else coords
    closure_bearing = _bearing(reference[0], reference[-1])
    route_bearing = _route_bearing(route, cum_m)

    parallel = (
        offset_m <= CLOSURE_BUFFER_M
        and _bearing_difference(closure_bearing, route_bearing) <= PARALLEL_BEARING_DEG
    )
    return parallel, offset_m, cum_m


def _route_bearing(route: Route, cum_m: float) -> float:
    """The route's own heading where a closure meets it."""
    points = route.points
    for before, after in zip(points[:-1], points[1:], strict=True):
        if before.cum_dist_m <= cum_m <= after.cum_dist_m:
            return _bearing((before.lon, before.lat), (after.lon, after.lat))
    return _bearing((points[0].lon, points[0].lat), (points[-1].lon, points[-1].lat))


def blocks_pedestrians(feature: Feature) -> bool:
    """ADR 0013 gate 1. `some-lanes-closed` is not a closed footway."""
    category = (feature.category or "").strip().lower()
    if category in IMPASSABLE_CATEGORIES:
        return True
    if category in IMPASSABLE_IMPACTS:
        return True
    # The adapter maps WZDx `vehicle_impact` onto `category`; prose is the fallback for
    # feeds that describe a sidewalk closure without having a field for one.
    detail = (feature.detail or "").lower()
    return "sidewalk clos" in detail or ("pedestrian" in detail and "clos" in detail)


def is_standing_condition(feature: Feature) -> bool:
    """ADR 0013 gate 4. Open-ended windows count as standing: a closure with no end date is
    a statement that nobody knows when it lifts, which is not an event."""
    if feature.start is None or feature.end is None:
        return True
    return (feature.end - feature.start) > timedelta(days=STANDING_CONDITION_DAYS)


def is_hard(feature: Feature, parallel: bool, when: datetime | None) -> bool:
    """All five of ADR 0013's gates, in one place so the rule is readable."""
    return (
        blocks_pedestrians(feature)
        and parallel
        and (when is None or feature.active_at(when))
        and not is_standing_condition(feature)
        and feature.tier <= MIN_HARD_FLAG_TIER
        and feature.confidence >= HARD_FLAG_CONFIDENCE
    )


def closures(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
) -> ScorerResult:
    """Overlaps with active and planned closures, per jurisdiction crossed (scope 7.6)."""
    from longrun.core.geo.segments import corridor, corridor_polygon

    result = ScorerResult(name=name)
    scan = route_jurisdictions(route, ctx)

    # A park agency with no resolvable state is a jurisdiction this plan cannot safely match
    # an adapter to. Reported on every run rather than only when nothing resolved: the route
    # around it is fully covered, which is exactly what would make the gap invisible.
    unqualified = unqualified_reason(scan)
    if unqualified:
        result.coverage.append(
            CoverageEntry(source=name, kind=name, checked=False, reason=unqualified)
        )

    if not scan.answered:
        for reason in scan.reasons or ["jurisdictions could not be resolved"]:
            result.coverage.append(
                CoverageEntry(source=name, kind=name, checked=False, reason=reason)
            )
        return _summarise(result, segments, {}, jurisdictions=0)

    # Street closures are published by *DOTs*, which register against census boundaries. A
    # route that resolved its park agencies and not its boundaries has therefore established
    # almost nothing about closures, and must say so rather than let five park entries imply
    # the question was covered. This is the partial-resolution case, and it is the ordinary
    # one for any fixture frozen before M4 added `boundaries` to the frozen layer set.
    if not scan.boundaries_checked:
        result.coverage.append(
            CoverageEntry(
                source=name,
                kind=name,
                checked=False,
                reason=next(
                    (r for r in scan.reasons if "boundaries" in r),
                    "boundaries unavailable: state and county adapters could not be looked up",
                ),
            )
        )

    if ctx.features is None:
        result.coverage.append(
            CoverageEntry(
                source=name,
                kind=name,
                checked=False,
                reason="no adapter registry configured for this plan",
            )
        )
        return _summarise(result, segments, {}, jurisdictions=len(scan.jurisdictions))

    day = ctx.clock.now().date()
    found: FeatureSet = ctx.features.fetch(
        name, scan.jurisdictions, corridor_polygon(corridor(route)), day
    )
    for answer in found.answers:
        result.coverage.append(answer.coverage(name))

    frame = RouteFrame(route)
    counts: dict[str, dict[str, int]] = {}

    for feature in found.features:
        parallel, offset_m, cum_m = runs_along(feature, frame, route)
        if offset_m > CLOSURE_BUFFER_M * 3:
            continue  # not on this route at all; the adapter answered about a polygon
        segment = segment_at(segments, cum_m)
        segment_id = segment.id if segment is not None else ROUTE_SUMMARY_ID
        when = _eta_for(segment, segments, etas)
        hard = is_hard(feature, parallel, when)

        counts.setdefault(segment_id, {})
        code = "closed_to_pedestrians" if hard else "closure_nearby"
        counts[segment_id][code] = counts[segment_id].get(code, 0) + 1
        result.flags.append(_flag(segment_id, feature, hard, offset_m))

    return _summarise(result, segments, counts, jurisdictions=len(scan.jurisdictions))


def _eta_for(
    segment: Segment | None, segments: list[Segment], etas: list[datetime] | None
) -> datetime | None:
    """ADR 0013 gate 3: the *segment's* arrival time, not the route's date.

    A closure lifting at noon does not block a runner who reaches it at 14:00, and a route
    scored by its start date alone cannot tell the difference.
    """
    if not etas or segment is None:
        return None
    try:
        index = segments.index(segment)
    except ValueError:  # pragma: no cover - defensive
        return None
    return etas[min(index, len(etas) - 1)]


def _flag(segment_id: str, feature: Feature, hard: bool, offset_m: float) -> Flag:
    detail = feature.detail or feature.category or "closure"
    where = f"{offset_m:.0f} m from the route"
    if hard:
        return Flag(
            scorer=name,
            segment_id=segment_id,
            kind=FlagKind.HARD,
            tier=Tier.SAFETY,
            severity=1.0,
            reason_code="closed_to_pedestrians",
            detail=f"{detail} ({where}, tier {feature.tier})",
        )
    return Flag(
        scorer=name,
        segment_id=segment_id,
        kind=FlagKind.SOFT,
        tier=Tier.COMFORT,
        # Confidence scales severity, which is the only channel it has: `arbitrate` reads
        # neither `Flag.confidence` (there is none) nor the coverage entry.
        severity=round(SOFT_SEVERITY * feature.confidence, 3),
        reason_code="closure_nearby",
        detail=f"{detail} ({where}, tier {feature.tier})",
    )


def _summarise(
    result: ScorerResult,
    segments: list[Segment],
    counts: dict[str, dict[str, int]],
    jurisdictions: int,
) -> ScorerResult:
    total = 0
    for segment in segments:
        found = counts.get(segment.id, {})
        total += sum(found.values())
        result.measurements.append(
            SegmentMeasurement(
                segment_id=segment.id,
                values={"closures": sum(found.values()), **found},
                confidence=1.0,
            )
        )
    answered = sum(1 for c in result.coverage if c.checked)
    result.measurements.append(
        SegmentMeasurement(
            segment_id=ROUTE_SUMMARY_ID,
            values={
                "closures": total,
                "jurisdictions_crossed": jurisdictions,
                "jurisdictions_answered": answered,
                "jurisdictions_unanswered": sum(1 for c in result.coverage if not c.checked),
            },
            confidence=1.0,
        )
    )
    return result


score = closures

__all__ = [
    "CLOSURE_BUFFER_M",
    "HARD_FLAG_CONFIDENCE",
    "MIN_HARD_FLAG_TIER",
    "PARALLEL_BEARING_DEG",
    "STANDING_CONDITION_DAYS",
    "blocks_pedestrians",
    "closures",
    "is_hard",
    "is_standing_condition",
    "name",
    "runs_along",
    "score",
]
