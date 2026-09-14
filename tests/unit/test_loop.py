"""The scope 8.1 loop: the cap, the lock, the pause and the resume.

`score_once` is stubbed here and the router is a stub, for the reason `arbitrate.py` gives
about itself - the control flow is what is being tested, and hand-built `ScorerResult`s are
the cheapest way to know the answer by construction. The real pass has its own tests in
`test_pipeline.py` and the real router has `tests/contract/test_graphhopper.py`.

The four properties that matter, and what each prevents:

* **the cap holds** - otherwise a route that cannot be improved is rerouted forever;
* **a resolved trade-off locks** - otherwise the loop reopens a choice the user already
  made, which is `Scratchpad.lock`'s documented purpose and had no caller until M5.5;
* **a locked span is excluded from the next round** - otherwise the lock is decorative;
* **a pause survives a process boundary** - which is the whole of scope 4.2's bet.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from longrun.agent import loop
from longrun.agent.loop import NO_MODEL, answer, plan_route
from longrun.core.data.cache import SqliteCache
from longrun.core.geo.segments import segment_route
from longrun.core.models.context import Budget, FrozenClock, ScorerContext
from longrun.core.models.coverage import CoverageManifest
from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.models.measurement import Flag, FlagKind, ScorerResult, Tier
from longrun.core.models.request import PlanRequest
from longrun.core.plan.pipeline import ScoredRoute
from longrun.core.plan.scratchpad import Scratchpad
from longrun.core.preferences.store import load_defaults

START = datetime(2026, 3, 15, 7, 30)


def _route(route_id: str = "r", n: int = 101) -> Route:
    return Route(
        id=route_id,
        points=[
            RoutePoint(lat=37.7749, lon=-122.4194 + i * 0.001139, cum_dist_m=i * 100.0)
            for i in range(n)
        ],
    )


def _ctx(tmp_path: Path) -> ScorerContext:
    from longrun.core.data.file_store import FileLayerStore, FileRasterStore

    return ScorerContext(
        layers=FileLayerStore(tmp_path),
        rasters=FileRasterStore(tmp_path),
        cache=SqliteCache(offline=True),
        clock=FrozenClock(START),
        coverage=CoverageManifest(),
        profile=load_defaults(),
        budget=Budget(),
    )


def _request() -> PlanRequest:
    return PlanRequest(mode="repair", date=START.date(), start_time=START.time())


class StubRouter:
    """Offers one detour per request, and counts how many it was asked for."""

    name = "stub"

    def __init__(self, *, offers: int = 1, same: bool = False) -> None:
        self.asked = 0
        self.offers = offers
        self.same = same

    def route(self, waypoints: Any, *a: Any, **k: Any) -> Route:  # pragma: no cover
        return _route()

    def alternatives(self, gpx: Route, around: Any = None, k: int = 3, **kw: Any) -> list[Route]:
        self.asked += 1
        if self.same:
            return [gpx.model_copy(update={"id": f"same-{self.asked}"})]
        return [_route(f"alt-{self.asked}-{i}") for i in range(self.offers)]

    def map_match(self, track: Route) -> tuple[Route, list[int | None]]:  # pragma: no cover
        return track, [None] * len(track.points)


def _scored(route: Route, flags: list[Flag]) -> ScoredRoute:
    segments = segment_route(route, max_len_m=1000.0)
    return ScoredRoute(
        route=route,
        segments=segments,
        results=[ScorerResult(name="segment_hostility", flags=flags)],
        etas=[START] * len(route.points),
        coverage=CoverageManifest(),
    )


def _flag(segment_id: str, severity: float = 0.8) -> Flag:
    return Flag(
        scorer="segment_hostility",
        segment_id=segment_id,
        kind=FlagKind.SOFT,
        tier=Tier.COMFORT,
        severity=severity,
        reason_code="lts_high",
    )


@pytest.fixture
def improving(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every round offers a route with one fewer flag than the one before it.

    Three earlier versions of this fixture were wrong in instructive ways, and each
    correction was the loop being right rather than the loop being broken. Flagging one
    segment stopped after a single round, because locking that span left nothing to fix.
    Flagging every segment *identically* stopped immediately, because a candidate that
    scores the same as the original is not an improvement and the loop refuses to adopt
    it. Only a genuinely improving sequence makes the five-round cap observable, which is
    the point: a loop that iterates on no improvement is the bug, not the fixture.
    """

    def improvements(route: Route) -> int:
        # `StubRouter` names its answers `alt-<ask>-<index>`, and the ask count rises with
        # the round - so this is "one better than last time".
        return int(route.id.split("-")[1]) if route.id.startswith("alt-") else 0

    def fake(route: Route, request: Any, ctx: Any, **kwargs: Any) -> ScoredRoute:
        segments = segment_route(route, max_len_m=1000.0)
        keep = max(1, len(segments) - improvements(route))
        return _scored(route, [_flag(segment.id) for segment in segments[:keep]])

    monkeypatch.setattr(loop, "score_once", fake)


