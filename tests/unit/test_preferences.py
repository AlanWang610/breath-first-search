"""Preference and safety-floor tests (scope 6.3).

The floor tests are the important ones. A safety limit that can be edited through the
same path as a taste preference is not a safety limit, and the whole point of keeping
floors out of `PreferenceProfile` is that the separation holds mechanically.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from longrun.core.models.profile import PreferenceEntry, PreferenceProfile, Provenance
from longrun.core.preferences.floors import (
    DRY_GAP_HARD_MIN,
    LTS_HARD,
    MAX_TRAFFIC_TOLERANCE,
    WBGT_HARD_C,
    FloorViolation,
    SafetyFloors,
    check_traffic_tolerance,
    check_water_gap,
)
from longrun.core.preferences.store import (
    ProfileError,
    apply_overrides,
    load_defaults,
    load_profile,
    merge,
    save_profile,
)

# --- shipped defaults -------------------------------------------------------


def test_defaults_yaml_parses_into_the_profile_model() -> None:
    """defaults.yaml is the scope 6.3 table; drift between it and the model is a bug."""
    profile = load_defaults()
    assert profile.water_gap_max_min.value == 90.0
    assert profile.toilet_gap_max_min.value == 150.0
    assert profile.carry_capacity_ml.value == 500.0
    assert profile.traffic_tolerance.value == 2
    assert profile.surface.value == "mixed"
    assert profile.grade.value.max_climb_pct == 10.0
    assert profile.detour_tolerance_pct.value == 10.0
    assert profile.stops_tolerance.value == 3.0
    assert profile.darkness_tolerance.value is False
    assert profile.scenery_vs_directness.value == 0.0


def test_every_shipped_entry_is_provenance_default() -> None:
    profile = load_defaults()
    for key in type(profile).model_fields:
        if key == "version":
            continue
        assert getattr(profile, key).provenance is Provenance.DEFAULT, key


def test_shipped_defaults_do_not_score_sun() -> None:
    """Scope 6.3: sun is reported, not costed, until the user says otherwise."""
    assert load_defaults().scores_sun is False


# --- safety floors ----------------------------------------------------------


def test_floors_may_be_tightened() -> None:
    assert SafetyFloors.tighten(wbgt_hard_c=27.0).wbgt_hard_c == 27.0
    assert SafetyFloors.tighten(dry_gap_hard_min=90.0).dry_gap_hard_min == 90.0


@pytest.mark.parametrize(
    ("field", "loosened"),
    [
        ("wbgt_hard_c", WBGT_HARD_C + 5),
        ("lts_hard", LTS_HARD + 1),
        ("crossing_hard_speed_kph", 100.0),
        ("dry_gap_hard_min", DRY_GAP_HARD_MIN + 60),
    ],
)
def test_no_floor_can_be_loosened(field: str, loosened: float) -> None:
    with pytest.raises(FloorViolation):
        SafetyFloors.tighten(**{field: loosened})


def test_direct_construction_still_refuses_to_loosen() -> None:
    """Even bypassing `tighten`, the model will not build a relaxed floor."""
    with pytest.raises(ValidationError):
        SafetyFloors(wbgt_hard_c=WBGT_HARD_C + 5)


def test_traffic_tolerance_cannot_reach_the_hard_lts_level() -> None:
    """Tolerating LTS 4 would silence a hard flag; the profile must not be able to."""
    assert check_traffic_tolerance(MAX_TRAFFIC_TOLERANCE) == MAX_TRAFFIC_TOLERANCE
    with pytest.raises(FloorViolation, match="hard flag"):
        check_traffic_tolerance(LTS_HARD)


def test_water_gap_preference_cannot_exceed_the_hard_floor() -> None:
    assert check_water_gap(120.0) == 120.0
    with pytest.raises(FloorViolation, match="hard dry-gap floor"):
        check_water_gap(DRY_GAP_HARD_MIN + 1)


def test_floors_are_not_fields_on_the_profile() -> None:
    """Structural: no preference edit can reach a safety limit (scope 6.3)."""
    fields = set(PreferenceProfile.model_fields)
    assert not fields & {"wbgt_hard_c", "lts_hard", "dry_gap_hard_min"}


# --- overrides --------------------------------------------------------------


def test_override_does_not_mutate_the_original() -> None:
    base = load_defaults()
    changed = apply_overrides(base, {"traffic_tolerance": 3})
    assert changed.traffic_tolerance.value == 3
    assert base.traffic_tolerance.value == 2


def test_bare_override_keeps_existing_provenance() -> None:
    """A per-run override must not promote itself to `stated` (scope 6.3)."""
    changed = apply_overrides(load_defaults(), {"traffic_tolerance": 3})
    assert changed.traffic_tolerance.provenance is Provenance.DEFAULT


def test_explicit_entry_override_may_set_provenance() -> None:
    changed = apply_overrides(
        load_defaults(),
        {"traffic_tolerance": {"value": 3, "provenance": Provenance.STATED}},
    )
    assert changed.traffic_tolerance.provenance is Provenance.STATED


def test_empty_override_is_a_no_op() -> None:
    base = load_defaults()
    assert apply_overrides(base, {}) is base


def test_unknown_preference_is_rejected() -> None:
    with pytest.raises(ProfileError, match="unknown preference"):
        apply_overrides(load_defaults(), {"favourite_colour": "blue"})


def test_override_cannot_breach_a_floor() -> None:
    with pytest.raises(FloorViolation):
        apply_overrides(load_defaults(), {"traffic_tolerance": 4})
    with pytest.raises(FloorViolation):
        apply_overrides(load_defaults(), {"water_gap_max_min": 999.0})


# --- merge and provenance precedence ---------------------------------------


def test_inferred_cannot_overwrite_stated() -> None:
    stated = PreferenceProfile(
        traffic_tolerance=PreferenceEntry(value=3, provenance=Provenance.STATED)
    )
    inferred = PreferenceProfile(
        traffic_tolerance=PreferenceEntry(value=1, provenance=Provenance.INFERRED)
    )
    merged, refused = merge(stated, inferred)
    assert merged.traffic_tolerance.value == 3
    assert "traffic_tolerance" in refused


def test_stated_overwrites_inferred() -> None:
    inferred = PreferenceProfile(
        traffic_tolerance=PreferenceEntry(value=1, provenance=Provenance.INFERRED)
    )
    stated = PreferenceProfile(
        traffic_tolerance=PreferenceEntry(value=3, provenance=Provenance.STATED)
    )
    merged, refused = merge(inferred, stated)
    assert merged.traffic_tolerance.value == 3
    assert refused == []


def test_merging_a_profile_with_itself_changes_nothing() -> None:
    base = load_defaults()
    merged, refused = merge(base, base)
    assert refused == []
    assert merged.model_dump() == base.model_dump()


# --- persistence ------------------------------------------------------------


def test_profile_round_trips_through_yaml(tmp_path: Path) -> None:
    profile = apply_overrides(
        load_defaults(),
        {"traffic_tolerance": {"value": 3, "provenance": Provenance.STATED}},
    )
    path = save_profile(profile, tmp_path / "profile.yaml")
    restored = load_profile(path)
    assert restored.traffic_tolerance.value == 3
    assert restored.traffic_tolerance.provenance is Provenance.STATED


def test_missing_profile_falls_back_to_defaults(tmp_path: Path) -> None:
    """A first-time user has no profile, and scope 6.3 forbids demanding one."""
    assert load_profile(tmp_path / "absent.yaml").traffic_tolerance.value == 2


def test_partial_profile_is_filled_in_from_defaults(tmp_path: Path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text("traffic_tolerance:\n  value: 3\n", encoding="utf-8")
    profile = load_profile(path)
    assert profile.traffic_tolerance.value == 3
    assert profile.water_gap_max_min.value == 90.0


def test_malformed_profile_is_a_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text("traffic_tolerance: [unclosed\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="could not parse"):
        load_profile(path)


def test_a_stored_profile_breaching_a_floor_is_rejected_at_load(tmp_path: Path) -> None:
    """Hand-edited files are the obvious way to try to disable a hard flag."""
    path = tmp_path / "profile.yaml"
    path.write_text("traffic_tolerance:\n  value: 4\n", encoding="utf-8")
    with pytest.raises(FloorViolation):
        load_profile(path)
