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


# --- a surveyed speed (M18, ADR 0053) ---------------------------------------


def test_a_surveyed_speed_above_the_limit_is_the_speed_read() -> None:
    """A 40 km/h street where traffic does 65 is a 65 km/h street to someone beside it."""
    from longrun.core.routing.lts import SURVEY_SPEED_COLUMN, lts_for_way

    tags = {"highway": "secondary", "maxspeed": "40", "sidewalk": "both"}
    posted = lts_for_way(tags)
    surveyed = lts_for_way({**tags, SURVEY_SPEED_COLUMN: 65.0})
    assert surveyed.level == posted.level + 2  # one step down for 40, one up for 65
    assert "speed85_65kph" in surveyed.reasons
    assert "maxspeed_40kph" not in surveyed.reasons


def test_a_survey_under_the_limit_is_no_reason_to_believe_the_sign_less() -> None:
    from longrun.core.routing.lts import SURVEY_SPEED_COLUMN, lts_for_way

    tags = {"highway": "secondary", "maxspeed": "70", "sidewalk": "both"}
    assert lts_for_way({**tags, SURVEY_SPEED_COLUMN: 52.0}) == lts_for_way(tags)


def test_a_surveyed_speed_is_a_measured_speed_so_it_lifts_the_missing_speed_penalty() -> None:
    from longrun.core.routing.lts import SURVEY_SPEED_COLUMN, lts_for_way

    tags = {"highway": "tertiary", "sidewalk": "both"}
    assert "maxspeed_unknown" in lts_for_way(tags).reasons
    surveyed = lts_for_way({**tags, SURVEY_SPEED_COLUMN: 48.0})
    assert "maxspeed_unknown" not in surveyed.reasons
    assert surveyed.confidence > lts_for_way(tags).confidence


def test_without_a_survey_the_one_reader_is_the_function_it_wraps() -> None:
    """The guarantee that let M18 add the reader without moving a golden: every way that
    has no survey scores exactly as it did."""
    from longrun.core.routing.lts import lts_for_way

    for tags in (
        {"highway": "primary", "maxspeed": "55 mph"},
        {"highway": "residential"},
        {"highway": "secondary", "sidewalk": "separate", "lanes": "4"},
        {"highway": "tertiary", "maxspeed": "50", "aadt": 25_000},
    ):
        assert lts_for_way(tags) == lts_from_tags(tags, aadt=tags.get("aadt"))


@pytest.mark.parametrize("value", [None, float("nan"), "", "fast", 0.0, -5.0])
def test_a_survey_cell_with_nothing_in_it_is_no_survey(value: object) -> None:
    """A frame read from PostGIS carries the column on every way, null where no survey
    matched; a null must read as absent, not as a speed of zero."""
    from longrun.core.routing.lts import SURVEY_SPEED_COLUMN, survey_speed_of

    assert survey_speed_of({SURVEY_SPEED_COLUMN: value}) is None
    assert survey_speed_of({}) is None
