"""The pacing curves a plan runs on (scope 6.2).

Either derived from a user's own history — grade-adjusted pace per 2% grade bin, fatigue
drift from their longest efforts, surface and descent sensitivity — or the population
defaults. The distinction is carried in `provenance` and surfaced in the plan sheet,
because scope 12 requires an extrapolated pace to be labelled as a guess.

`longest_effort_m` is the honesty mechanism: past it, the model is extrapolating beyond
anything the user has actually done, and the plan has to say so.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from longrun.core.pacing.population import (
    DEFAULT_FATIGUE_DRIFT_PCT_PER_10KM,
    DEFAULT_FLAT_SPEED_MS,
    FATIGUE_ONSET_M,
    fatigue_factor,
    grade_factor,
)

CurveProvenance = Literal["population", "history"]

#: Multiplier applied to sustainable speed on unpaved surfaces, when no history says
#: otherwise. Scope 6.2 derives this per user from surface sensitivity.
DEFAULT_UNPAVED_FACTOR = 0.93


class PacingCurves(BaseModel):
    """Everything the pacing model needs about a particular runner."""

    model_config = ConfigDict(frozen=True)

    flat_speed_ms: float = Field(default=DEFAULT_FLAT_SPEED_MS, gt=0)
    fatigue_drift_pct_per_10km: float = Field(default=DEFAULT_FATIGUE_DRIFT_PCT_PER_10KM, ge=0)
    fatigue_onset_m: float = Field(default=FATIGUE_ONSET_M, ge=0)
    unpaved_factor: float = Field(default=DEFAULT_UNPAVED_FACTOR, gt=0, le=1.0)
    provenance: CurveProvenance = "population"

    #: Longest single effort in the user's history, in metres. None with no history.
    longest_effort_m: float | None = None

    #: Grade-adjusted speed per 2% grade bin, keyed by the bin's lower bound in percent.
    #: When present it overrides the Minetti curve, because a measured curve beats a
    #: population one.
    speed_by_grade_bin: dict[str, float] = Field(default_factory=dict)

    def speed_ms(self, gradient: float, distance_m: float, unpaved: bool = False) -> float:
        """Sustainable speed at a gradient, this far into the run."""
        speed = self._grade_speed(gradient)
        speed *= fatigue_factor(
            distance_m,
            drift_pct_per_10km=self.fatigue_drift_pct_per_10km,
            onset_m=self.fatigue_onset_m,
        )
        if unpaved:
            speed *= self.unpaved_factor
        return max(speed, 0.1)

    def _grade_speed(self, gradient: float) -> float:
        """Measured curve where the user has one, Minetti where they do not."""
        if self.speed_by_grade_bin:
            bin_key = f"{int(gradient * 100 // 2) * 2:+d}"
            measured = self.speed_by_grade_bin.get(bin_key)
            if measured is not None:
                return measured
        return self.flat_speed_ms * grade_factor(gradient)

    def extrapolates_beyond_history(self, distance_m: float) -> bool:
        """Whether a plan of this length is past anything the runner has done.

        Scope 6.2 puts the threshold at 50%: with no effort at least half the target
        distance, the extrapolation is unreliable and the plan must say so.
        """
        if self.provenance == "population" or self.longest_effort_m is None:
            return True
        return self.longest_effort_m < distance_m * 0.5


POPULATION_CURVES = PacingCurves()
