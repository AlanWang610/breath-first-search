"""The scope 7.4 environment scorers, at the level where the answer is arithmetic.

The integration is pinned by the golden route. What is pinned here is the handful of pure
functions that decide what a number *means* — a WBGT formula, a cloud factor, an
`opening_hours` string — because those are where a plausible wrong answer hides. A WBGT
that is two degrees low still looks like a WBGT.

Every three-state function gets the same shape of test, because the third state is the one
the project exists to preserve: `lit_state` and `is_open` both distinguish "no" from "we
could not tell", and collapsing either into a boolean is how "unknown" quietly becomes
"absent".
"""

from __future__ import annotations

import math
from datetime import datetime

import pytest

from longrun.core.data.air import AIR_FIELDS, air_args, parse_air
from longrun.core.geo.solar import (
    CIVIL_TWILIGHT_DEG,
    clear_sky,
    solar_positions,
    utc_offset_from_longitude,
)
from longrun.core.preferences.floors import DRY_GAP_HARD_MIN, WBGT_HARD_C
from longrun.core.scorers.heat import WBGT_SOFT_C, severity_for, vapour_pressure_hpa, wbgt_c
from longrun.core.scorers.lighting import lit_state
from longrun.core.scorers.resupply_schedule import (
    HEAT_SCALE_AT_FLOOR,
    heat_scaled,
    is_open,
)
from longrun.core.scorers.sun import _cloud_factors

SF_LAT, SF_LON = 37.7955, -122.3937

#: 12 September 2026 is a Saturday; 14 September is a Monday.
SATURDAY_NOON = datetime(2026, 9, 12, 12, 0)
MONDAY_NOON = datetime(2026, 9, 14, 12, 0)
MONDAY_MIDNIGHT = datetime(2026, 9, 14, 2, 0)


# --- WBGT -------------------------------------------------------------------


def test_wbgt_matches_a_hand_computed_value() -> None:
    """30 C at 50% RH is WBGT 29.3 by the ACSM formula. Arithmetic, not a fixture."""
    assert vapour_pressure_hpa(30.0, 50.0) == pytest.approx(21.1, abs=0.2)
    assert wbgt_c(30.0, 50.0) == pytest.approx(29.3, abs=0.2)


def test_wbgt_rises_with_humidity_at_a_fixed_temperature() -> None:
    """The whole reason WBGT is used rather than air temperature."""
    assert wbgt_c(30.0, 90.0) > wbgt_c(30.0, 50.0) > wbgt_c(30.0, 10.0)


def test_severity_is_zero_at_the_soft_threshold_and_one_at_the_floor() -> None:
    """Scope 8.3's two numbers, linear between, saturating above."""
    assert severity_for(WBGT_SOFT_C) == 0.0
    assert severity_for(WBGT_HARD_C) == 1.0
    assert severity_for((WBGT_SOFT_C + WBGT_HARD_C) / 2) == pytest.approx(0.5)
    assert severity_for(WBGT_HARD_C + 10) == 1.0
    assert severity_for(0.0) == 0.0


# --- cloud scaling ----------------------------------------------------------


def test_a_clear_sky_scales_nothing() -> None:
    assert _cloud_factors(0.0) == (1.0, 1.0)


def test_unknown_cloud_leaves_irradiance_at_its_clear_sky_ceiling() -> None:
    """An upper bound, and the coverage manifest says so. Not an assumed clear day."""
    assert _cloud_factors(None) == (1.0, 1.0)


def test_overcast_kills_the_beam_but_not_the_sky() -> None:
    """The reason beam and global scale separately: under cloud the sky stays bright."""
    beam, glob = _cloud_factors(100.0)
    assert beam == 0.0
    assert glob == pytest.approx(0.25)


def test_thin_cloud_barely_dims_the_sky() -> None:
    """Kasten-Czeplak is strongly non-linear: the last tenth takes most of the light."""
    _, light = _cloud_factors(30.0)
    _, heavy = _cloud_factors(90.0)
    assert light > 0.97
    assert heavy < 0.55


# --- solar geometry ---------------------------------------------------------


def test_the_sun_is_up_at_noon_and_down_at_midnight_in_san_francisco() -> None:
    position = solar_positions(
        [datetime(2026, 9, 12, 12, 0), datetime(2026, 9, 12, 0, 0)], SF_LAT, SF_LON, -7.0
    )
    assert math.degrees(position.elevation[0]) > 40.0
    assert math.degrees(position.elevation[1]) < -20.0
    assert list(position.is_daylight) == [True, False]


def test_the_sun_is_south_of_east_at_noon() -> None:
    """The azimuth convention, asserted where a sign error would be invisible."""
    position = solar_positions([datetime(2026, 9, 12, 12, 0)], SF_LAT, SF_LON, -7.0)
    azimuth = math.degrees(position.azimuth[0])
    assert 120.0 < azimuth < 190.0, azimuth


def test_an_hour_of_offset_error_moves_the_sun_more_than_fifteen_degrees() -> None:
    """Why `--utc-offset` exists, and why guessing it is reported.

    An hour is fifteen degrees of *hour angle*, but azimuth sweeps faster than that near
    noon — measured at 25 degrees here. Either way it moves a building's shadow across a
    street, which is the difference between a shaded segment and an exposed one.
    """
    correct = solar_positions([datetime(2026, 9, 12, 12, 0)], SF_LAT, SF_LON, -7.0)
    wrong = solar_positions([datetime(2026, 9, 12, 12, 0)], SF_LAT, SF_LON, -8.0)
    drift = abs(math.degrees(correct.azimuth[0] - wrong.azimuth[0]))
    assert 15.0 < drift < 40.0, drift


