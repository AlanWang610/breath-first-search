"""Which scorers exist, and the order scope 8.1 step 5 runs them in.

This lived in `cli/repair.py` from M1 until M5.1, which is where it was written and where
it could not stay. The agent loop, the MCP tool layer and the CLI all need the same list,
and `core/` may not import `cli/` (scope 4.1) - so a registry owned by a CLI command would
have forced every consumer to invert the one arrow `test_layering.py` exists to protect.

The order is dependency order rather than alphabetical, and two entries rely on it. Nothing
here decides anything: a scorer that is missing is reported, never skipped, because a plan
sheet must not imply a scorer ran clean when it does not exist (scope 3.6).
"""

from __future__ import annotations

import importlib
import inspect
import time
from collections.abc import Collection, Iterable, Sequence
from datetime import datetime
from typing import Any, cast

from longrun.core.geo.segments import SegmentMap, map_segments
from longrun.core.models.context import BudgetExceeded, ScorerContext
from longrun.core.models.measurement import Regrounded, ScorerResult
from longrun.core.models.plan import Manifest, ToolCall
from longrun.core.scorers.base import unavailable

#: Scorer name -> module path. Scope 8.1 step 5's list, minus the ones whose milestone
#: has not landed; each missing module is reported, never silently omitted.
SCORERS: dict[str, str] = {
    "legality": "longrun.core.scorers.legality",
    "segment_hostility": "longrun.core.scorers.hostility",
    "crossings": "longrun.core.scorers.crossings",
    "stop_density": "longrun.core.scorers.stop_density",
    "surface_profile": "longrun.core.scorers.surface",
    "services_along": "longrun.core.scorers.services",
    "microclimate": "longrun.core.scorers.microclimate",
    # Order is dependency order: `sun_exposure` before `heat_stress`, which reads the
    # sunlit fraction back out of it (scope 7.4: "from sun exposure + temp + ...").
    "sun_exposure": "longrun.core.scorers.sun",
    "heat_stress": "longrun.core.scorers.heat",
    "lighting": "longrun.core.scorers.lighting",
    "air_quality": "longrun.core.scorers.air_quality",
    # After heat_stress: scope 8.3 scales the dry-gap thresholds down with WBGT.
    "resupply_schedule": "longrun.core.scorers.resupply_schedule",
    "hazards": "longrun.core.scorers.hazards",
    # The three adapter-fed scorers (scope 7.6, 7.10). Grouped because they resolve the
    # same jurisdictions and ask the same registry; `closures` is first because it is the
    # only one that can fail verification.
    "closures": "longrun.core.scorers.closures",
    "trail_status": "longrun.core.scorers.trail_status",
    "access_hours": "longrun.core.scorers.access_hours",
    "transit": "longrun.core.scorers.transit",
    # After transit: both read the same stop layer, and `bailouts` reuses its service test.
    "bailouts": "longrun.core.scorers.bailouts",
    "crew_points": "longrun.core.scorers.crew_points",
    # Last, and deliberately: it re-evaluates what the time-dependent scorers above
    # measured, at a dozen other start times, from the horizons they already built.
    "start_time_optimizer": "longrun.core.scorers.start_time_optimizer",
    "cell_coverage": "longrun.core.scorers.cell_coverage",
}

#: Scorers the scope calls for whose milestone has not arrived. Named explicitly so the
#: coverage manifest can say they were not run, instead of the sheet staying silent.
#:
#: **Empty since M4**, and kept rather than deleted. The mechanism is the honest one for a
#: scorer the scope names and this build cannot run - it is how `closures`, `trail_status`
#: and `access_hours` were reported from M1 until M4.4 wrote them. The next scorer the scope
#: names and a milestone defers belongs here, not in a comment.
NOT_YET_IMPLEMENTED: dict[str, str] = {}