# --- the cap ------------------------------------------------------------------


def test_the_loop_stops_at_five_rounds(tmp_path: Path, improving: None) -> None:
    """Scope 8.1 step 6: "iterate up to 5 rounds"."""
    router = StubRouter()

    outcome = plan_route(_request(), _ctx(tmp_path), start_at=START, route=_route(), router=router)

    assert outcome.scratchpad.round == 5
    assert outcome.scratchpad.status == "complete"
    assert outcome.plan is not None


def test_a_lower_cap_is_honoured(tmp_path: Path, improving: None) -> None:
    outcome = plan_route(
        _request(),
        _ctx(tmp_path),
        start_at=START,
        route=_route(),
        router=StubRouter(),
        max_rounds=2,
    )
    assert outcome.scratchpad.round == 2


def test_an_alternative_that_is_no_better_ends_the_loop(tmp_path: Path) -> None:
    """The sabotage M3 and M4 both paid for, as a test.

    A proposer that returns the same route must stop the loop, not run five rounds and
    report five rounds of improvement it did not make.
    """
    router = StubRouter(same=True)

    def fake(route: Route, request: Any, ctx: Any, **kwargs: Any) -> ScoredRoute:
        segments = segment_route(route, max_len_m=1000.0)
        return _scored(route, [_flag(segment.id) for segment in segments])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(loop, "score_once", fake)
        outcome = plan_route(
            _request(), _ctx(tmp_path), start_at=START, route=_route(), router=router
        )

    assert outcome.scratchpad.round == 0
    assert outcome.scratchpad.status == "complete"


def test_no_router_is_flag_but_do_not_fix(tmp_path: Path, improving: None) -> None:
    """`NullRouter`'s degradation, at the level of the loop: repair mode has no router by
    design (scope 6.1), and a plan is still a plan."""
    outcome = plan_route(_request(), _ctx(tmp_path), start_at=START, route=_route(), router=None)

    assert outcome.scratchpad.round == 0
    assert outcome.plan is not None
    assert outcome.plan.residual_flags, "the flags are still reported"


# --- the pause and the resume -------------------------------------------------


