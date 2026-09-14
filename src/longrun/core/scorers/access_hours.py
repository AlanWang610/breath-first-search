"""Park gates and dawn-to-dusk limits, tested at arrival (scope 7.6, 7.10).

The only one of M4's three scorers whose whole subject is *time*. A park gate is not a
property of the ground; it is a property of the ground at 05:40 on a Tuesday, and a scorer
that tested the route's date rather than each segment's ETA would report a locked gate for a
runner who arrives two hours after it opens.

**Soft, at severity 0.9, and a locked gate really is disqualifying** - which looks like a
contradiction and is the most considered decision in
[ADR 0013](../../../docs/decisions/0013-closures-may-hard-fail.md). An arbitration tier is
not a severity ranking; it is a claim of *incommensurability*, and `Tier.SAFETY` means no
amount of everything below can buy it back. A gate is bought back by starting twenty minutes
later. Scope 6.4 already files *"earliest start (e.g. gate opens)"* as a **request
constraint**, and 8.1 step 7's `start_time_optimizer` is the machinery for exactly this. So
this scorer surfaces a proposed start shift and lets the clock solve it.

It deliberately does **not** write that shift into `PlanRequest.time_constraints`. Check 9
tests the constraints the *user* stated, and a scorer that added its own would make check 9
start failing on a requirement nobody asked for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from longrun.core.data.jurisdictions import (
    JurisdictionScan,
    route_jurisdictions,
    unqualified_reason,
)
from longrun.core.models.coverage import CoverageEntry
from longrun.core.models.measurement import Flag, FlagKind, ScorerResult, SegmentMeasurement, Tier
from longrun.core.scorers._common import ROUTE_SUMMARY_ID, RouteFrame, segment_at

if TYPE_CHECKING:  # pragma: no cover
    from datetime import datetime

    from longrun.core.models.context import ScorerContext
    from longrun.core.models.features import Feature, FeatureKind
    from longrun.core.models.geometry import Route, Segment

name: FeatureKind = "access_hours"

#: How far off the route a gate still governs. A gate is a point on a way; the way is what
#: matters, and 200 m is generous enough to catch one placed at a car park entrance.
GATE_REACH_M = 200.0

#: Severity for arriving at a closed gate. High enough to top the comfort tier and always
#: survive into `residual_flags`, which is what makes it visible without making it a reroute
#: demand - see the module docstring.
CLOSED_SEVERITY = 0.9

#: Severity when a gate exists and the arrival time cannot be established. Lower, because
#: the claim is weaker: a gate whose hours nobody could check is a thing to know about.
UNKNOWN_TIME_SEVERITY = 0.3


def shift_to_open(feature: Feature, arrival: datetime) -> float | None:
    """Minutes a start would have to move for this gate to be open on arrival.

    `None` when the gate's window does not say - an open-ended `start` with no `end` is a
    gate that has opened and not yet shut, which needs no shift at all.
    """
    if feature.start is None or feature.active_at(arrival):
        return None
    if arrival < feature.start:
        return round((feature.start - arrival).total_seconds() / 60.0, 1)
    return None


def access_hours(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
) -> ScorerResult:
    """Gate and dawn-to-dusk violations at each segment's ETA (scope 7.6)."""
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
    agencies = [j for j in scan.jurisdictions if j.source == "padus"]

    if not scan.parks_checked:
        result.coverage.append(
            CoverageEntry(
                source=name,
                kind=name,
                checked=False,
                reason=next(
                    (r for r in scan.reasons if "parks" in r),
                    "no parks layer: gated land not established",
                ),
            )
        )
        return _summarise(result, segments, {}, agencies=0, shift=None)

    if not agencies:
        result.coverage.append(
            CoverageEntry(source=name, kind=name, checked=True, reason=_no_agency_reason(scan))
        )
        return _summarise(result, segments, {}, agencies=0, shift=None)

    if ctx.features is None:
        for agency in agencies:
            result.coverage.append(
                CoverageEntry(
                    source=name,
                    kind=name,
                    checked=False,
                    jurisdiction=f"{agency.name} ({agency.id})",
                    reason="no adapter registry configured for this plan",
                )
            )
        return _summarise(result, segments, {}, agencies=len(agencies), shift=None)

    found = ctx.features.fetch(
        name, agencies, corridor_polygon(corridor(route)), ctx.clock.now().date()
    )
    for answer in found.answers:
        result.coverage.append(answer.coverage(name))

    frame = RouteFrame(route)
    counts: dict[str, dict[str, int]] = {}
    shifts: list[float] = []

    for feature in found.features:
        placed = _place(feature, frame)
        if placed is None:
            continue
        cum_m, offset_m = placed
        segment = segment_at(segments, cum_m)
        segment_id = segment.id if segment is not None else ROUTE_SUMMARY_ID
        arrival = _eta_for(segment, segments, etas)

        if arrival is None:
            code, severity = "gate_hours_unknown", UNKNOWN_TIME_SEVERITY
        elif feature.active_at(arrival):
            continue  # open when the runner gets there, which is the ordinary case
        else:
            code, severity = "gate_closed_at_eta", CLOSED_SEVERITY
            shift = shift_to_open(feature, arrival)
            if shift is not None:
                shifts.append(shift)

        counts.setdefault(segment_id, {})
        counts[segment_id][code] = counts[segment_id].get(code, 0) + 1
        result.flags.append(
            Flag(
                scorer=name,
                segment_id=segment_id,
                kind=FlagKind.SOFT,
                tier=Tier.COMFORT,
                severity=round(severity * feature.confidence, 3),
                reason_code=code,
                detail=(
                    f"{feature.detail or feature.category or 'gate'} "
                    f"({offset_m:.0f} m from the route, tier {feature.tier})"
                ),
            )
        )

    return _summarise(
        result, segments, counts, agencies=len(agencies), shift=max(shifts) if shifts else None
    )


