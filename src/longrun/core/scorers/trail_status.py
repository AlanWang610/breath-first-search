"""Park alerts and seasonal closures on the ground the route crosses (scope 7.6, 7.10).

Jurisdictions here are **agencies, not boundaries**. §7.6 says so directly - *"Managing
agency resolved from PAD-US -> NPS Alerts API, USFS/BLM alert pages, state park systems"* -
and it is why `padus.units.agency` was loaded in M3, months before anything read it.

**Soft flags only, and the reason is that a park alert is prose.** "Muddy in places",
"bridge out at mile 4", "mountain lion activity" - sorting the disqualifying from the merely
wet is exactly the act scope 3.2 forbids a scorer: attaching a sign to a measurement.
[ADR 0013](../../../docs/decisions/0013-closures-may-hard-fail.md) settles it, and gives the
escape hatch a structural shape rather than a textual one: an agency adapter that sees a
*structured* closure emits `Feature(kind="closures")` instead, so there is one hard-flag
owner, one check, and no classifier trying to read English.

Weighted at `MAX_WEIGHT` rather than never-weighted, which is the other half of ADR 0013's
split. A closed road is categorical - equally closed at kilometre 2 and kilometre 80. Mud is
a condition you endure, and it compounds with fatigue exactly as `surface_profile` does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from longrun.core.data.jurisdictions import JurisdictionScan, route_jurisdictions
from longrun.core.models.coverage import CoverageEntry
from longrun.core.models.measurement import Flag, FlagKind, ScorerResult, SegmentMeasurement, Tier
from longrun.core.scorers._common import ROUTE_SUMMARY_ID, RouteFrame, segment_at

if TYPE_CHECKING:  # pragma: no cover
    from datetime import datetime

    from longrun.core.models.context import ScorerContext
    from longrun.core.models.features import Feature, FeatureKind
    from longrun.core.models.geometry import Route, Segment

name: FeatureKind = "trail_status"

#: How far off the route an alert still counts. Wider than `closures`' 20 m because a park
#: alert is about an *area* - the agency is telling you about the park, not about a line.
ALERT_REACH_M = 500.0

#: Base severity before confidence scaling. Deliberately modest: this is information, and
#: the runner decides what it is worth.
ALERT_SEVERITY = 0.4

#: Categories an agency uses for something more than an advisory. Still soft - see the
#: module docstring - but worth more severity than a general notice.
SERIOUS_CATEGORIES = frozenset({"closure", "danger", "hazard", "caution"})
SERIOUS_SEVERITY = 0.6


def severity_for(feature: Feature) -> float:
    """Severity for one alert, scaled by the confidence the adapter reported.

    Confidence is the only channel that bites - `arbitrate` reads no confidence field, so a
    tier-4 extraction and a tier-1 feed would otherwise rank identically.
    """
    base = (
        SERIOUS_SEVERITY
        if (feature.category or "").strip().lower() in SERIOUS_CATEGORIES
        else ALERT_SEVERITY
    )
    return round(base * feature.confidence, 3)


def trail_status(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
) -> ScorerResult:
    """Park alerts and seasonal closures for the agencies managing this route's ground."""
    from longrun.core.geo.segments import corridor, corridor_polygon

    result = ScorerResult(name=name)
    scan = route_jurisdictions(route, ctx)
    agencies = [j for j in scan.jurisdictions if j.source == "padus"]

    if not scan.parks_checked:
        for reason in scan.reasons or ["no parks layer: managing agencies not established"]:
            if "parks" in reason or not scan.reasons:
                result.coverage.append(
                    CoverageEntry(source=name, kind=name, checked=False, reason=reason)
                )
        return _summarise(result, segments, {}, agencies=0)

    if not agencies:
        # A real answer, and a common one: a street route crosses no managed land at all.
        result.coverage.append(
            CoverageEntry(
                source=name,
                kind=name,
                checked=True,
                reason=_no_agency_reason(scan),
            )
        )
        return _summarise(result, segments, {}, agencies=0)

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
        return _summarise(result, segments, {}, agencies=len(agencies))

    found = ctx.features.fetch(
        name, agencies, corridor_polygon(corridor(route)), ctx.clock.now().date()
    )
    for answer in found.answers:
        result.coverage.append(answer.coverage(name))

    frame = RouteFrame(route)
    counts: dict[str, dict[str, int]] = {}

    for feature in found.features:
        placed = _place(feature, frame)
        if placed is None:
            continue
        cum_m, offset_m = placed
        segment = segment_at(segments, cum_m)
        segment_id = segment.id if segment is not None else ROUTE_SUMMARY_ID
        when = etas[min(segments.index(segment), len(etas) - 1)] if etas and segment else None
        if when is not None and not feature.active_at(when):
            continue
        code = (feature.category or "alert").strip().lower().replace(" ", "_")
        counts.setdefault(segment_id, {})
        counts[segment_id][code] = counts[segment_id].get(code, 0) + 1
        result.flags.append(
            Flag(
                scorer=name,
                segment_id=segment_id,
                kind=FlagKind.SOFT,
                tier=Tier.COMFORT,
                severity=severity_for(feature),
                reason_code="trail_alert",
                detail=(
                    f"{feature.detail or feature.category or 'alert'} "
                    f"({offset_m:.0f} m from the route, tier {feature.tier})"
                ),
            )
        )

    return _summarise(result, segments, counts, agencies=len(agencies))


def _place(feature: Feature, frame: RouteFrame) -> tuple[float, float] | None:
    """Where an alert meets the route, or `None` if it does not come near it."""
    from longrun.core.scorers.closures import _coordinates

    coords = _coordinates(feature.geometry)
    if not coords:
        # An alert with no geometry is about the whole park, which is where the route is.
        return 0.0, 0.0
    cum_m, offset_m = frame.locate_path(coords)
    return (cum_m, offset_m) if offset_m <= ALERT_REACH_M else None


def _summarise(
    result: ScorerResult,
    segments: list[Segment],
    counts: dict[str, dict[str, int]],
    agencies: int,
) -> ScorerResult:
    total = 0
    for segment in segments:
        found = counts.get(segment.id, {})
        total += sum(found.values())
        result.measurements.append(
            SegmentMeasurement(
                segment_id=segment.id,
                values={"trail_alerts": sum(found.values()), **found},
                confidence=1.0,
            )
        )
    result.measurements.append(
        SegmentMeasurement(
            segment_id=ROUTE_SUMMARY_ID,
            values={
                "trail_alerts": total,
                "agencies_crossed": agencies,
                # Only entries naming an agency count as one answering. The "no managing
                # agency on this route" entry is `checked=True` - the parks layer *was* read -
                # but counting it here produced "1 answered of 0 crossed", which is nonsense a
                # reader would have to decode.
                "agencies_answered": sum(
                    1 for c in result.coverage if c.checked and c.jurisdiction
                ),
            },
            confidence=1.0,
        )
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
    return "no managed land on this route"


score = trail_status

__all__ = [
    "ALERT_REACH_M",
    "ALERT_SEVERITY",
    "SERIOUS_CATEGORIES",
    "name",
    "score",
    "severity_for",
    "trail_status",
]
