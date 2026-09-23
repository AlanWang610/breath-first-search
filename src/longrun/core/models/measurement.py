"""What a scorer returns (scope 3.2, 3.4, 8.3, 8.4).

Scorers measure; they never attach a sign. A `SegmentMeasurement` carries unsigned
values — irradiance, minutes to water, an LTS level — and the preference profile
(scope 6.3) is the only thing that decides whether a value is good or bad. The one
exception is a `Flag`, which marks a threshold crossing: soft thresholds come from the
profile, hard thresholds are fixed safety floors that a user may raise but never lower.
"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from longrun.core.models.coverage import CoverageEntry
from longrun.core.models.waypoint import PlanWaypoint

#: Per-segment severity, always in [0, 1]. Comparable within a scorer, not across them.
Severity = Annotated[float, Field(ge=0.0, le=1.0)]


class Tier(IntEnum):
    """Arbitration tiers, lexicographic (scope 8.4).

    Lower is more important. A shadier alternative never wins against a hard-hostility
    alternative, regardless of weights — which is why this is an ordering and not a
    coefficient.
    """

    SAFETY = 0
    PHYSIOLOGICAL = 1
    COMFORT = 2


class FlagKind(IntEnum):
    """Soft flags are preference-driven; hard flags are safety floors (scope 6.3, 8.3)."""

    SOFT = 0
    HARD = 1


class Flag(BaseModel):
    """A threshold crossing on one segment, with a machine-stable reason.

    `reason_code` is what tests and golden files assert on; `detail` is prose for the
    plan sheet and may change freely without breaking a regression test.
    """

    model_config = ConfigDict(frozen=True)

    scorer: str
    segment_id: str
    kind: FlagKind
    tier: Tier
    severity: Severity
    reason_code: str
    detail: str | None = None


class SegmentMeasurement(BaseModel):
    """Unsigned measurements for one segment.

    `values` is deliberately open: each scorer documents its own keys, and the plan
    sheet renders them without needing to know what they mean. `confidence` degrades
    when inputs were missing rather than the measurement being dropped (scope 12).
    """

    model_config = ConfigDict(frozen=True)

    segment_id: str
    values: dict[str, float | int | str | bool | None] = Field(default_factory=dict)
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0


class Regrounded(BaseModel):
    """What re-keying a carried result onto a changed segmentation kept and lost.

    Scope 3.6's rule in a new place. A partial re-score across a geometry edit carries the
    scorers it did not re-run, and a carried measurement is keyed on a `segment_id` that
    was an index into the segmentation the *old* line had. Some of those ids still name the
    same ground and some do not, and a result that cannot say which is a plan sheet
    reporting a measurement of a street the runner will not be on (ADR 0032).

    Three counts rather than two, because the reasons differ and a reader needs to tell
    them apart. A measurement dropped because its segment is gone describes ground the edit
    removed. A route total dropped - `ROUTE_SUMMARY_ID`, a `start@07:00` sweep row, a
    `meet#0` crew point - describes the whole route, and the whole route is what changed;
    nothing is wrong with that ground, the number is simply about a different line.
    """

    model_config = ConfigDict(frozen=True)

    measurements_kept: int = 0
    measurements_dropped: int = 0
    #: Measurements whose id never named a segment at all.
    route_totals_dropped: int = 0
    flags_kept: int = 0
    flags_dropped: int = 0
    waypoints_kept: int = 0
    waypoints_dropped: int = 0

    @property
    def lost_anything(self) -> bool:
        return bool(
            self.measurements_dropped
            or self.route_totals_dropped
            or self.flags_dropped
            or self.waypoints_dropped
        )

    def describe(self) -> str:
        """One line for a terminal or a sheet, in the terms a reader asks in."""
        parts = [f"{self.measurements_kept} measurement(s) kept"]
        if self.measurements_dropped:
            parts.append(f"{self.measurements_dropped} on ground that is no longer on the route")
        if self.route_totals_dropped:
            parts.append(f"{self.route_totals_dropped} route total(s) about the old line")
        if self.flags_dropped:
            parts.append(f"{self.flags_dropped} flag(s) dropped")
        if self.waypoints_dropped:
            parts.append(f"{self.waypoints_dropped} waypoint(s) dropped")
        return ", ".join(parts)


class ScorerResult(BaseModel):
    """One scorer's output over a whole route (scope 3.4).

    No single aggregate score: the worst N segments with reasons are the product.
    """

    name: str
    measurements: list[SegmentMeasurement] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)
    coverage: list[CoverageEntry] = Field(default_factory=list)
    #: Places this scorer found, for scope 9's GPX and the course exports.
    #:
    #: A fourth sibling list rather than coordinates inside `SegmentMeasurement.values`,
    #: which is scalars-only by design. The practical consequence is that
    #: `expectation.measurements_hash` cannot see a waypoint, so recording one moves no
    #: golden content hash - and a waypoint is free to carry a real `datetime` and a nested
    #: `LatLon`, neither of which a measurement value may be.
    waypoints: list[PlanWaypoint] = Field(default_factory=list)
    #: Set when this result was not produced by the pass that returned it: the planned
    #: start of the pass that *was*. `None` means this pass measured it.
    #:
    #: A partial re-score (`run_scorers(only=...)`) carries the scorers it did not run, and
    #: a carried result that cannot say so is the whole failure mode of a refresh — a sheet
    #: claiming a fresh check nobody made. The claim belongs here because a `ScorerResult` is
    #: exactly the thing that was or was not re-run.
    #:
    #: Not a coverage entry, which was the obvious home and is the wrong one:
    #: `cli/export.py` already reads source-keyed `unchecked()` as a statement about a
    #: scorer, so a carried marker there would flip carried scorers to "unavailable" in
    #: `longrun summary` — a lie in the opposite direction. And not `ToolCall.cached`, which
    #: is written by `cache.fetch` and means "this external call was served from SQLite": a
    #: carried scorer makes no external call, so marking one would mean fabricating a
    #: `ToolCall` for work that did not happen.
    #:
    #: A `datetime` rather than a `bool` because `lighting` at 05:00 and at 19:00 are
    #: different answers, and a reader needs to know how stale rather than merely that it is.
    carried_from: datetime | None = None
    #: Set when this result was carried across a segmentation that **moved**, and says what
    #: survived the move. See `core.geo.segments.map_segments` and ADR 0032.
    #:
    #: `None` is not "nothing was dropped". It means the question did not arise: either this
    #: pass measured the result itself, or it carried it across a line that did not change -
    #: which is every refresh, because a refresh passes no router (ADR 0030). `carried_from`
    #: is what distinguishes those two, and a `Regrounded` with every `dropped` at zero is a
    #: third thing again: an edit happened and this scorer lost nothing to it.
    regrounded: Regrounded | None = None

    @property
    def carried(self) -> bool:
        """Whether this result was brought forward rather than measured by this pass."""
        return self.carried_from is not None

    def worst(self, n: int = 5) -> list[Flag]:
        """The n most severe flags, hard before soft, ties broken by segment id.

        The tie-break is not cosmetic: without a total order, golden `expected.json`
        files reorder between runs and every regression test becomes flaky.
        """
        return sorted(
            self.flags,
            key=lambda f: (int(f.tier), -int(f.kind), -f.severity, f.segment_id),
        )[:n]

    @property
    def has_hard_flag(self) -> bool:
        return any(f.kind is FlagKind.HARD for f in self.flags)