def _place(feature: Feature, frame: RouteFrame) -> tuple[float, float] | None:
    from longrun.core.scorers.closures import _coordinates

    coords = _coordinates(feature.geometry)
    if not coords:
        return 0.0, 0.0
    cum_m, offset_m = frame.locate_path(coords)
    return (cum_m, offset_m) if offset_m <= GATE_REACH_M else None


def _eta_for(
    segment: Segment | None, segments: list[Segment], etas: list[datetime] | None
) -> datetime | None:
    if not etas or segment is None:
        return None
    try:
        index = segments.index(segment)
    except ValueError:  # pragma: no cover - defensive
        return None
    return etas[min(index, len(etas) - 1)]


def _summarise(
    result: ScorerResult,
    segments: list[Segment],
    counts: dict[str, dict[str, int]],
    agencies: int,
    shift: float | None,
) -> ScorerResult:
    total = 0
    for segment in segments:
        found = counts.get(segment.id, {})
        total += sum(found.values())
        result.measurements.append(
            SegmentMeasurement(
                segment_id=segment.id,
                values={"access_violations": sum(found.values()), **found},
                confidence=1.0,
            )
        )
    values: dict[str, float | int | str | bool | None] = {
        "access_violations": total,
        "agencies_crossed": agencies,
        # Only entries naming an agency count as one answering. The "no managing
        # agency on this route" entry is `checked=True` - the parks layer *was* read -
        # but counting it here produced "1 answered of 0 crossed", which is nonsense a
        # reader would have to decode.
        "agencies_answered": sum(1 for c in result.coverage if c.checked and c.jurisdiction),
    }
    if shift is not None:
        # What `start_time_optimizer` reads, and what the plan sheet reports instead of a
        # reroute demand. Never written into the request's time constraints - see the module
        # docstring: check 9 tests what the *user* asked for.
        values["earliest_feasible_start_shift_min"] = shift
    result.measurements.append(
        SegmentMeasurement(segment_id=ROUTE_SUMMARY_ID, values=values, confidence=1.0)
    )
    return result


def _no_agency_reason(scan: JurisdictionScan) -> str:
    """Why no agency was asked, distinguishing two different facts.

    A route over no managed land at all is an all-clear. A route over parks whose manager
    this data cannot name is not - it is a gap, and scope 12's "unknown, not absent" applies
    to the owner of a park exactly as it does to a missing OSM tag.
    """
    if scan.unattributed_parks:
        return (
            f"{scan.unattributed_parks} park polygon(s) on this route name no managing "
            f"agency, so there was nobody to ask"
        )
    return "no gated land on this route"


score = access_hours

__all__ = [
    "CLOSED_SEVERITY",
    "GATE_REACH_M",
    "UNKNOWN_TIME_SEVERITY",
    "access_hours",
    "name",
    "score",
    "shift_to_open",
]