#: Scorer -> the earlier scorers it reads out of `prior`. The `_call` docstring below argues
#: that these dependencies are the scope's rather than an implementation shortcut; this is
#: that argument written down in a form something can check.
#:
#: It exists because a partial re-score can otherwise produce a *wrong* number rather than a
#: stale one. Carrying `sun_exposure` while re-running `heat_stress` against a new date feeds
#: WBGT a shaded fraction computed from last month's solar geometry - silently, in the
#: physiological tier. `closure()` is what makes that impossible to ask for by accident.
PRIOR_DEPENDENCIES: dict[str, frozenset[str]] = {
    "heat_stress": frozenset({"sun_exposure"}),
    "resupply_schedule": frozenset({"heat_stress"}),
}


class UnknownScorer(ValueError):
    """A scorer was named that nothing answers to - usually a typo in `--only`."""


class StalePrior(ValueError):
    """A partial pass would have fed a scorer an earlier result it did not re-run."""


class UngroundedCarry(ValueError):
    """Results were offered to carry with no segmentation to say what their ids meant.

    A carried `segment_id` is an index into the segmentation that produced it, and without
    that segmentation there is no way to tell whether `s00042` still names the same ground.
    Refused rather than assumed, for the reason `StalePrior` is: assuming produces a
    *wrong* number rather than a missing one, and a wrong one is silent.
    """


def closure(names: Iterable[str]) -> frozenset[str]:
    """`names`, plus everything they read out of `prior`, transitively.

    The public helper a caller uses to widen its own request *and be able to say so*.
    `run_scorers` deliberately refuses an unclosed set rather than expanding one quietly:
    asking for one scorer and getting three is a thing the user should be told, and the
    place to tell them is the caller that has a terminal.
    """
    wanted = set(names)
    frontier = list(wanted)
    while frontier:
        for dependency in PRIOR_DEPENDENCIES.get(frontier.pop(), frozenset()):
            if dependency not in wanted:
                wanted.add(dependency)
                frontier.append(dependency)
    return frozenset(wanted)


def carry(result: ScorerResult, as_of: datetime, onto: SegmentMap | None = None) -> ScorerResult:
    """A stored result, restated as one this pass did not produce, on this pass's ground.

    `result.carried_from or as_of` rather than `as_of`: a result carried through five
    successive refreshes must keep the date it was *originally measured for*. Overwriting
    it each time would make a six-month-old `legality` report as one day old after a single
    refresh, which turns the honesty field into a laundering mechanism.

    `deep=True` because `ScorerResult` is the one measurement model that is not frozen and
    `record_coverage` appends to `result.coverage` in place - a shallow copy would share its
    lists with the stored plan's objects.

    **`onto` is the other half, and it is what M11.3 is for** (ADR 0032). A stored
    measurement is keyed on a `segment_id`, `segment_id` is `f"s{index:05d}"`, and an edit
    that moves the line renumbers every segment downstream of it - so a carried measurement
    whose id is merely *reused* is a measurement of different ground, presented as this
    one's. Where the map says the segmentation did not move, nothing here changes, which is
    every refresh. Where it moved, every measurement, flag and waypoint is re-keyed or
    dropped, and `Regrounded` records which.

    Dropped, never re-pointed. There is no rule by which the nearest surviving segment
    inherits a measurement: "close to where this was taken" is not "where this was taken",
    and scope 3.6's whole argument is that an absent answer beats a plausible wrong one.
    """
    carried = result.model_copy(update={"carried_from": result.carried_from or as_of}, deep=True)
    if onto is None or not onto.moved:
        return carried

    kept_measurements = []
    dropped = totals_dropped = 0
    for measurement in carried.measurements:
        landed = onto.onto.get(measurement.segment_id)
        if landed is not None:
            kept_measurements.append(measurement.model_copy(update={"segment_id": landed}))
        elif measurement.segment_id in onto:
            dropped += 1
        else:
            totals_dropped += 1

    kept_flags = []
    flags_dropped = 0
    for flag in carried.flags:
        landed = onto.onto.get(flag.segment_id)
        if landed is not None:
            kept_flags.append(flag.model_copy(update={"segment_id": landed}))
        else:
            flags_dropped += 1

    # A waypoint carries no segment id - it is keyed on `cum_dist_m`, which is a distance
    # along the line that was just edited. Kept only where the ground at that distance is
    # ground this map matched, because that is exactly the condition under which the
    # distance still points at the place the scorer found.
    spans = onto.kept_spans
    kept_waypoints = [w for w in carried.waypoints if _within(spans, w.cum_dist_m)]

    carried.measurements = kept_measurements
    carried.flags = kept_flags
    dropped_waypoints = len(carried.waypoints) - len(kept_waypoints)
    carried.waypoints = kept_waypoints
    carried.regrounded = Regrounded(
        measurements_kept=len(kept_measurements),
        measurements_dropped=dropped,
        route_totals_dropped=totals_dropped,
        flags_kept=len(kept_flags),
        flags_dropped=flags_dropped,
        waypoints_kept=len(kept_waypoints),
        waypoints_dropped=dropped_waypoints,
    )
    return carried


