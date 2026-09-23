"""A pass that runs some scorers and carries the rest (scope 7.8).

The primitive `longrun refresh` is built on, and the one M12 and M13 need so a direct
manipulation does not re-run twenty-one scorers inside a 180-second budget.

Most of these are about honesty rather than about speed. A partial pass that merely went
faster would be easy; one that cannot quietly claim a scorer ran, cannot lose a carried
scorer's coverage, and cannot feed a re-run scorer a stale `prior` is the thing worth having.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from longrun.core.data.cache import SqliteCache
from longrun.core.data.file_store import FileLayerStore, FileRasterStore
from longrun.core.models.context import Budget, FrozenClock, ScorerContext
from longrun.core.models.coverage import CoverageEntry, CoverageManifest
from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.models.measurement import ScorerResult, SegmentMeasurement
from longrun.core.models.plan import Manifest
from longrun.core.preferences.store import load_defaults
from longrun.core.scorers import registry
from longrun.core.scorers.registry import (
    SCORERS,
    StalePrior,
    UnknownScorer,
    closure,
    run_scorers,
)

MEASURED_AT = datetime(2026, 3, 15, 7, 30)
REFRESHED_AT = datetime(2026, 9, 20, 7, 30)


def _route() -> Route:
    return Route(
        id="partial",
        points=[
            RoutePoint(lat=37.7749, lon=-122.4194 + i * 0.00114, cum_dist_m=i * 100.0)
            for i in range(5)
        ],
    )


def _ctx(root: Path, budget: Budget | None = None) -> ScorerContext:
    return ScorerContext(
        layers=FileLayerStore(root),
        rasters=FileRasterStore(root),
        cache=SqliteCache(offline=True),
        clock=FrozenClock(MEASURED_AT),
        coverage=CoverageManifest(),
        profile=load_defaults(),
        budget=budget or Budget(),
    )


def _stored(name: str, *, sentinel: float = 1.0) -> ScorerResult:
    """A result with a value nothing else could produce, so carrying is provable."""
    return ScorerResult(
        name=name,
        measurements=[SegmentMeasurement(segment_id="s0", values={"sentinel": sentinel})],
        coverage=[CoverageEntry(source=name, kind="stored", checked=True)],
    )


def _etas(route: Route) -> list[datetime]:
    return [MEASURED_AT for _ in route.points]


def test_a_partial_pass_returns_every_scorer_not_just_the_ones_it_ran(tmp_path: Path) -> None:
    """A missing name in `Plan.results` reads as "did not run", so nothing may be omitted.

    Order matters as well as membership: "complete but reordered" passes a set assertion and
    breaks both the `prior` chain and the coverage block a golden compares line by line.
    """
    route = _route()
    results = run_scorers(
        route,
        [],
        _ctx(tmp_path),
        _etas(route),
        only={"lighting"},
        carried=[_stored(name) for name in SCORERS if name != "lighting"],
        carried_as_of=MEASURED_AT,
        carried_route=route,
        carried_segments=[],
    )
    assert [r.name for r in results] == list(SCORERS)


def test_a_carried_result_says_it_was_carried_and_a_rescored_one_does_not(
    tmp_path: Path,
) -> None:
    route = _route()
    stored = _stored("legality")
    results = run_scorers(
        route,
        [],
        _ctx(tmp_path),
        _etas(route),
        only={"lighting"},
        carried=[stored],
        carried_as_of=MEASURED_AT,
        carried_route=route,
        carried_segments=[],
    )
    by_name = {r.name: r for r in results}

    assert by_name["legality"].carried_from == MEASURED_AT
    assert by_name["legality"].measurements[0].values["sentinel"] == 1.0
    assert by_name["lighting"].carried_from is None

    # Marking by mutation would stamp `carried_from` onto the stored plan still in memory,
    # and after a `--out` write onto the file the user believes is the original.
    assert by_name["legality"] is not stored
    assert stored.carried_from is None


def test_a_result_carried_twice_keeps_the_date_it_was_measured_for(tmp_path: Path) -> None:
    """Otherwise the honesty field becomes a laundering mechanism: a six-month-old result
    reports as one day old after a single refresh."""
    route = _route()
    once = run_scorers(
        route,
        [],
        _ctx(tmp_path),
        _etas(route),
        only={"lighting"},
        carried=[_stored("legality")],
        carried_as_of=MEASURED_AT,
        carried_route=route,
        carried_segments=[],
    )
    twice = run_scorers(
        route,
        [],
        _ctx(tmp_path),
        _etas(route),
        only={"lighting"},
        carried=[r for r in once if r.name == "legality"],
        carried_as_of=REFRESHED_AT,
        carried_route=route,
        carried_segments=[],
    )
    assert next(r for r in twice if r.name == "legality").carried_from == MEASURED_AT


def test_a_scorer_with_nothing_to_carry_is_reported_as_not_run(tmp_path: Path) -> None:
    """The other end of the rule: you cannot get a silent gap either."""
    route = _route()
    results = run_scorers(route, [], _ctx(tmp_path), _etas(route), only={"lighting"}, carried=[])
    legality = next(r for r in results if r.name == "legality")
    assert legality.carried_from is None
    assert not legality.measurements
    assert legality.coverage and not legality.coverage[0].checked
    assert "carry" in (legality.coverage[0].reason or "")


def test_a_rescored_scorer_is_handed_the_carried_results_in_registry_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline test, and the one the natural bug passes every other assertion against.

    Appending carried results at the end of the list still yields a complete, correctly named
    result set - and a re-run scorer's `prior` sees nothing before it. So this asserts on what
    the scorer was actually handed, not on what came back.

    Both real `prior` readers scan by name, so an incomplete list does not raise: it silently
    finds nothing and falls back. `heat._sunlit_by_segment` returning empty means WBGT
    computed as though the whole route were in full sun.
    """
    seen: list[list[str]] = []

    def _third(route: Any, segments: Any, ctx: Any, etas: Any, prior: Any) -> ScorerResult:
        seen.append([r.name for r in prior])
        return ScorerResult(name="third")

    # No declared edge: `third` reading `prior` without declaring a dependency is legal, and
    # is exactly the case where ordering has to be right rather than merely checked.
    monkeypatch.setattr(
        registry,
        "SCORERS",
        {"first": "stub.first", "second": "stub.second", "third": "stub.third"},
    )
    monkeypatch.setattr(registry, "PRIOR_DEPENDENCIES", {})
    monkeypatch.setattr(registry, "load_scorer", lambda path: _third)

    route = _route()
    registry.run_scorers(
        route,
        [],
        _ctx(tmp_path),
        _etas(route),
        only={"third"},
        carried=[_stored("first"), _stored("second")],
        carried_as_of=MEASURED_AT,
        carried_route=route,
        carried_segments=[],
    )
    assert seen == [["first", "second"]], (
        "the carried results were not in `prior`, in registry order, when `third` ran"
    )


