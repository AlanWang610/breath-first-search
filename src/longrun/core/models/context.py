"""What every scorer is handed (scope 3.3, 6.4).

One context type, not per-scorer keyword arguments. That is what lets scorers be written
in any order by anyone and tested against nothing but synthetic geometry.

`Clock` is a protocol rather than a call to `datetime.now()` because scope 3.3 makes
everything time-aware: ETAs, store hours, daylight and forecasts are all evaluated at
projected arrival time. A scorer that reads the wall clock is untestable and makes its
golden route non-reproducible, so `core/` contains no concrete system clock at all —
`FrozenClock` lives here, and real time is injected from outside `core/` by the caller.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.data.base import Cache, FeatureSource, LayerStore, RasterStore
    from longrun.core.models.coverage import CoverageManifest
    from longrun.core.models.profile import PreferenceProfile


@runtime_checkable
class Clock(Protocol):
    """The only way core/ learns the time."""

    def now(self) -> datetime: ...


@dataclass(frozen=True)
class FrozenClock:
    """A fixed instant. The default in tests and in any reproducible plan."""

    instant: datetime

    def now(self) -> datetime:
        return self.instant


class BudgetExceeded(RuntimeError):
    """A plan hit its call or time ceiling (scope 6.4).

    Raised rather than silently degrading, because the caller decides which rung of the
    degradation ladder to drop to; the choice is recorded in the manifest.
    """


@dataclass
class Budget:
    """Hard caps per plan (scope 6.4).

    Real from the first milestone, so that the ~3-minute end-to-end target is measured
    early rather than discovered late.
    """

    #: Wall-clock seconds one plan may take - scope 6.4's "~3 min" - measured with a
    #: monotonic counter rather than against the injected `Clock`. Those are different
    #: questions and only one of them advances: the production clock is
    #: `FrozenClock(start_at)`, pinned at the route's start time so a plan is
    #: reproducible, so a deadline compared against it could never pass. This field
    #: replaced a `deadline: datetime` in M5.2 that nothing called and nothing could.
    latency_budget_s: float | None = None
    api_calls_max: int = 200
    imagery_tiles_max: int = 10
    #: Windowed COG reads over `/vsicurl/`. Metered separately from API calls because
    #: they are a different kind of cost - content-addressed, immutable, and cached by
    #: GDAL rather than by us - but metered, because a DSM over a 100 km corridor is
    #: tens of range requests per tile and nothing else would notice.
    raster_windows_max: int = 500
    #: Scope 4.1 fixes the LLM to five call sites and scope 6.4 budgets everything else,
    #: so this is the ceiling the scope implies and does not state. Twelve: one intent
    #: parse, up to five trade-off comparisons across five rounds, three elicitation
    #: questions, a preference proposal, and slack. Tier-4 extraction is capped
    #: separately, by the adapter fetch limits it already lives behind.
    model_calls_max: int = 12
    api_calls_used: int = 0
    imagery_tiles_used: int = 0
    raster_windows_used: int = 0
    model_calls_used: int = 0
    model_tokens_used: int = 0
    #: Set when the budget is constructed, which is when the plan starts.
    started_at: float = field(default_factory=time.perf_counter, repr=False)

    @property
    def elapsed_s(self) -> float:
        """How long this plan has been running. A duration, never a date."""
        return time.perf_counter() - self.started_at

    def spend_api_call(self, n: int = 1) -> None:
        if self.api_calls_used + n > self.api_calls_max:
            raise BudgetExceeded(f"external API budget exhausted ({self.api_calls_max} calls)")
        self.api_calls_used += n

    def spend_raster_window(self, n: int = 1) -> None:
        """Charge a remote windowed raster read (scope 6.4)."""
        if self.raster_windows_used + n > self.raster_windows_max:
            raise BudgetExceeded(
                f"remote raster budget exhausted ({self.raster_windows_max} windows)"
            )
        self.raster_windows_used += n

    def spend_imagery_tile(self, n: int = 1) -> None:
        if self.imagery_tiles_used + n > self.imagery_tiles_max:
            raise BudgetExceeded(f"imagery budget exhausted ({self.imagery_tiles_max} tiles)")
        self.imagery_tiles_used += n

    def spend_model_call(self, tokens: int = 0) -> None:
        """Charge one call at a fixed LLM call site (scope 4.1).

        Tokens are recorded and not capped: what a plan must be able to say is *how much*
        it asked a model for, and a cap on tokens would refuse a long answer halfway
        through, which is a worse failure than an expensive one.
        """
        if self.model_calls_used + 1 > self.model_calls_max:
            raise BudgetExceeded(f"model call budget exhausted ({self.model_calls_max} calls)")
        self.model_calls_used += 1
        self.model_tokens_used += max(0, tokens)

    def check_deadline(self) -> None:
        """Raise once the plan has outrun scope 6.4's latency target.

        Raised rather than degraded in place, for the reason `BudgetExceeded` gives: the
        caller picks the rung of the ladder to drop to, and records the choice.
        """
        if self.latency_budget_s is not None and self.elapsed_s > self.latency_budget_s:
            raise BudgetExceeded(
                f"plan latency budget exhausted ({self.latency_budget_s:.0f} s); "
                f"{self.elapsed_s:.0f} s elapsed"
            )


@dataclass
class ScorerContext:
    """Everything a scorer may reach for, and nothing else."""

    layers: LayerStore
    rasters: RasterStore
    cache: Cache
    clock: Clock
    coverage: CoverageManifest
    profile: PreferenceProfile
    budget: Budget = field(default_factory=Budget)
    snapshot: dict[str, str] = field(default_factory=dict)
    #: Jurisdiction adapters (scope 7.10). `None` rather than a `NullFeatureSource`
    #: default, because "no registry was configured" and "a registry, but nobody covers
    #: this county" are different claims and the sheet has to be able to make both.
    features: FeatureSource | None = None
    #: Hours to add to a naive plan time to get UTC. `None` means nobody said, and a
    #: scorer that needs it derives one from the route's longitude and reports having
    #: guessed - an hour of error is fifteen degrees of solar azimuth, which moves a
    #: shadow across the street, so this is not a detail that may be assumed silently.
    utc_offset_hours: float | None = None
