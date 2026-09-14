"""What a scorer returns (scope 3.2, 3.4, 8.3, 8.4).

Scorers measure; they never attach a sign. A `SegmentMeasurement` carries unsigned
values — irradiance, minutes to water, an LTS level — and the preference profile
(scope 6.3) is the only thing that decides whether a value is good or bad. The one
exception is a `Flag`, which marks a threshold crossing: soft thresholds come from the
profile, hard thresholds are fixed safety floors that a user may raise but never lower.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from longrun.core.models.coverage import CoverageEntry

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


class ScorerResult(BaseModel):
    """One scorer's output over a whole route (scope 3.4).

    No single aggregate score: the worst N segments with reasons are the product.
    """

    name: str
    measurements: list[SegmentMeasurement] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)
    coverage: list[CoverageEntry] = Field(default_factory=list)

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