def test_a_declared_prior_is_always_re_run_and_so_is_never_carried(tmp_path: Path) -> None:
    """The safety property `closure()` buys, stated as behaviour rather than as a set.

    Because `only` must be closed, a scorer that reads an earlier result is only ever handed
    one this pass measured. There is no code path on which `heat_stress` sees a carried
    `sun_exposure`, which is why the stale-prior case cannot arise rather than being handled.
    """
    assert closure({"heat_stress"}) == frozenset({"sun_exposure", "heat_stress"})

    route = _route()
    results = run_scorers(
        route,
        [],
        _ctx(tmp_path),
        _etas(route),
        only=closure({"heat_stress"}),
        carried=[_stored(name) for name in SCORERS],
        carried_as_of=MEASURED_AT,
        carried_route=route,
        carried_segments=[],
    )
    by_name = {r.name: r for r in results}
    assert by_name["sun_exposure"].carried_from is None
    assert by_name["heat_stress"].carried_from is None
    assert by_name["legality"].carried_from == MEASURED_AT


def test_only_refuses_a_set_that_would_feed_a_scorer_a_stale_prior(tmp_path: Path) -> None:
    """The one place a partial pass produces a *wrong* number rather than a stale one: WBGT
    computed from last month's solar geometry, silently, in the physiological tier."""
    route = _route()
    with pytest.raises(StalePrior, match="sun_exposure"):
        run_scorers(route, [], _ctx(tmp_path), _etas(route), only={"heat_stress"})


def test_only_refuses_a_name_no_scorer_answers_to(tmp_path: Path) -> None:
    """A typo that refreshed nothing and reported success would be worse than a crash."""
    route = _route()
    with pytest.raises(UnknownScorer, match="heat_stres"):
        run_scorers(route, [], _ctx(tmp_path), _etas(route), only={"heat_stres"})


def test_a_carried_scorers_coverage_reaches_the_manifest_exactly_once(
    tmp_path: Path,
) -> None:
    """Present, and present once. The count is the assertion: the natural bug is draining in
    two places, which loses nothing and duplicates everything."""
    route = _route()
    ctx = _ctx(tmp_path)
    run_scorers(
        route,
        [],
        ctx,
        _etas(route),
        only={"lighting"},
        carried=[_stored("legality")],
        carried_as_of=MEASURED_AT,
        carried_route=route,
        carried_segments=[],
    )
    stored_entries = [e for e in ctx.coverage.entries if e.kind == "stored"]
    assert len(stored_entries) == 1
    assert stored_entries[0].source == "legality"


def test_a_carried_scorer_costs_no_latency_and_no_manifest_entry(tmp_path: Path) -> None:
    route = _route()
    manifest = Manifest()
    run_scorers(
        route,
        [],
        _ctx(tmp_path),
        _etas(route),
        manifest,
        only={"lighting"},
        carried=[_stored(name) for name in SCORERS if name != "lighting"],
        carried_as_of=MEASURED_AT,
        carried_route=route,
        carried_segments=[],
    )
    assert {c.tool for c in manifest.tool_calls} == {"lighting"}


def test_an_exhausted_budget_still_carries_rather_than_dropping(tmp_path: Path) -> None:
    """A carried scorer spent no time, so it must not be converted into "not scored" for want
    of time it never used. This is what forces the skip branch above the deadline check."""
    route = _route()
    results = run_scorers(
        route,
        [],
        _ctx(tmp_path, Budget(latency_budget_s=0.0)),
        _etas(route),
        only={"lighting"},
        carried=[_stored("legality")],
        carried_as_of=MEASURED_AT,
        carried_route=route,
        carried_segments=[],
    )
    legality = next(r for r in results if r.name == "legality")
    assert legality.carried_from == MEASURED_AT
    assert legality.measurements[0].values["sentinel"] == 1.0
