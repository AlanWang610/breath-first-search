"""Loading, saving and overriding the preference profile (scope 6.3).

Two rules the storage layer is responsible for:

* **Per-run overrides do not persist** unless the user confirms. `apply_overrides`
  returns a new profile and never writes; `save_profile` is a separate, explicit call.
  A conversational "let's avoid dirt today" must not silently become a standing
  preference.
* **`stated` beats `inferred` beats `default`.** `merge` refuses to let a lower-provenance
  entry overwrite a higher one, so an inference drawn from post-run feedback cannot
  quietly replace something the user said.

Safety floors are checked on the way in, not on the way out: a profile that would
suppress a hard flag is rejected at load and at override, so no downstream scorer has to
remember to re-check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from longrun.core.models.profile import PreferenceEntry, PreferenceProfile
from longrun.core.preferences.floors import check_traffic_tolerance, check_water_gap

DEFAULTS_PATH = Path(__file__).with_name("defaults.yaml")

#: Where a user's profile lives when the caller does not say (scope 6.3).
USER_PROFILE_PATH = Path.home() / ".longrun" / "profile.yaml"


class ProfileError(ValueError):
    """A profile file could not be read or was not valid."""


def load_defaults() -> PreferenceProfile:
    """The shipped scope 6.3 table, every entry with provenance `default`."""
    raw = yaml.safe_load(DEFAULTS_PATH.read_text(encoding="utf-8"))
    return PreferenceProfile.model_validate(raw)


def load_profile(path: Path | None = None) -> PreferenceProfile:
    """Load a user profile, falling back to the shipped defaults.

    A missing file is normal, not an error: a first-time user has no profile and scope
    6.3 forbids an up-front questionnaire to create one.
    """
    target = path or USER_PROFILE_PATH
    if not target.exists():
        return load_defaults()

    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ProfileError(f"could not parse profile at {target}: {exc}") from exc

    merged = load_defaults().model_dump()
    merged.update(raw)
    profile = PreferenceProfile.model_validate(merged)
    return _check_floors(profile)


def save_profile(profile: PreferenceProfile, path: Path | None = None) -> Path:
    """Persist a profile. Called only after the user confirms a change (scope 6.3)."""
    target = path or USER_PROFILE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(profile.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    return target


def apply_overrides(profile: PreferenceProfile, overrides: dict[str, Any]) -> PreferenceProfile:
    """Return a profile with per-run overrides applied. Never writes to disk.

    An override may carry a bare value (`{"traffic_tolerance": 3}`) or a full entry
    (`{"traffic_tolerance": {"value": 3, "provenance": 2}}`); the bare form keeps the
    existing provenance, because using an override to change provenance would be exactly
    the silent persistence scope 6.3 forbids.
    """
    if not overrides:
        return profile

    data = profile.model_dump()
    for key, value in overrides.items():
        if key not in data:
            raise ProfileError(f"unknown preference {key!r}")
        if isinstance(value, dict) and "value" in value:
            data[key] = {**data[key], **value}
        else:
            data[key] = {**data[key], "value": value}

    return _check_floors(PreferenceProfile.model_validate(data))


def merge(
    base: PreferenceProfile, incoming: PreferenceProfile
) -> tuple[PreferenceProfile, list[str]]:
    """Merge `incoming` into `base`, honouring provenance precedence.

    Returns the merged profile and the keys that were refused, so the caller can tell the
    user what was not applied rather than leaving them to discover it.
    """
    merged = base.model_dump()
    refused: list[str] = []

    for key in type(base).model_fields:
        if key == "version":
            continue
        base_entry: PreferenceEntry[Any] = getattr(base, key)
        new_entry: PreferenceEntry[Any] = getattr(incoming, key)
        if new_entry == base_entry:
            continue
        if new_entry.supersedes(base_entry):
            merged[key] = new_entry.model_dump()
        else:
            refused.append(key)

    return _check_floors(PreferenceProfile.model_validate(merged)), refused


def _check_floors(profile: PreferenceProfile) -> PreferenceProfile:
    """Reject a profile that would suppress a hard flag (scope 6.3)."""
    check_traffic_tolerance(profile.traffic_tolerance.value)
    check_water_gap(profile.water_gap_max_min.value)
    return profile
