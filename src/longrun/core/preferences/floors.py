"""Safety floors: outside the preference profile, and not lowerable (scope 6.3, 8.3).

These are deliberately not fields on `PreferenceProfile`. If they were, every code path
that edits a preference would also be a path that could disable a safety limit — a
conversational "I don't mind traffic" would be one LLM-proposed field update away from
turning off the LTS 4 hard flag.

**A user may raise a floor, never lower it.** "Raise" means make it stricter, which for a
threshold expressed as a maximum means a smaller number. Every setter here enforces the
direction, so the rule holds structurally rather than by review.

Scope 8.3's hard thresholds live here; the soft thresholds marked "(profile)" live in the
profile, where the user owns them.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

#: Wet-bulb globe temperature above which a plan hard-fails (scope 8.3).
WBGT_HARD_C = 30.0

#: Level of traffic stress that is always a hard flag, whatever the profile says.
LTS_HARD = 4

#: Highest value `traffic_tolerance` may take: one below the hard LTS, so that LTS 4 can
#: never be tolerated by raising the preference.
MAX_TRAFFIC_TOLERANCE = LTS_HARD - 1

#: An unsignalized crossing of a road at or above this posted speed is a hard flag.
CROSSING_HARD_SPEED_KPH = 40.0

#: Longest dry gap permitted at projected pace, in minutes (scope 8.3).
DRY_GAP_HARD_MIN = 150.0


class FloorViolation(ValueError):
    """An attempt to make a safety limit less strict."""


class SafetyFloors(BaseModel):
    """The fixed limits, with per-plan tightening allowed.

    Construct with the defaults for the shipped floors; pass stricter values to tighten.
    Any attempt to loosen is rejected rather than silently clamped, because quietly
    ignoring a user's setting is its own kind of bug.

    Note the two entry points: direct construction surfaces pydantic's `ValidationError`
    (with the `FloorViolation` as its cause), while `tighten` raises `FloorViolation`
    itself. Use `tighten` where the caller means to catch it.
    """

    model_config = ConfigDict(frozen=True)

    wbgt_hard_c: float = Field(default=WBGT_HARD_C)
    lts_hard: int = Field(default=LTS_HARD)
    crossing_hard_speed_kph: float = Field(default=CROSSING_HARD_SPEED_KPH)
    dry_gap_hard_min: float = Field(default=DRY_GAP_HARD_MIN)

    @model_validator(mode="after")
    def _no_loosening(self) -> SafetyFloors:
        if self.wbgt_hard_c > WBGT_HARD_C:
            raise FloorViolation(
                f"WBGT floor may be raised (stricter, lower than {WBGT_HARD_C} C) "
                f"but not lowered to {self.wbgt_hard_c}"
            )
        if self.lts_hard > LTS_HARD:
            raise FloorViolation(
                f"LTS {LTS_HARD} is always a hard flag; cannot relax to {self.lts_hard}"
            )
        if self.crossing_hard_speed_kph > CROSSING_HARD_SPEED_KPH:
            raise FloorViolation(
                f"unsignalized-crossing speed floor may not be raised above "
                f"{CROSSING_HARD_SPEED_KPH} kph"
            )
        if self.dry_gap_hard_min > DRY_GAP_HARD_MIN:
            raise FloorViolation(f"dry-gap floor may not be relaxed beyond {DRY_GAP_HARD_MIN} min")
        return self

    @classmethod
    def tighten(cls, **overrides: float | int) -> SafetyFloors:
        """Build stricter floors, raising `FloorViolation` directly on any loosening."""
        try:
            return cls(**overrides)  # type: ignore[arg-type]
        except ValidationError as exc:
            for error in exc.errors():
                cause = error.get("ctx", {}).get("error")
                if isinstance(cause, FloorViolation):
                    raise cause from exc
            raise


DEFAULT_FLOORS = SafetyFloors()


def check_traffic_tolerance(value: int) -> int:
    """Reject a `traffic_tolerance` that would tolerate the hard LTS level."""
    if value > MAX_TRAFFIC_TOLERANCE:
        raise FloorViolation(
            f"traffic_tolerance {value} would suppress the LTS {LTS_HARD} hard flag; "
            f"the maximum is {MAX_TRAFFIC_TOLERANCE}"
        )
    return value


def check_water_gap(minutes: float, floors: SafetyFloors = DEFAULT_FLOORS) -> float:
    """Reject a soft water-gap preference looser than the hard floor."""
    if minutes > floors.dry_gap_hard_min:
        raise FloorViolation(
            f"water_gap_max_min {minutes} exceeds the hard dry-gap floor of "
            f"{floors.dry_gap_hard_min} min"
        )
    return minutes
