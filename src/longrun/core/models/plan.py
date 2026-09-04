"""The stored plan (scope 6.4, 9).

The plan schema is also the API contract (scope 10.3), so everything here stays
serializable. A plan plus its manifest is what `refresh_plan` and `route_diff` operate on
later, which is why the data-snapshot pins matter: without them a difference between two
plans cannot be attributed to a routing change rather than a new OSM extract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from longrun.core.models.coverage import CoverageManifest
from longrun.core.models.geometry import Route, Segment
from longrun.core.models.measurement import Flag, ScorerResult
from longrun.core.models.profile import PreferenceProfile
from longrun.core.models.request import PlanRequest

PlanStatus = Literal["complete", "needs_input", "failed"]


class SnapshotPins(BaseModel):
    """Which vintage of every source produced this plan (scope 6.4).

    Recorded so a plan is reproducible and so differences between two plans are
    attributable. Free-form by design: adapters register their own keys.
    """

    osm_extract_date: str | None = None
    hpms_vintage: str | None = None
    dem_resolution_m: float | None = None
    canopy_version: str | None = None
    gtfs_feed_versions: dict[str, str] = Field(default_factory=dict)
    adapter_versions: dict[str, str] = Field(default_factory=dict)
    extra: dict[str, str] = Field(default_factory=dict)


class ToolCall(BaseModel):
    """One tool invocation, timed. Feeds the budget report in the plan sheet."""

    model_config = ConfigDict(frozen=True)

    tool: str
    elapsed_s: float = Field(ge=0)
    args_hash: str | None = None
    cached: bool = False


class Manifest(BaseModel):
    """Provenance and cost of a plan (scope 9)."""

    snapshot: SnapshotPins = SnapshotPins()
    tool_calls: list[ToolCall] = Field(default_factory=list)
    api_calls_used: int = 0
    imagery_tiles_used: int = 0
    degradation: list[str] = Field(default_factory=list)

    def record(self, call: ToolCall) -> None:
        self.tool_calls.append(call)

    @property
    def total_elapsed_s(self) -> float:
        """Measured against the ~3-minute target in scope 6.4."""
        return sum(c.elapsed_s for c in self.tool_calls)


class TradeOff(BaseModel):
    """A same-tier conflict the loop refuses to resolve silently (scope 8.4).

    Both options are surfaced with a one-line comparison and the user chooses; the choice
    then auto-locks the segment.
    """

    model_config = ConfigDict(frozen=True)

    segment_id: str
    option_a: str
    option_b: str
    comparison: str


class Plan(BaseModel):
    """Everything a plan sheet is rendered from, and everything a refresh needs."""

    id: str
    request: PlanRequest
    route: Route
    segments: list[Segment] = Field(default_factory=list)
    results: list[ScorerResult] = Field(default_factory=list)
    etas: list[datetime] = Field(default_factory=list)
    residual_flags: list[Flag] = Field(default_factory=list)
    trade_offs: list[TradeOff] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    coverage: CoverageManifest = CoverageManifest()
    manifest: Manifest = Manifest()
    profile: PreferenceProfile = PreferenceProfile()
    status: PlanStatus = "complete"

    @property
    def finish_time(self) -> datetime | None:
        return self.etas[-1] if self.etas else None

    def result(self, scorer: str) -> ScorerResult | None:
        return next((r for r in self.results if r.name == scorer), None)
