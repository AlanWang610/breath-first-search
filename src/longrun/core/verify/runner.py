"""Running the scope 7.9 checklist against a plan.

Collects every check into one report. The report distinguishes three states rather than
two — passed, failed, skipped — because a plan sheet that prints "10/10 checks passed"
when three of them never ran is worse than one that admits the gap.
"""

from __future__ import annotations

from datetime import datetime

from longrun.core.models.geometry import Route, Segment
from longrun.core.models.measurement import FlagKind, ScorerResult
from longrun.core.models.request import PlanRequest
from longrun.core.models.verification import VerifyReport
from longrun.core.verify import checks


def _offenders_from(
    results: list[ScorerResult], scorer: str, requires: str | None = None
) -> list[str] | None:
    """Hard-flag segment ids from a scorer, or None if the check cannot be answered.

    None and [] are different answers: one means nobody looked, the other means nothing
    was found. Checks 5, 6 and 8 rely on that distinction to skip rather than pass.

    `requires` names the coverage *kind* a check actually depends on, which matters when
    a scorer half-ran. The crossings scorer can read the ways layer, emit soft flags, and
    still have no node layer to tell signalized crossings from unsignalized ones. Judging
    only by "did anything get flagged" would let check 8 - an explicit safety check for
    unsignalized crossings of fast roads - pass on a region where signalization was never
    known. A safety check that cannot run has to skip, not pass.
    """
    result = next((r for r in results if r.name == scorer), None)
    if result is None:
        return None
    if result.coverage and all(not entry.checked for entry in result.coverage):
        return None
    if requires is not None and any(
        entry.kind == requires and not entry.checked for entry in result.coverage
    ):
        return None
    return [f.segment_id for f in result.flags if f.kind is FlagKind.HARD]


def _unanswered(results: list[ScorerResult], scorer: str, kind: str) -> list[str]:
    """Jurisdictions a scorer could not check, named for a skip reason (ADR 0013).

    Separate from `requires=` on purpose. `requires` throws the whole answer away when any
    entry of the kind is unchecked, which is right for check 8 - a crossing whose
    signalization was never known cannot be judged at all - and wrong for check 6, where a
    closure that *was* found is a fact regardless of who else stayed silent.
    """
    result = next((r for r in results if r.name == scorer), None)
    if result is None:
        return []
    return [
        entry.jurisdiction or entry.source
        for entry in result.coverage
        if entry.kind == kind and not entry.checked
    ]


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
            checks.check_6_no_active_closures(
                _offenders_from(scorer_results, "closures"),
                _unanswered(scorer_results, "closures", "closures"),
            ),
            checks.check_7_distance_in_tolerance(
                route, request.target_distance_km, request.distance_tolerance_pct
            ),
            checks.check_8_no_dangerous_crossings(
                _offenders_from(scorer_results, "crossings", requires="traffic_signals")
            ),
            checks.check_9_time_constraints(request.time_constraints, etas),
            checks.check_10_locks_intact(segments or [], request.locked, original),
        ]
    )