@pytest.fixture
def tied(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two candidates that differ inside one tier by less than the trade-off margin."""

    def fake(route: Route, request: Any, ctx: Any, **kwargs: Any) -> ScoredRoute:
        segments = segment_route(route, max_len_m=1000.0)
        severity = 0.80 if route.id == "r" else 0.79
        return _scored(route, [_flag(segment.id, severity) for segment in segments])

    monkeypatch.setattr(loop, "score_once", fake)


def test_a_same_tier_conflict_parks_the_job_and_says_what_it_asked(
    tmp_path: Path, tied: None
) -> None:
    """Scope 8.1 step 6: surfaced to the user, scratchpad persisted, `needs_input`."""
    outcome = plan_route(
        _request(), _ctx(tmp_path), start_at=START, route=_route(), router=StubRouter()
    )

    pad = outcome.scratchpad
    assert outcome.needs_input
    assert outcome.plan is None, "a parked plan is not a finished one"
    assert pad.question is not None
    assert len(pad.question.options) == 2
    assert pad.trade_offs, "the conflict is recorded whichever way it was asked"
    # `segment_hostility` maps to `traffic_tolerance`, which is `default` on a fresh
    # profile - so scope 6.3 permits the better question, and M5.11 asks it. The route
    # choice is still the answer; what changes is which of the two is settled by it.
    assert pad.question.kind == "preference"
    assert pad.question.axis == "traffic_tolerance"
    assert pad.questions_asked == 1


def test_a_parked_job_resumes_from_disk_and_locks_what_was_chosen(
    tmp_path: Path, tied: None
) -> None:
    """Scope 4.2's whole bet, across a file rather than a function call.

    `Scratchpad.lock` has said "called automatically when the user resolves a trade-off"
    since M1 and had no caller in `src/` until this. A choice already made must not be
    reopened on the next iteration.
    """
    first = plan_route(
        _request(), _ctx(tmp_path), start_at=START, route=_route(), router=StubRouter()
    )
    path = first.scratchpad.save(tmp_path / "scratchpad.json")

    reloaded = Scratchpad.load(path)
    assert reloaded.status == "needs_input"
    assert reloaded.question is not None
    answer(reloaded, reloaded.question.options[1])

    second = plan_route(
        _request(),
        _ctx(tmp_path),
        start_at=START,
        route=reloaded.route,
        router=StubRouter(),
        resume=reloaded,
    )

    pad = second.scratchpad
    assert pad.locked, "the resolved trade-off locked its segment"
    assert pad.answers, "and the answer travelled with the scratchpad"
    # It may well stop again - every segment of this route is a tie, so the next one is
    # too - but it must not stop on the *same* question, which is what a loop with no
    # memory of the answer would do.
    assert pad.question is None or pad.question.id not in pad.answers


def test_a_locked_span_is_not_offered_again(tmp_path: Path, improving: None) -> None:
    """Otherwise the lock is decorative and the loop reroutes the same segment five times."""
    router = StubRouter()
    outcome = plan_route(_request(), _ctx(tmp_path), start_at=START, route=_route(), router=router)

    pad = outcome.scratchpad
    assert len(pad.locked) == 5
    spans = {(round(lock.start_m), round(lock.end_m)) for lock in pad.locked}
    assert len(spans) == 5, f"the same span was rerouted twice: {spans}"


def test_answering_nothing_is_an_error_rather_than_a_silent_no_op() -> None:
    pad = Scratchpad(plan_id="p", request=_request())
    with pytest.raises(ValueError, match="nothing was asked"):
        answer(pad, "alternative 1")


# --- no model -----------------------------------------------------------------


def test_the_whole_loop_runs_with_no_call_sites_configured(tmp_path: Path, tied: None) -> None:
    """ADR 0015. `NO_MODEL` is what every golden and every test runs with, and the
    trade-off still gets raised, phrased and recorded - just terser."""
    outcome = plan_route(
        _request(),
        _ctx(tmp_path),
        start_at=START,
        route=_route(),
        router=StubRouter(),
        sites=NO_MODEL,
    )

    assert outcome.needs_input
    assert outcome.scratchpad.question is not None
    assert outcome.scratchpad.question.prompt, "a question with no model still has words"


def test_the_model_writes_the_comparison_and_not_the_choice(tmp_path: Path, tied: None) -> None:
    """ADR 0019: scope 4.1 lists "choose among same-tier alternatives" as a call site and
    scope 8.1 step 6 gives the choice to the user. The describer is handed both candidates
    and its answer is *prose* - the loop still parks."""
    from longrun.agent.loop import CallSites

    seen: list[tuple[str, str]] = []

    def describe(a: Any, b: Any) -> str:
        seen.append((a.label, b.label))
        return "A is 0.8 km longer; B has 600 m without a sidewalk at mile 4"

    outcome = plan_route(
        _request(),
        _ctx(tmp_path),
        start_at=START,
        route=_route(),
        router=StubRouter(),
        sites=CallSites(describe=describe),
    )

    assert seen, "the describer was called"
    assert outcome.needs_input, "and it did not get to decide"
    pad = outcome.scratchpad
    assert pad.question is not None
    assert pad.trade_offs, "the comparison it wrote is recorded on the trade-off"
    assert "sidewalk" in pad.trade_offs[0].comparison


# --- step 8 --------------------------------------------------------------------


def test_the_imagery_step_is_reported_rather_than_skipped(tmp_path: Path, improving: None) -> None:
    """Scope 8.1 step 8 has a budget meter, no implementation and no provider. A step that
    is not run and not mentioned is indistinguishable from one that found nothing."""
    outcome = plan_route(_request(), _ctx(tmp_path), start_at=START, route=_route())

    assert any("step 8" in note for note in outcome.scratchpad.manifest.degradation)
