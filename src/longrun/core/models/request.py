"""What the user asked for (scope 6.1, 6.4).

Constraints are hard and distinct from preferences: a via point or an arrive-by time is
not a matter of taste and cannot be traded away by the arbitration in scope 8.4. Per-run
preference overrides ride along here too, and do not persist unless confirmed (scope 6.3).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from longrun.core.models.geometry import LatLon

EntryMode = Literal["generate", "repair"]


class TimeWindow(BaseModel):
    """A range of acceptable start times, swept by `start_time_optimizer` (scope 7.7)."""

    model_config = ConfigDict(frozen=True)

    earliest: time
    latest: time

    @model_validator(mode="after")
    def _check_order(self) -> TimeWindow:
        if self.latest < self.earliest:
            raise ValueError("start window ends before it begins")
        return self


class TimeConstraints(BaseModel):
    """Hard time limits. Violations are hard flags, never soft ones (scope 8.3)."""

    model_config = ConfigDict(frozen=True)

    arrive_by: datetime | None = None
    earliest_start: datetime | None = None
    total_budget: timedelta | None = None


class LockedRange(BaseModel):
    """A stretch the loop may not reroute (scope 6.4).

    Set explicitly, or automatically when the user picks between alternatives — a choice
    already made should not be silently re-opened on the next iteration.
    """

    model_config = ConfigDict(frozen=True)

    start_m: float = Field(ge=0)
    end_m: float = Field(ge=0)
    reason: str | None = None

    @model_validator(mode="after")
    def _check_order(self) -> LockedRange:
        if self.end_m <= self.start_m:
            raise ValueError("locked range must have positive length")
        return self


class PlanRequest(BaseModel):
    """A plan to produce, in either entry mode (scope 6.1)."""

    mode: EntryMode = "generate"
    date: date
    start: LatLon | None = None
    end: LatLon | None = None
    via: list[LatLon] = Field(default_factory=list)
    loop: bool = False
    start_time: time | None = None
    #: Hours from UTC at the route's location. Pinned rather than looked up: there is no
    #: timezone database in this project's dependencies, and a scorer that guesses says so.
    utc_offset_hours: float | None = None
    start_window: TimeWindow | None = None
    target_distance_km: float | None = Field(default=None, gt=0)
    distance_tolerance_pct: float = Field(default=10.0, ge=0)
    time_constraints: TimeConstraints = TimeConstraints()
    avoid_names: list[str] = Field(default_factory=list)
    avoid_polygons: list[dict[str, Any]] = Field(default_factory=list)
    locked: list[LockedRange] = Field(default_factory=list)
    overrides: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_mode(self) -> PlanRequest:
        if self.mode == "generate" and (self.start is None or self.end is None):
            raise ValueError("generate mode needs a start and an end")
        if self.start_time and self.start_window:
            raise ValueError("give a start time or a start window, not both")
        return self
