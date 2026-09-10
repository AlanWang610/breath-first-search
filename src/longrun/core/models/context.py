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

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.data.base import Cache, LayerStore, RasterStore
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

    deadline: datetime | None = None
    api_calls_max: int = 200
    imagery_tiles_max: int = 10
    #: Windowed COG reads over `/vsicurl/`. Metered separately from API calls because
    #: they are a different kind of cost - content-addressed, immutable, and cached by
    #: GDAL rather than by us - but metered, because a DSM over a 100 km corridor is
    #: tens of range requests per tile and nothing else would notice.
    raster_windows_max: int = 500
    api_calls_used: int = 0
    imagery_tiles_used: int = 0
    raster_windows_used: int = 0

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

    def check_deadline(self, clock: Clock) -> None:
        if self.deadline is not None and clock.now() > self.deadline:
            raise BudgetExceeded(f"plan deadline {self.deadline.isoformat()} passed")


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
    #: Hours to add to a naive plan time to get UTC. `None` means nobody said, and a
    #: scorer that needs it derives one from the route's longitude and reports having
    #: guessed - an hour of error is fifteen degrees of solar azimuth, which moves a
    #: shadow across the street, so this is not a detail that may be assumed silently.
    utc_offset_hours: float | None = None
