"""Where the phone stops working (scope 7.7).

The measurement is simple — which stretches of the route fall outside every carrier's
mapped coverage, and how long the worst one is — and its value is entirely in the
*reporting*, because the data behind it is both patchy and optimistic.

**FCC Broadband Data Collection is carrier self-report.** A provider files the area it
believes it serves; nobody drives it. So a route inside a coverage polygon has not been
shown to have signal, and a plan that said "covered" would be repeating a claim rather
than making one. Every measurement here carries `SELF_REPORT_CONFIDENCE`, and the coverage
manifest says whose claim it is. A *gap* is the finding worth trusting: a carrier has no
incentive to under-report.

**The layer is not loaded, and this scorer says so rather than pretending otherwise.**
`broadbandmap.fcc.gov`'s bulk endpoints are behind an account, and there is no key in
`.env.example` because nobody has one — so `cell_coverage` reports `unavailable` on every
route today, with the reason naming the blocker. That is the same answer scope 3.6 wants
for any absent source, and it is why the scorer exists now: the plumbing, the layer name
and the thresholds are settled and tested, and the day a region build loads
`fcc.cell_coverage` it starts answering with no further code.

Soft flags in COMFORT (ADR 0011). A dead zone is a logistics fact, and one hill without
signal must not outrank the heat findings for the whole route.
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
from longrun.core.scorers._common import ROUTE_SUMMARY_ID
from longrun.core.scorers.base import record_coverage, unavailable

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext

name = "cell_coverage"

COVERAGE_LAYER = "cell_coverage"

#: How long a stretch with no mapped signal has to be before it is worth a flag.
#:
#: Three kilometres is roughly twenty minutes at a long-run pace — long enough that a
#: runner who falls at the start of it is out of contact for a meaningful time, short
#: enough not to fire on every railway cutting.
GAP_FLAG_M = 3000.0

#: Coverage is carrier self-report, not measurement. A route inside a filed polygon has
#: not been shown to have signal.
SELF_REPORT_CONFIDENCE = 0.6

SEVERITY_BY_CODE: dict[str, float] = {"no_signal_gap": 0.5}


def _covered(point: Any, polygons: list[Any]) -> bool:
    return any(polygon is not None and polygon.covers(point) for polygon in polygons)


def cell_coverage(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
) -> ScorerResult:
    """Stretches of the route outside every carrier's filed coverage (scope 7.7)."""
    try:
        frame = ctx.layers.polygons_intersecting(corridor(route), COVERAGE_LAYER)
    except (LayerNotFound, FileNotFoundError):
        return unavailable(
            name,
            "no cell coverage layer: the FCC bulk download needs an account, so no region "
            "build has loaded one",
        )

    from shapely.geometry import Point

    polygons = [row.geometry for _, row in frame.iterrows() if row.geometry is not None]
    carriers = sorted(
        {str(row["provider"]) for _, row in frame.iterrows() if row.get("provider")}
        if "provider" in frame.columns
        else set()
    )

    result = ScorerResult(name=name)
    dark = [not _covered(Point(p.lon, p.lat), polygons) for p in route.points]

    worst_gap = 0.0
    for segment in segments:
        span = dark[segment.start_idx : segment.end_idx + 1]
        # A segment counts as dark when *every* sampled point in it is: a segment with one
        # covered point still has somewhere to stand and make the call.
        all_dark = bool(span) and all(span)
        if all_dark:
            worst_gap = max(worst_gap, segment.length_m)
        result.measurements.append(
            SegmentMeasurement(
                segment_id=segment.id,
                values={
                    "no_signal": all_dark,
                    "dark_points": sum(1 for value in span if value),
                    "points": len(span),
                },
                confidence=SELF_REPORT_CONFIDENCE,
            )
        )

    for start_m, end_m, first_segment in _runs(route, segments, dark):
        length = end_m - start_m
        if length < GAP_FLAG_M:
            continue
        result.flags.append(
            Flag(
                scorer=name,
                segment_id=first_segment,
                kind=FlagKind.SOFT,
                tier=Tier.COMFORT,
                severity=SEVERITY_BY_CODE["no_signal_gap"],
                reason_code="no_signal_gap",
                detail=(
                    f"{length / 1000:.1f} km with no mapped signal from {start_m / 1000:.1f} km"
                ),
            )
        )
        worst_gap = max(worst_gap, length)

    result.measurements.append(
        SegmentMeasurement(
            segment_id=ROUTE_SUMMARY_ID,
            values={
                "dark_fraction": round(sum(dark) / len(dark), 3) if dark else None,
                "longest_gap_m": round(worst_gap, 1),
                "gap_threshold_m": GAP_FLAG_M,
                "carriers": ", ".join(carriers) or None,
            },
            confidence=SELF_REPORT_CONFIDENCE,
        )
    )

    record_coverage(
        result,
        source="fcc_bdc",
        kind="cell_coverage",
        vintage=ctx.layers.vintage(COVERAGE_LAYER),
        confidence=SELF_REPORT_CONFIDENCE,
    )
    result.coverage.append(
        CoverageEntry(
            source="fcc_bdc",
            kind="self_report",
            checked=True,
            reason=(
                "coverage is filed by carriers, not measured; a gap is evidence and "
                "coverage is a claim"
            ),
            confidence=SELF_REPORT_CONFIDENCE,
        )
    )
    return result


def _runs(
    route: Route, segments: list[Segment], dark: list[bool]
) -> list[tuple[float, float, str]]:
    """Contiguous dark stretches as (start_m, end_m, id of the segment they begin in).

    Measured over route *points* rather than segments, because a gap does not respect
    segment boundaries: two adjacent 250 m segments each half-dark are one 250 m gap, and
    counting them separately would report two shorter ones and flag neither.
    """
    out: list[tuple[float, float, str]] = []
    start: int | None = None
    for index, value in enumerate(dark):
        if value and start is None:
            start = index
        elif not value and start is not None:
            out.append(_span(route, segments, start, index - 1))
            start = None
    if start is not None:
        out.append(_span(route, segments, start, len(dark) - 1))
    return out


def _span(route: Route, segments: list[Segment], first: int, last: int) -> tuple[float, float, str]:
    from longrun.core.scorers._common import segment_at

    start_m = route.points[first].cum_dist_m
    end_m = route.points[min(last, len(route.points) - 1)].cum_dist_m
    segment = segment_at(segments, start_m)
    return start_m, end_m, segment.id if segment else ROUTE_SUMMARY_ID


score = cell_coverage

__all__ = [
    "COVERAGE_LAYER",
    "GAP_FLAG_M",
    "SELF_REPORT_CONFIDENCE",
    "SEVERITY_BY_CODE",
    "cell_coverage",
    "name",
    "score",
]
