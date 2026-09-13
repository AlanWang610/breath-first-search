"""LTS tag-reading edge cases (scope 7.1, 12).

Split from the scorer suite because these pin `lts_from_tags` itself rather than any
scorer that consumes it.
"""

from __future__ import annotations

import pytest

from longrun.core.routing.lts import has_sidewalk, lts_from_tags


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("yes", True),
        ("both", True),
        ("left", True),
        ("right", True),
        ("no", False),
        ("none", False),
        # The sidewalk is mapped as its own way, so THIS way carries none.
        ("separate", False),
        # Says something about a junction, not about walking the length of the way.
        ("crossing", None),
    ],
)
def test_sidewalk_tag_readings(value: str, expected: bool | None) -> None:
    assert has_sidewalk({"sidewalk": value}) is expected


def test_separate_does_not_earn_a_stress_discount() -> None:
    """Reading `separate` as "has sidewalk" moved a 50 kph secondary from LTS 4 to 2."""
    tags = {"highway": "secondary", "maxspeed": "50"}
    separate = lts_from_tags({**tags, "sidewalk": "separate"})
    present = lts_from_tags({**tags, "sidewalk": "both"})
    assert separate.level > present.level
    assert "no_sidewalk" in separate.reasons


def test_unsurveyed_road_class_is_known_not_unknown() -> None:
    """`highway=road` is common in thin-data regions (scope 11 region 3)."""
    result = lts_from_tags({"highway": "road"})
    assert not any(r.startswith("unknown_highway_class") for r in result.reasons)


# --- risk R2 / ADR 0012: AADT is optional, and today it is always absent ------


def test_lts_is_computed_without_any_volume_at_all() -> None:
    """ADR 0012: no region build loads AADT, because no unauthenticated endpoint serves it.

    So the level every plan reports today is tag-only, and that has to be a supported
    state rather than a degraded one - a `lts_from_tags` that needed a volume would make
    `hostility` report `unavailable` on every segment of every route in the system.
    """
    from longrun.core.routing.lts import lts_from_tags

    result = lts_from_tags({"highway": "primary", "lanes": "4", "sidewalk": "no"})
    assert result.level == 4
    assert result.confidence > 0.0
    assert not any("aadt" in reason for reason in result.reasons)


def test_a_volume_refines_the_level_rather_than_being_required_for_one() -> None:
    """The seam ADR 0012 keeps: the parameter costs nothing and is where a future loader
    plugs in, so removing it would have to be undone to add the loader back."""
    from longrun.core.routing.lts import lts_from_tags

    tags = {"highway": "secondary", "maxspeed": "30 mph", "sidewalk": "both"}
    without = lts_from_tags(tags)
    quiet = lts_from_tags(tags, aadt=800.0)
    busy = lts_from_tags(tags, aadt=30_000.0)
    assert without.level >= 1
    assert quiet.level <= without.level
    assert busy.level >= without.level
