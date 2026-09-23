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

## The other half: tide conflicts (ADR 0042)

Scope 7.6 signs this tool *"park gate and dawn-to-dusk violations at ETA; **tide conflicts
for beach segments**"*, and the second clause is a different question wearing the same name.

**None of the paragraph above transfers, and the tier is decided separately.** A gate is
bought back by starting twenty minutes later; the tide is not bought back by starting at
all. On an out-and-back the same stretch is crossed twice, separated by however long the
run takes, and a semidiurnal tide is ~12 h 25 min periodic — so for a route whose two
crossings straddle high water there is **no start time** that clears both, and
`start_time_optimizer`'s sweep cannot manufacture a second low water. The failure mode
differs too: you turn round at a locked gate, and a causeway that floods behind you cuts
off the way back. That is what `Tier.SAFETY` claims — incommensurability, not magnitude —
so a conflict on a way a mapper tagged `tidal=yes` is **soft at SAFETY**, the first flag in
this codebase that is one without the other. A `natural=beach` way is an inference rather
than a statement and stays at COMFORT. ADR 0042 is the argument in full; ADR 0013's rule
that a scorer emits HARD flags only where §7.9 gives it a check is why neither is hard.

**The feature seam does not fit and is not used.** `_place` drops anything past
`GATE_REACH_M = 200 m`, which is right for a gate and meaningless for a tide station tens
of kilometres away. Nor is a station a *feature*: it governs a **stretch** of ground, not a
point beside the route. So the tide path reads the `ways` layer directly through
`_coastal.coastal_stretches`, places its flag with `segment_at` on the segment the stretch
begins at — one flag per stretch, following `surface_profile`'s rule that a 2 km run must
not outweigh a worse 300 m one just by covering more segments — and lets
`tides.STATION_REACH_M` decide, with its own number and its own reason, whether any gauge
governs the stretch at all.

**A route with no tidal stretch reports nothing, and that is deliberate.** No flag, no
measurement key, no coverage row. "No key" and "zero" read differently — `expectation.py`
already says so for waypoints — so `tidal_stretches` is absent on an inland route and
present as `2` with `tide_conflicts: 0` on a coastal one that was checked and found benign.
That is the difference between *not asked*, *asked and unanswerable* (a `checked=False`
`noaa_coops` row) and *measured as none*. It also means adding this moved no golden: none
of the seven crosses tidal ground, and none of their `ways` fixtures carries the tags.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from longrun.core.data.file_store import LayerNotFound
from longrun.core.data.jurisdictions import (
    JurisdictionScan,
    route_jurisdictions,
    unqualified_reason,
)
from longrun.core.data.tides import station_tides
from longrun.core.models.coverage import CoverageEntry
from longrun.core.models.geometry import LatLon
from longrun.core.models.measurement import Flag, FlagKind, ScorerResult, SegmentMeasurement, Tier
from longrun.core.models.waypoint import PlanWaypoint
from longrun.core.scorers._coastal import CoastalStretch, coastal_stretches
from longrun.core.scorers._common import (
    ROUTE_SUMMARY_ID,
    WAYS_LAYER,
    RouteFrame,
    segment_at,
    way_tags_in_corridor,
)

if TYPE_CHECKING:  # pragma: no cover
    from datetime import datetime

    from longrun.core.data.tides import StationTides, TideExtreme
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

#: Severity for arriving on tidal ground inside the high-water window. Above
#: `CLOSED_SEVERITY` because a shut gate is a detour and a flooded causeway is not, and
#: scaled by the stretch's evidence - `TIDAL_CONFIDENCE` 0.9 or `BEACH_CONFIDENCE` 0.5 -
#: exactly as ADR 0013 has adapter confidence scale a soft closure.
TIDE_CONFLICT_SEVERITY = 0.95

#: Severity when the ground is tidal and the tide could not be established. Low, because the
#: claim is only that nobody checked; the *coverage* entry is what carries that fact, and a
#: flag this size exists so the stretch is visible in `residual_flags` rather than to argue.
TIDE_UNKNOWN_SEVERITY = 0.35

#: Arbitration tier per class of coastal evidence (ADR 0042). `tidal=yes` is a categorical
#: statement that the water covers the way, and nothing below SAFETY buys that back. A beach
#: is a statement about the ground with the water inferred, so it sits where the gate does.
TIDE_TIER: dict[str, Tier] = {"tidal": Tier.SAFETY, "beach": Tier.COMFORT}


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
    """Gate violations and tide conflicts at each segment's ETA (scope 7.6).

    Two independent questions sharing one tool because scope 7.6 signs it that way. They
    are asked in that order and neither gates the other: a route may cross tidal ground
    with no park on it, and a park with no coast, and the plan sheet has to be able to
    report either alone. The gate half runs first so that its coverage entries keep the
    positions seven golden routes already pin.
    """
    result = ScorerResult(name=name)
    counts: dict[str, dict[str, int]] = {}

    agencies, shift = _gates(route, segments, ctx, etas, result, counts)
    tide = _tides(route, segments, ctx, etas, result, counts)

    return _summarise(result, segments, counts, agencies=agencies, shift=shift, tide=tide)