def _within(spans: Sequence[tuple[float, float]], distance_m: float) -> bool:
    return any(start <= distance_m <= end for start, end in spans)


def load_scorer(module_path: str) -> Any | None:
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError:
        return None
    return getattr(module, "score", None)


def _call(
    func: Any,
    route: Any,
    segments: list[Any],
    ctx: ScorerContext,
    etas: list[datetime],
    prior: list[ScorerResult],
) -> ScorerResult:
    """Invoke a scorer, handing it earlier results only if it asks for them.

    Two scope 7.4/7.5 tools are defined in terms of another scorer's output rather than of
    raw data: `heat_stress` is "WBGT ... from **sun exposure** + temp + humidity + wind",
    and scope 8.3's dry-gap thresholds "both scale down with WBGT". So the dependency is
    the scope's, not an implementation shortcut.

    Inspected rather than passed to everything, so the six scorers that are pure functions
    of `(route, segments, ctx, etas)` stay that way and cannot quietly grow a dependency on
    execution order. `SCORERS` is an ordered dict, and that order is the dependency order.
    """
    if "prior" in inspect.signature(func).parameters:
        return cast("ScorerResult", func(route, segments, ctx, etas, prior=prior))
    return cast("ScorerResult", func(route, segments, ctx, etas))


def _grounding(
    route: Any,
    segments: Sequence[Any],
    carried: Sequence[ScorerResult],
    carried_route: Any,
    carried_segments: Sequence[Any] | None,
) -> SegmentMap | None:
    """The map from the stored results' segmentation onto this pass's, or None to carry none.

    Computed once for the whole pass rather than per scorer: every carried result was
    measured against the same line, so twenty-one identical searches would be twenty of them
    wasted - and, worse, twenty chances for two scorers to disagree about where a segment
    went.
    """
    if not carried:
        return None
    if carried_route is None or carried_segments is None:
        raise UngroundedCarry(
            f"{len(carried)} stored result(s) were offered to carry with no segmentation "
            f"they were measured against. Pass `carried_route` and `carried_segments`; a "
            f"`segment_id` is positional, so without them a carried measurement cannot say "
            f"whether it is still about the same ground."
        )
    return map_segments(carried_route, list(carried_segments), route, list(segments))


def _check_only(only: Collection[str]) -> None:
    """Refuse a partial pass that would misreport, before any of it runs."""
    known = set(SCORERS) | set(NOT_YET_IMPLEMENTED)
    unknown = sorted(set(only) - known)
    if unknown:
        raise UnknownScorer(f"no scorer answers to {unknown}; known names are {sorted(known)}")
    missing = sorted(closure(only) - set(only))
    if missing:
        raise StalePrior(
            f"{missing} would be carried while a scorer that reads them is re-run, so they "
            f"would be measured against a different date. Add them, or use `closure()`."
        )


