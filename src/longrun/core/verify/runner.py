"""Running the scope 7.9 checklist against a plan.

Collects every check into one report. The report distinguishes three states rather than
two — passed, failed, skipped — because a plan sheet that prints "10/10 checks passed"
when three of them never ran is worse than one that admits the gap.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from longrun.core.models.geometry import Route, Segment
from longrun.core.models.measurement import FlagKind, ScorerResult
from longrun.core.models.request import PlanRequest
from longrun.core.verify import checks
from longrun.core.verify.checks import CheckResult


class VerifyReport(BaseModel):
    """The outcome of all ten checks."""

    results: list[CheckResult] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True only when nothing failed. Skips do not fail a plan, but are reported."""
        return not any(r.blocking for r in self.results)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == "failed"]

    @property
    def skipped(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == "skipped"]

    def offending_segments(self) -> list[str]:
        """Everything a failure named, for scope 8.1 step 9 to reroute."""
        return [o for r in self.failures for o in r.offenders]

    def summary(self) -> str:
        counts = {"passed": 0, "failed": 0, "skipped": 0}
        for result in self.results:
            counts[result.status] += 1
        return (
            f"{counts['passed']} passed, {counts['failed']} failed, "
            f"{counts['skipped']} skipped of {len(self.results)}"
        )


def _offenders_from(results: list[ScorerResult], scorer: str) -> list[str] | None:
    """Hard-flag segment ids from a scorer, or None if the scorer never ran.

    None and [] are different answers: one means nobody looked, the other means nothing
    was found. Checks 5, 6 and 8 rely on that distinction to skip rather than pass.
    """
    result = next((r for r in results if r.name == scorer), None)
    if result is None:
        return None
    if any(not entry.checked for entry in result.coverage) and not result.flags:
        return None
    return [f.segment_id for f in result.flags if f.kind is FlagKind.HARD]


def gpx_verify(
    route: Route,
    request: PlanRequest,
    segments: list[Segment] | None = None,
    results: list[ScorerResult] | None = None,
    etas: list[datetime] | None = None,
    elevations: list[float | None] | None = None,
    snapped_distances_m: list[float] | None = None,
    original: Route | None = None,
) -> VerifyReport:
    """Run the scope 7.9 checklist.

    Every input except the route is optional, and each missing one turns its check into a
    skip rather than a pass — so a partially-built plan verifies honestly instead of
    claiming clearance it has not earned.
    """
    scorer_results = results or []
    return VerifyReport(
        results=[
            checks.check_1_valid_gpx(route),
            checks.check_2_on_network(route, snapped_distances_m),
            checks.check_3_no_gaps(route),
            checks.check_4_elevation_sane(elevations),
            checks.check_5_no_illegal_ways(_offenders_from(scorer_results, "legality")),
            checks.check_6_no_active_closures(_offenders_from(scorer_results, "closures")),
            checks.check_7_distance_in_tolerance(
                route, request.target_distance_km, request.distance_tolerance_pct
            ),
            checks.check_8_no_dangerous_crossings(_offenders_from(scorer_results, "crossings")),
            checks.check_9_time_constraints(request.time_constraints, etas),
            checks.check_10_locks_intact(segments or [], request.locked, original),
        ]
    )
