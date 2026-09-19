"""Re-score a stored plan against a new date (scope 7.8).

`longrun refresh plan.json` has been advertised in `cli/__init__.py` since the first commit
and did not exist. What did exist was the `refresh_plan` MCP tool, which re-ran all
twenty-one scorers, returned a dict, and wrote nothing anywhere.

A refresh answers *"is this still true today"*. It does not re-route: re-drawing the line
would answer a different question, and the user would get a route they were never shown. So
the geometry is identical by construction, which is why what changed is reported over flags
(`result_diff`) rather than over geometry.

Pure of environment and filesystem - the context is passed in - so the CLI command and the
MCP tool are two callers of one function rather than two implementations of one idea.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, time

from longrun.core.models.context import ScorerContext
from longrun.core.models.coverage import CoverageManifest
from longrun.core.models.plan import Manifest, Plan
from longrun.core.plan.diff import ResultDiff, result_diff
from longrun.core.plan.pipeline import build_plan, score_once
from longrun.core.scorers.freshness import rescore_set
from longrun.core.scorers.registry import closure


@dataclass(frozen=True)
class RefreshDelta:
    """What a refresh found, in the terms a refresh can actually speak about.

    Deliberately not a route diff. A refresh passes no router, so the line cannot have moved
    and a geometry comparison says "same line" every time - which reads as "nothing changed"
    next to a plan whose trail has just closed.
    """

    rescored: tuple[str, ...] = ()
    carried: tuple[str, ...] = ()
    results: ResultDiff = field(default_factory=ResultDiff)
    residual_before: int = 0
    residual_after: int = 0
    verify_before: str | None = None
    verify_after: str | None = None
    finish_before: datetime | None = None
    finish_after: datetime | None = None
    #: Sources that answered before and did not now, and the other way round.
    stopped_answering: tuple[str, ...] = ()
    started_answering: tuple[str, ...] = ()
    #: Snapshot pins that differ between the stored plan and this pass. The honest place to
    #: report that a *carried* scorer may be stale for a reason the date cannot express: a
    #: rebuilt OSM extract moves `legality` even though legality is time-independent.
    pins_changed: tuple[str, ...] = ()

    @property
    def new_hard_flags(self) -> tuple[str, ...]:
        """Reason codes that hard-flag now and did not before - the reason to run this."""
        return tuple(
            code
            for delta in self.results.scorers
            for code in delta.appeared
            if delta.hard_after > delta.hard_before
        )

    def lines(self) -> list[str]:
        out = [
            f"re-scored {len(self.rescored)}, carried {len(self.carried)}",
            self.results.summary(),
        ]
        if self.residual_after != self.residual_before:
            out.append(f"residual flags: {self.residual_before} -> {self.residual_after}")
        if self.verify_after != self.verify_before:
            out.append(f"verification: {self.verify_before} -> {self.verify_after}")
        if self.finish_before and self.finish_after:
            out.append(
                f"finish: {self.finish_before:%Y-%m-%d %H:%M} -> {self.finish_after:%Y-%m-%d %H:%M}"
            )
        if self.stopped_answering:
            out.append(f"stopped answering: {', '.join(self.stopped_answering)}")
        if self.started_answering:
            out.append(f"started answering: {', '.join(self.started_answering)}")
        if self.pins_changed:
            out.append(
                f"snapshot moved ({', '.join(self.pins_changed)}), so a carried scorer may "
                f"be stale for a reason the date does not express"
            )
        return out


@dataclass(frozen=True)
class Refreshed:
    plan: Plan
    delta: RefreshDelta


def _answered(coverage: CoverageManifest) -> set[str]:
    return {f"{e.source}/{e.kind}" for e in coverage.checked()}


def _measured_at(stored: Plan) -> datetime:
    """The planned start the stored results were measured against.

    The ETA vector first, because that is what the scorers were actually handed. The request
    is the fallback, and its `start_time` is optional - `PlanRequest` allows a window
    instead - so midnight stands in rather than the whole refresh failing over a field that
    only matters for saying how stale a carried result is.
    """
    if stored.etas:
        return stored.etas[0]
    return datetime.combine(stored.request.date, stored.request.start_time or time.min)


def rescore_plan(
    stored: Plan,
    ctx: ScorerContext,
    *,
    start_at: datetime,
    only: Collection[str] | None = None,
    manifest: Manifest | None = None,
    resample_elevation: bool = False,
) -> Refreshed:
    """Re-score the date-sensitive scorers; carry the rest, and say which was which.

    `only=None` means `freshness.rescore_set()`. A caller wanting to report the widening
    should call `closure()` itself first - `run_scorers` refuses an unclosed set rather than
    expanding one quietly.
    """
    wanted = closure(rescore_set() if only is None else only)
    as_of = _measured_at(stored)
    request = stored.request.model_copy(
        update={"date": start_at.date(), "start_time": start_at.time()}
    )

    # A *fresh* manifest, seeded with the stored plan's pins rather than the stored manifest
    # itself. `Manifest.record` is a bare append and `total_elapsed_s` sums, so reusing the
    # stored one accumulates forever and makes scope 6.4's latency budget unmeasurable after
    # the first refresh. The carried scorers' original timings are deliberately left out:
    # `total_elapsed_s` should answer "what did this run cost", and that seven scorers did
    # not run is already reported by `carried_from` on their own results.
    manifest = manifest or Manifest(snapshot=stored.manifest.snapshot.model_copy(deep=True))

    scored = score_once(
        stored.route,
        request,
        ctx,
        start_at=start_at,
        manifest=manifest,
        only=wanted,
        carried=stored.results,
        carried_as_of=as_of,
        # Terrain does not change between Tuesday and Friday, and reading a DEM this machine
        # may not have would overwrite the stored profile with nulls. See M10.4.
        elevations=None if resample_elevation else [point.ele_m for point in stored.route.points],
    )

    plan = build_plan(
        scored,
        request,
        profile=stored.profile,
        coverage=scored.coverage,
        manifest=manifest,
        # A refresh restates the same plan as of a new date; it does not mint a new one.
        plan_id=stored.id,
    ).model_copy(
        # `build_plan` writes 14 of `Plan`'s 17 fields and the loop patches the other three
        # back. Nothing patched them here, so a refresh discarded the trade-offs the user had
        # already resolved and flipped `needs_input` to `complete`.
        update={
            "trade_offs": list(stored.trade_offs),
            "warnings": list(stored.warnings),
            "status": stored.status,
        }
    )

    was, now = _answered(stored.coverage), _answered(plan.coverage)
    pins_before = stored.manifest.snapshot.model_dump()
    pins_after = plan.manifest.snapshot.model_dump()
    delta = RefreshDelta(
        rescored=tuple(name for name in (r.name for r in plan.results) if name in wanted),
        carried=tuple(r.name for r in plan.results if r.carried),
        results=result_diff(stored.results, plan.results),
        residual_before=len(stored.residual_flags),
        residual_after=len(plan.residual_flags),
        verify_before=stored.verify.summary() if stored.verify else None,
        verify_after=plan.verify.summary() if plan.verify else None,
        finish_before=stored.finish_time,
        finish_after=plan.finish_time,
        stopped_answering=tuple(sorted(was - now)),
        started_answering=tuple(sorted(now - was)),
        pins_changed=tuple(
            sorted(key for key in pins_before if pins_before[key] != pins_after.get(key))
        ),
    )
    return Refreshed(plan=plan, delta=delta)


__all__ = ["Refreshed", "RefreshDelta", "rescore_plan"]
