"""The preference profile: the only place a sign is attached to a measurement (scope 6.3).

Scorers measure. This is what maps a measurement to a cost, with a user-supplied sign and
weight. Two rules are structural rather than incidental:

* **Sun is not assumed bad.** With both `sun` weights at 0 the shade fraction is reported
  and not scored. Heat stress is scored regardless, because it is physiology, not taste.
* **Safety floors are not in this model at all.** They live in `core.preferences.floors`
  precisely so that no profile edit can reach them; a user may raise a floor, never lower it.

Every entry carries provenance. `stated` beats `inferred` beats `default`, and `inferred`
comes only from post-run feedback at low weight — because a preference the user asserted
must never be silently overwritten by one the system guessed.
"""

from __future__ import annotations

from datetime import date
from enum import IntEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from longrun.core.pacing.curves import PacingCurves

#: -1..+1 throughout: negative avoids, positive seeks, 0 reports without scoring.
Weight = Annotated[float, Field(ge=-1.0, le=1.0)]


class Provenance(IntEnum):
    """Higher wins. See `PreferenceEntry.supersedes`."""

    DEFAULT = 0
    INFERRED = 1
    STATED = 2


class PreferenceEntry[T](BaseModel):
    """One profile axis, with where its value came from."""

    model_config = ConfigDict(frozen=True)

    value: T
    weight: Weight = 0.0
    provenance: Provenance = Provenance.DEFAULT
    updated: date | None = None

    @field_validator("provenance", mode="before")
    @classmethod
    def _name_or_number(cls, value: object) -> object:
        """Accept `stated` as well as `2`.

        Profiles are hand-edited YAML — the golden routes pin one, and scope 6.3 expects a
        user to keep their own. A file that has to say `provenance: 2` is a file whose
        diffs cannot be reviewed, and the round trip through `save_profile` writes the
        integer either way.
        """
        if isinstance(value, str) and not value.isdigit():
            try:
                return Provenance[value.strip().upper()]
            except KeyError:
                names = ", ".join(p.name.lower() for p in Provenance)
                raise ValueError(f"unknown provenance {value!r}; expected one of {names}") from None
        return value

    def supersedes(self, other: PreferenceEntry[T]) -> bool:
        """Whether this entry may overwrite `other` (scope 6.3)."""
        return self.provenance >= other.provenance


class SunPreference(BaseModel):
    """Weight applied below/above `pivot_c`, using temperature at ETA (scope 6.3).

    Deliberately not split by time of day: "sun in the morning" means "sun when it is
    cool", and the temperature at arrival is already known from pacing + microclimate.
    Seasonal and heat-acclimation cases fit the same three numbers.
    """

    model_config = ConfigDict(frozen=True)

    cool: Weight = 0.0
    hot: Weight = 0.0
    pivot_c: float = 22.0


class GradePreference(BaseModel):
    """Sustained grade limits in percent, plus whether hills are sought or avoided."""

    model_config = ConfigDict(frozen=True)

    max_climb_pct: float = Field(default=10.0, gt=0)
    max_descent_pct: float = Field(default=10.0, gt=0)
    hills: Weight = 0.0


SurfaceKind = Literal["paved", "dirt", "mixed"]


class PreferenceProfile(BaseModel):
    """The scope 6.3 table. Persisted as versioned YAML at ~/.longrun/profile.yaml."""

    version: int = 1
    sun: PreferenceEntry[SunPreference] = PreferenceEntry(value=SunPreference())
    water_gap_max_min: PreferenceEntry[float] = PreferenceEntry(value=90.0)
    toilet_gap_max_min: PreferenceEntry[float] = PreferenceEntry(value=150.0)
    carry_capacity_ml: PreferenceEntry[float] = PreferenceEntry(value=500.0)
    traffic_tolerance: PreferenceEntry[int] = PreferenceEntry(value=2)
    surface: PreferenceEntry[SurfaceKind] = PreferenceEntry(value="mixed", weight=0.1)
    grade: PreferenceEntry[GradePreference] = PreferenceEntry(value=GradePreference())
    detour_tolerance_pct: PreferenceEntry[float] = PreferenceEntry(value=10.0)
    stops_tolerance: PreferenceEntry[float] = PreferenceEntry(value=3.0)
    darkness_tolerance: PreferenceEntry[bool] = PreferenceEntry(value=False)
    scenery_vs_directness: PreferenceEntry[float] = PreferenceEntry(value=0.0)
    #: Scope 6.3's table has listed this since the first draft and the model has never had
    #: it, so a history-derived curve had nowhere to live and `pacing_model`'s `curves`
    #: argument was never passed by anything.
    #:
    #: Two provenance vocabularies meet here and they answer different questions.
    #: `PacingCurves.provenance` is "population" or "history" - *what measured this*.
    #: `PreferenceEntry.provenance` is DEFAULT/INFERRED/STATED - *who said so*. A measured
    #: curve is INFERRED on the outside and "history" on the inside: derived, and therefore
    #: overridable by a runner who says otherwise, which is scope 6.3's rule that `stated`
    #: always beats `inferred`.
    pacing: PreferenceEntry[PacingCurves] = PreferenceEntry(value=PacingCurves())

    @property
    def scores_sun(self) -> bool:
        """False when both weights are 0: shade is then reported, never costed."""
        return self.sun.value.cool != 0.0 or self.sun.value.hot != 0.0