def test_longitude_gives_pacific_time_for_san_francisco() -> None:
    """Right to the hour in winter, an hour out in summer — which is why it is reported."""
    assert utc_offset_from_longitude(SF_LON) == -8.0


def test_clear_sky_peaks_at_midday_and_is_zero_at_night() -> None:
    sky = clear_sky(
        [datetime(2026, 9, 12, 12, 0), datetime(2026, 9, 12, 2, 0)], SF_LAT, SF_LON, -7.0
    )
    assert sky.ghi[0] > 500.0
    assert sky.ghi[1] == pytest.approx(0.0, abs=1.0)


def test_civil_twilight_is_the_darkness_threshold_not_sunset() -> None:
    """A runner has usable light below the horizon; -6 degrees, not 0."""
    assert CIVIL_TWILIGHT_DEG == -6.0


# --- lighting ---------------------------------------------------------------


def test_lit_has_three_states() -> None:
    """Scope 8.3 flags `lit=no`, not "not `lit=yes`". Silence is not a claim."""
    assert lit_state({"lit": "yes"}) is True
    assert lit_state({"lit": "no"}) is False
    assert lit_state({"highway": "footway"}) is None
    assert lit_state(None) is None


def test_an_interval_is_still_a_claim_that_lighting_exists() -> None:
    assert lit_state({"lit": "22:00-05:00"}) is True


# --- opening hours ----------------------------------------------------------


def test_a_service_with_no_hours_at_all_is_open() -> None:
    """A park drinking fountain has no `opening_hours` because it has no hours."""
    assert is_open(None, SATURDAY_NOON) is True
    assert is_open("", SATURDAY_NOON) is True


def test_round_the_clock_and_explicitly_closed() -> None:
    assert is_open("24/7", MONDAY_MIDNIGHT) is True
    assert is_open("off", SATURDAY_NOON) is False


def test_a_weekday_rule_is_read() -> None:
    assert is_open("Mo-Fr 07:00-19:00", MONDAY_NOON) is True
    assert is_open("Mo-Fr 07:00-19:00", SATURDAY_NOON) is False
    assert is_open("Mo-Fr 07:00-19:00", MONDAY_MIDNIGHT) is False


def test_several_rules_are_tried_in_turn() -> None:
    spec = "Mo-Fr 07:00-19:00; Sa 08:00-17:00"
    assert is_open(spec, SATURDAY_NOON) is True
    assert is_open(spec, MONDAY_NOON) is True


def test_a_dialect_this_parser_does_not_speak_is_unknown_not_closed() -> None:
    """The load-bearing case. Reading it as closed would invent dry gaps out of syntax."""
    assert is_open("sunrise-sunset", SATURDAY_NOON) is None
    assert is_open("Apr-Oct 09:00-18:00", SATURDAY_NOON) is None


def test_an_overnight_range_wraps_past_midnight() -> None:
    assert is_open("Mo-Su 22:00-06:00", MONDAY_MIDNIGHT) is True


# --- heat scaling of the dry gap --------------------------------------------


def test_a_cool_day_does_not_shrink_the_dry_gap_threshold() -> None:
    assert heat_scaled(90.0, None) == 90.0
    assert heat_scaled(90.0, 18.0) == 90.0
    assert heat_scaled(90.0, WBGT_SOFT_C) == 90.0


def test_the_threshold_halves_by_the_hard_floor() -> None:
    """Scope 8.3 requires the scaling and does not specify it; this is the stated choice."""
    assert heat_scaled(90.0, WBGT_HARD_C) == pytest.approx(90.0 * HEAT_SCALE_AT_FLOOR)
    assert heat_scaled(DRY_GAP_HARD_MIN, WBGT_HARD_C) == pytest.approx(75.0)
    midpoint = (WBGT_SOFT_C + WBGT_HARD_C) / 2
    assert heat_scaled(90.0, midpoint) == pytest.approx(67.5)


def test_scaling_saturates_rather_than_going_negative() -> None:
    assert heat_scaled(90.0, 45.0) == pytest.approx(45.0)


# --- air quality ------------------------------------------------------------


def test_air_quality_parses_to_hours() -> None:
    hours = parse_air(
        {
            "hourly": {
                "time": ["2026-09-12T12:00", "2026-09-12T13:00"],
                "us_aqi": [42.0, 51.0],
                "pm2_5": [9.1, 11.4],
                "pm10": [14.0, None],
            }
        }
    )
    assert [h.us_aqi for h in hours] == [42.0, 51.0]
    assert hours[1].pm10 is None, "a missing value is unknown, not zero"


def test_the_air_quality_cache_key_rounds_its_coordinates() -> None:
    from datetime import date

    assert air_args(37.79551234, -122.40008765, date(2026, 9, 12)) == air_args(
        37.7955, -122.4001, date(2026, 9, 12)
    )
    assert air_args(37.7955, -122.4, date(2026, 9, 12))["hourly"] == AIR_FIELDS