def run_scorers(
    route: Any,
    segments: list[Any],
    ctx: ScorerContext,
    etas: list[datetime],
    manifest: Manifest | None = None,
    *,
    only: Collection[str] | None = None,
    carried: Sequence[ScorerResult] = (),
    carried_as_of: datetime | None = None,
    carried_route: Any = None,
    carried_segments: Sequence[Any] | None = None,
) -> list[ScorerResult]:
    """Run every scorer that exists; report every one that does not.

    Each is timed into the manifest. Scope 6.4 wants per-tool elapsed time recorded so the
    ~3-minute budget is measured rather than assumed, and until M2 `Manifest.tool_calls`
    existed with nothing writing to it - so "which scorer is slow" was a question only a
    profiler could answer, and only on a machine that had one.

    `only` names the scorers to actually run; the rest are taken from `carried` and stamped
    with `carried_as_of`. `only=None` runs everything and must stay a bit-exact no-op - the
    golden suite is what checks that.

    `carried_route` and `carried_segments` are the line and the segmentation those stored
    results were measured against, and they are **required** whenever anything is carried.
    A `segment_id` is `f"s{index:05d}"` - an index - so without them there is no way to ask
    whether `s00042` still names the same ground, and the failure of guessing is silent
    (ADR 0032). `carried_segments=[]` is a real answer and not a missing one: it says the
    stored results were measured against no segmentation, which is what a synthetic result
    in a test has, and it maps identically onto `segments=[]`.

    **The merge happens here rather than in a caller, and that is the load-bearing choice.**
    Two things force it. `prior` is assembled from the list under construction, so a carried
    result is only visible to a later scorer if it already sits at its registry position when
    that scorer runs - no post-hoc merge can achieve that, and both real `prior` readers scan
    by name, so an incomplete list does not raise but silently finds nothing. And the coverage
    drain at the bottom of this function iterates `results`, so carried results seeded here
    drain with no new code; merging outside means either losing their coverage entries or
    draining in two places, and the second is a double-count waiting for its first refactor.
    """
    if only is not None:
        _check_only(only)
    wanted = set(SCORERS) if only is None else set(only)
    stored = {result.name: result for result in carried}
    as_of = carried_as_of or (etas[0] if etas else None)
    onto = _grounding(route, segments, carried, carried_route, carried_segments)

    results: list[ScorerResult] = []
    #: Scorers this pass actually invoked, as opposed to carried. Counted separately so the
    #: degradation message below says "17 of 17" rather than "17 of 21" on a partial pass.
    ran = 0
    #: Set once the plan outruns scope 6.4's latency target. Every scorer after that is
    #: reported as not run rather than dropped, because a sheet that is quietly shorter is
    #: the failure scope 3.6 exists to prevent - and the degradation is named in the
    #: manifest, which is the field scope 6.4 asks for and nothing wrote until M5.2.
    exhausted: str | None = None

    for name, module_path in SCORERS.items():
        # Above the deadline check, deliberately: a carried scorer costs no latency, so an
        # exhausted budget must not convert it into "not scored". That would take a result
        # the pass already had and throw it away for want of time it never spent.
        if name not in wanted:
            previous = stored.get(name)
            results.append(
                carry(previous, as_of, onto)
                if previous is not None and as_of is not None
                else unavailable(name, "not re-scored, and no stored result was available to carry")
            )
            continue
        if exhausted is None:
            try:
                ctx.budget.check_deadline()
            except BudgetExceeded as exc:
                exhausted = str(exc)
                if manifest is not None:
                    manifest.degradation.append(
                        f"stopped scoring after {ran} of {len(wanted)}: {exhausted}"
                    )
        if exhausted is not None:
            results.append(unavailable(name, f"not scored: {exhausted}"))
            continue
        ran += 1
        func = load_scorer(module_path)
        if func is None:
            results.append(unavailable(name, "scorer not implemented yet"))
            continue
        started = time.perf_counter()
        try:
            results.append(_call(func, route, segments, ctx, etas, results))
        except Exception as exc:  # a scorer must never take the whole plan down
            results.append(unavailable(name, f"scorer failed: {type(exc).__name__}: {exc}"))
        if manifest is not None:
            manifest.record(ToolCall(tool=name, elapsed_s=time.perf_counter() - started))

    for name, reason in NOT_YET_IMPLEMENTED.items():
        results.append(unavailable(name, reason))

    for result in results:
        for entry in result.coverage:
            ctx.coverage.record(entry)
    return results


__all__ = [
    "NOT_YET_IMPLEMENTED",
    "PRIOR_DEPENDENCIES",
    "SCORERS",
    "StalePrior",
    "UngroundedCarry",
    "UnknownScorer",
    "carry",
    "closure",
    "load_scorer",
    "run_scorers",
]
