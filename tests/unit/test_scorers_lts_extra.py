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
