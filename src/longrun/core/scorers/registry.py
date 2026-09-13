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
from datetime import datetime
from typing import Any, cast

from longrun.core.models.context import BudgetExceeded, ScorerContext
from longrun.core.models.measurement import ScorerResult
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


def run_scorers(
    route: Any,
    segments: list[Any],
    ctx: ScorerContext,
    etas: list[datetime],
    manifest: Manifest | None = None,
) -> list[ScorerResult]:
    """Run every scorer that exists; report every one that does not.

    Each is timed into the manifest. Scope 6.4 wants per-tool elapsed time recorded so the
    ~3-minute budget is measured rather than assumed, and until M2 `Manifest.tool_calls`
    existed with nothing writing to it - so "which scorer is slow" was a question only a
    profiler could answer, and only on a machine that had one.
    """
    results: list[ScorerResult] = []
    #: Set once the plan outruns scope 6.4's latency target. Every scorer after that is
    #: reported as not run rather than dropped, because a sheet that is quietly shorter is
    #: the failure scope 3.6 exists to prevent - and the degradation is named in the
    #: manifest, which is the field scope 6.4 asks for and nothing wrote until M5.2.
    exhausted: str | None = None

    for name, module_path in SCORERS.items():
        if exhausted is None:
            try:
                ctx.budget.check_deadline()
            except BudgetExceeded as exc:
                exhausted = str(exc)
                if manifest is not None:
                    manifest.degradation.append(
                        f"stopped scoring after {len(results)} of {len(SCORERS)}: {exhausted}"
                    )
        if exhausted is not None:
            results.append(unavailable(name, f"not scored: {exhausted}"))
            continue
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


__all__ = ["NOT_YET_IMPLEMENTED", "SCORERS", "load_scorer", "run_scorers"]