def _gates(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None,
    result: ScorerResult,
    counts: dict[str, dict[str, int]],
) -> tuple[int, float | None]:
    """Park gates at arrival: the M4 scorer, moved out whole.

    Returns the agency count and the proposed start shift, which is everything
    `_summarise` needs from it. Extracted when the tide half arrived, because its four
    early returns each used to call `_summarise` — so a second question could not be asked
    after any of them without being skipped on exactly the routes that took them.
    """
    from longrun.core.geo.segments import corridor, corridor_polygon

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
        return 0, None

    if not agencies:
        result.coverage.append(
            CoverageEntry(source=name, kind=name, checked=True, reason=_no_agency_reason(scan))
        )
        return 0, None

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
        return len(agencies), None

    found = ctx.features.fetch(
        name, agencies, corridor_polygon(corridor(route)), ctx.clock.now().date()
    )
    for answer in found.answers:
        result.coverage.append(answer.coverage(name))

    frame = RouteFrame(route)
    shifts: list[float] = []

    for feature in found.features:
        placed = _place(feature, frame)
        if placed is None:
            continue
        cum_m, offset_m, at = placed
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
        # Only where it flagged. A gate that is open when the runner reaches it is not a
        # waypoint - it is the ordinary case, and a marker on every open park gate would
        # bury the one that is shut.
        if at is not None:
            result.waypoints.append(
                PlanWaypoint(
                    position=at,
                    kind="gate",
                    label=str(feature.detail or feature.category or "gate"),
                    cum_dist_m=cum_m,
                    scorer=name,
                    offset_m=round(offset_m, 1),
                    eta=arrival,
                    detail=code,
                )
            )

    return len(agencies), (max(shifts) if shifts else None)


@dataclass
class _TideScan:
    """What the tide half found, for `_summarise` to report as route totals.

    A dataclass rather than four loose returns because every field is only meaningful
    beside the others: `conflicts: 0` says nothing until `stretches` says whether anything
    was asked, and `unchecked` is what separates "no conflict" from "nobody could tell".
    """

    stretches: int = 0
    conflicts: int = 0
    unchecked: int = 0
    #: Minutes to the next predicted low water at the worst conflict, or None.
    wait_min: float | None = None


def _tides(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None,
    result: ScorerResult,
    counts: dict[str, dict[str, int]],
) -> _TideScan:
    """Tide conflicts on the stretches of this route the tide governs (scope 7.6).

    **Silent on a route with no tidal stretch**, which is three quarters of why this is
    safe to add: no flag, no measurement key, no coverage row, so no existing plan changes.
    The module docstring argues that "absent" and "zero" are the right pair of answers here.

    The fourth quarter is that a `ways` layer that cannot be read is a *fourth* answer and
    does get a row: on that route nobody established whether the tide matters, and silence
    would be indistinguishable from an inland route.
    """
    try:
        by_way = way_tags_in_corridor(route, ctx)
    except (LayerNotFound, FileNotFoundError) as exc:
        result.coverage.append(
            CoverageEntry(
                source=WAYS_LAYER,
                kind="tidal_ground",
                checked=False,
                reason=f"whether this route crosses tidal ground is not established: {exc}",
            )
        )
        return _TideScan()

    stretches = coastal_stretches(segments, by_way)
    if not stretches:
        return _TideScan()

    by_id = {segment.id: segment for segment in segments}
    scan = _TideScan(stretches=len(stretches))
    day = ctx.clock.now().date()

    for stretch in stretches:
        where = f"{stretch.cum_start_m / 1000:.1f}-{stretch.cum_end_m / 1000:.1f} km"
        anchor = route.points[min(stretch.mid_route_index, len(route.points) - 1)]
        tides = station_tides(ctx, anchor.lat, anchor.lon, day)
        result.coverage.append(tides.coverage("tidal_ground", where=f"{stretch.kind} {where}"))

        first = by_id.get(stretch.segment_ids[0])
        segment_id = first.id if first is not None else ROUTE_SUMMARY_ID
        conflict = _stretch_conflict(stretch, by_id, segments, etas, tides)

        if not tides.answered:
            scan.unchecked += 1
            code, severity = "tide_unknown", TIDE_UNKNOWN_SEVERITY
            arrival, detail = _eta_for(first, segments, etas), tides.reason or "no tide prediction"
        elif conflict is None:
            continue  # checked, and the water is not up when the runner is there
        else:
            scan.conflicts += 1
            arrival, extreme = conflict
            code, severity = "tide_conflict_at_eta", TIDE_CONFLICT_SEVERITY
            wait = tides.minutes_to_next_low(arrival)
            if wait is not None and (scan.wait_min is None or wait > scan.wait_min):
                scan.wait_min = wait
            height = "" if extreme.height_m is None else f" at {extreme.height_m:.2f} m MLLW"
            detail = f"high water {extreme.time:%H:%M}{height}" + (
                f", {wait:.0f} min to the next low" if wait is not None else ""
            )

        counts.setdefault(segment_id, {})
        counts[segment_id][code] = counts[segment_id].get(code, 0) + 1
        result.flags.append(
            Flag(
                scorer=name,
                segment_id=segment_id,
                kind=FlagKind.SOFT,
                tier=TIDE_TIER[stretch.kind],
                severity=round(severity * stretch.confidence, 3),
                reason_code=code,
                # One flag per stretch, on the segment it begins at. `surface_profile`'s
                # rule and its reason: a flag per segment would let a 2 km beach outweigh a
                # worse 300 m causeway purely by covering more of them.
                detail=f"{stretch.length_m / 1000:.2f} km of {stretch.kind} ground, {detail}",
            )
        )
        if first is not None:
            point = route.points[min(first.start_idx, len(route.points) - 1)]
            result.waypoints.append(
                PlanWaypoint(
                    position=LatLon(lat=point.lat, lon=point.lon),
                    # `hazard`, not a ninth `WaypointKind`. Scope 9's own list names
                    # hazards, a stretch under water is one, and widening a closed Literal
                    # would reach both course writers for a symbol they already have.
                    kind="hazard",
                    label=f"{stretch.kind} ground, {where}",
                    cum_dist_m=stretch.cum_start_m,
                    scorer=name,
                    offset_m=0.0,
                    eta=arrival,
                    detail=code,
                )
            )

    return scan


