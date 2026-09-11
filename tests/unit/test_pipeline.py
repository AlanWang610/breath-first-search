"""One scoring pass, and the property M5.1 exists to provide: a context that survives it.

Scope 8.1 step 6 scores several candidate routes per round for up to five rounds. Until
M5.1 the only way to score a route was `cli/repair.py::score_route`, which built its own
`SqliteCache`, its own `Budget` and its own `CoverageManifest` - so `longrun plan
--alternatives 3` already ran four independent 200-call budgets against a scope 6.4 cap of
200, and the loop would have made that sixteen.

These tests are the ones that would have caught that, and they are deliberately about the
*second* pass. A seam that is only ever exercised once is not a seam.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from longrun.core.data.cache import SqliteCache
from longrun.core.data.file_store import FileLayerStore, FileRasterStore
from longrun.core.models.context import Budget, FrozenClock, ScorerContext
from longrun.core.models.coverage import CoverageManifest
from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.models.plan import Manifest
from longrun.core.models.request import PlanRequest
from longrun.core.plan.pipeline import build_plan, score_once
from longrun.core.preferences.store import load_defaults

START = datetime(2026, 3, 15, 7, 30)


def _route(points: int = 21, spacing_m: float = 100.0) -> Route:
    """A straight run east from a point in San Francisco, at 100 m spacing."""
    return Route(
        id="pass",
        points=[
            RoutePoint(
                lat=37.7749,
                lon=-122.4194 + index * 0.00114,
                cum_dist_m=index * spacing_m,
            )
            for index in range(points)
        ],
    )


def _ctx(root: Path, budget: Budget) -> ScorerContext:
    return ScorerContext(
        layers=FileLayerStore(root),
        rasters=FileRasterStore(root),
        cache=SqliteCache(offline=True),
        clock=FrozenClock(START),
        coverage=CoverageManifest(),
        profile=load_defaults(),
        budget=budget,
    )


def _request() -> PlanRequest:
    return PlanRequest(mode="repair", date=START.date(), start_time=START.time())


def test_a_second_pass_does_not_inherit_the_first_passs_coverage(tmp_path: Path) -> None:
    """`CoverageManifest.record` is a bare append, so a shared manifest doubles.

    The failure this prevents is not a crash. It is a sheet that reports every source twice
    because the loop looked at two candidates, in the block `expectation.digest` compares
    verbatim - which would read as a coverage regression in a golden diff and be a lifecycle
    bug in the loop.
    """
    ctx = _ctx(tmp_path, Budget())
    route, request = _route(), _request()

    first = score_once(route, request, ctx, start_at=START)
    # Read before the second pass runs. Comparing the two manifests afterwards is what the
    # first version of this test did, and a shared manifest makes them the *same object* -
    # so control and treatment agreed and the test passed against the bug it was written
    # for. Deliberate sabotage is what found that, as it did in M3 and M4.
    sources = [entry.source for entry in first.coverage.entries]
    assert sources, "a run against no layers still reports what it could not check"

    second = score_once(route, request, ctx, start_at=START)

    assert [entry.source for entry in second.coverage.entries] == sources
    assert [entry.source for entry in first.coverage.entries] == sources, (
        "the second pass appended to the first pass's record"
    )


def test_the_context_keeps_its_own_coverage_across_a_pass(tmp_path: Path) -> None:
    """A pass borrows `ctx.coverage`; it does not keep it.

    Otherwise the second pass would start from wherever the first one left off, which is
    the same bug wearing the other hat.
    """
    ctx = _ctx(tmp_path, Budget())
    before = ctx.coverage

    score_once(_route(), _request(), ctx, start_at=START)

    assert ctx.coverage is before
    assert ctx.coverage.entries == []


def test_the_budget_is_the_plans_and_not_the_passs(tmp_path: Path) -> None:
    """Scope 6.4 caps external calls per *plan*, and a plan is several passes.

    Spending between passes and finding the spend still counted is the whole difference
    between one 200-call budget and sixteen.
    """
    budget = Budget()
    ctx = _ctx(tmp_path, budget)

    score_once(_route(), _request(), ctx, start_at=START)
    budget.spend_api_call(3)
    score_once(_route(), _request(), ctx, start_at=START)

    assert ctx.budget is budget
    assert budget.api_calls_used == 3


def test_a_pass_becomes_a_plan_carrying_that_passs_coverage(tmp_path: Path) -> None:
    """`build_plan` is the other half of the split, and the seam the loop assembles through."""
    ctx = _ctx(tmp_path, Budget())
    request = _request()
    manifest = Manifest()

    scored = score_once(_route(), request, ctx, start_at=START, manifest=manifest)
    plan = build_plan(
        scored,
        request,
        profile=load_defaults(),
        coverage=scored.coverage,
        manifest=manifest,
    )

    assert plan.route is scored.route
    assert plan.coverage is scored.coverage
    assert plan.etas == scored.etas
    # Every scorer is timed into the manifest (scope 6.4), including the ones that report
    # unavailable - "which scorer is slow" must not become unanswerable on a bare fixture.
    assert manifest.tool_calls, "a pass records what it ran"