def _stretch_conflict(
    stretch: CoastalStretch,
    by_id: dict[str, Segment],
    segments: list[Segment],
    etas: list[datetime] | None,
    tides: StationTides,
) -> tuple[datetime, TideExtreme] | None:
    """The first arrival on this stretch that falls inside a high-water window.

    **Both ends are tested, not just the entry.** A 3 km beach takes twenty minutes and the
    water rises while the runner is on it, so entering at the edge of the window and leaving
    inside it is a conflict that testing the entry alone would miss. The *other* direction
    of an out-and-back needs no special handling: it is a second, non-contiguous run of
    segments, so `coastal_stretches` already returns it as its own stretch with its own ETA.
    """
    ends = (stretch.segment_ids[0], stretch.segment_ids[-1])
    for segment_id in dict.fromkeys(ends):
        arrival = _eta_for(by_id.get(segment_id), segments, etas)
        if arrival is None:
            continue
        extreme = tides.high_water_conflict(arrival)
        if extreme is not None:
            return arrival, extreme
    return None


def _place(feature: Feature, frame: RouteFrame) -> tuple[float, float, LatLon | None] | None:
    """Where a gate sits along the route, how far off it is, and where it actually is.

    The third element used to be computed and dropped. A gate is a place a runner arrives at
    and may not get through, which is exactly what belongs on a course.
    """
    from longrun.core.scorers.closures import _coordinates

    coords = _coordinates(feature.geometry)
    if not coords:
        return 0.0, 0.0, None
    cum_m, offset_m = frame.locate_path(coords)
    if offset_m > GATE_REACH_M:
        return None
    lon, lat = coords[0]
    return cum_m, offset_m, LatLon(lat=lat, lon=lon)


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
    tide: _TideScan | None = None,
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
    if tide is not None and tide.stretches:
        # Present only on a route that has tidal ground, so an inland plan has no key rather
        # than a zero - the distinction `expectation.py` already draws for waypoint kinds,
        # and the reason adding this half moved no golden. Once a stretch exists all three
        # are written, including the zeroes: `tide_conflicts: 0` beside
        # `tide_stretches_unchecked: 0` is the "measured as none" answer, and it must be
        # tellable from the stretch nobody could check.
        values["tidal_stretches"] = tide.stretches
        values["tide_conflicts"] = tide.conflicts
        values["tide_stretches_unchecked"] = tide.unchecked
        if tide.wait_min is not None:
            # The tide's answer to `earliest_feasible_start_shift_min`, and deliberately not
            # the same number. A gate is solved by starting earlier; a high water is solved
            # by waiting at the edge of it, which is a different instruction to a runner and
            # a different input to `start_time_optimizer` (ADR 0042).
            values["tide_wait_to_low_water_min"] = tide.wait_min
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
    "TIDE_CONFLICT_SEVERITY",
    "TIDE_TIER",
    "TIDE_UNKNOWN_SEVERITY",
    "UNKNOWN_TIME_SEVERITY",
    "access_hours",
    "name",
    "score",
    "shift_to_open",
]
