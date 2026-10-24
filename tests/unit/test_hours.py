"""Opening hours (OSM `opening_hours`), read by `core/scorers/_hours.py` (M18).

Table-driven, because the language is the subject: each row is a string a mapper actually
wrote, an instant, and the answer. The strings in the first table come from the amenities
fixtures the goldens carry, so a regression here is a regression on a real route.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from longrun.core.scorers._hours import is_open, next_open, read_hours, sun_at

MONDAY_MIDNIGHT = datetime(2026, 9, 14, 0, 0)
WEDNESDAY_NOON = datetime(2026, 9, 16, 12, 0)
WEDNESDAY_0730 = datetime(2026, 9, 16, 7, 30)
SATURDAY_0100 = datetime(2026, 9, 19, 1, 0)
SATURDAY_NOON = datetime(2026, 9, 19, 12, 0)
SF = sun_at(37.7955, -122.3937, -7.0)


@pytest.mark.parametrize(
    ("spec", "when", "expected"),
    [
        # The restaurant beside phoenix-heat. Only the Sunday rule used to parse, so a
        # Wednesday lunchtime read closed; `We, Th` with a space is ordinary OSM.
        ("We, Th 11:00-22:00; Fr, Sa 11:00-23:00; Su 11:00-20:00", WEDNESDAY_NOON, True),
        ("Mo-Fr 07:00-19:00", SATURDAY_NOON, False),
        ("Mo-Fr 07:00-19:00; Sa 08:00-17:00", SATURDAY_NOON, True),
        ("mo-fr 07:00-19:00", WEDNESDAY_NOON, True),
        ("8:00-17:00", WEDNESDAY_NOON, True),
        ("Mo-Fr 08:00-11:00,13:00-17:00", WEDNESDAY_NOON, False),
        ("Mo-Fr 08:00-11:00, 11:30-17:00", WEDNESDAY_NOON, True),
        ("Mo-Fr 08:00-12:00, Sa 09:00-12:00", SATURDAY_NOON, True),
        ('Mo-Fr 08:00-18:00 "call first"', WEDNESDAY_NOON, True),
        ("24/7", MONDAY_MIDNIGHT, True),
        ("off", SATURDAY_NOON, False),
        ("closed", SATURDAY_NOON, False),
        ("open", SATURDAY_NOON, True),
    ],
)
def test_the_common_forms_are_read(spec: str, when: datetime, expected: bool) -> None:
    assert is_open(spec, when) is expected


def test_no_hours_at_all_is_open() -> None:
    """A park fountain has no `opening_hours` because it has no hours."""
    assert is_open(None, SATURDAY_NOON) is True
    assert is_open("  ", SATURDAY_NOON) is True


# --- OSM's rule order --------------------------------------------------------


def test_a_later_rule_replaces_the_earlier_ones_for_the_days_it_selects() -> None:
    assert is_open("Mo-Fr 08:00-18:00; We off", WEDNESDAY_NOON) is False
    assert is_open("Mo-Fr 08:00-18:00; We off", WEDNESDAY_NOON - timedelta(days=1)) is True


def test_a_comma_joined_rule_adds_to_the_one_before_it() -> None:
    """Which is how a lunch break is written - and why `;` there would close the day."""
    spec = "Mo-Fr 08:00-18:00, We 12:00-13:00 off"
    assert is_open(spec, WEDNESDAY_NOON) is False
    assert is_open(spec, WEDNESDAY_NOON + timedelta(hours=2)) is True


def test_a_semicolon_between_two_ranges_on_the_same_days_is_read_as_the_mapper_meant() -> None:
    """Strict OSM makes this café open for dinner only; OSM's validator calls it a
    mistake, and reading it strictly would invent a lunchtime closure (M18). Both strings
    are in the bay-urban fixtures."""
    assert is_open("10:00-14:30; 17:30-20:30; PH unknown", WEDNESDAY_NOON) is True
    assert is_open("We-Mo 12:00-15:00; We-Mo 17:00-22:00", WEDNESDAY_NOON) is True
    assert is_open("Mo-Fr 08:00-18:00; Mo-Fr 12:00-13:00 off", WEDNESDAY_NOON) is False
    assert (
        is_open("Mo-Fr 08:00-18:00; Mo-Fr 12:00-13:00 off", WEDNESDAY_0730 + timedelta(hours=2))
        is True
    )
    # Different days, or no times: the later rule still replaces, as OSM says.
    assert is_open("Mo-Fr 08:00-18:00; We off", WEDNESDAY_NOON) is False
    assert is_open("Mo-Fr 08:00-18:00; Mo-Fr off", WEDNESDAY_NOON) is False


def test_a_range_past_midnight_spills_into_the_next_morning() -> None:
    assert is_open("Mo-Su 22:00-06:00", MONDAY_MIDNIGHT) is True
    assert is_open("Fr 22:00-02:00", SATURDAY_0100) is True
    assert is_open("Fr 10:00-26:00", SATURDAY_0100) is True
    # Saturday's own rule does not reach back into Saturday's early hours.
    assert is_open("Sa 22:00-02:00", SATURDAY_0100) is False


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("Apr-Oct 09:00-18:00", True),
        ("Nov-Mar 09:00-18:00", False),
        ("May-Sep: 06:00-22:00", True),
        ("Sep 19 off; 09:00-17:00", True),  # a later all-days rule replaces it
        ("09:00-17:00; Sep 19 off", False),
        ("09:00-17:00; Sep 15-25 off", False),
        ("09:00-17:00; Aug 30-Sep 02 off", True),
        ("09:00-17:00; Dec 24-Jan 02 off", True),
    ],
)
def test_months_and_dates_select_days(spec: str, expected: bool) -> None:
    assert is_open(spec, SATURDAY_NOON) is expected


def test_a_date_range_across_new_year_wraps() -> None:
    assert is_open("09:00-17:00; Dec 24-Jan 02 off", datetime(2026, 12, 30, 12)) is False
    assert is_open("09:00-17:00; Dec 24-Jan 02 off", datetime(2027, 1, 1, 12)) is False
    assert is_open("09:00-17:00; Dec 24-Jan 02 off", datetime(2027, 1, 3, 12)) is True


def test_a_public_holiday_is_read_and_never_matches() -> None:
    """Nothing here knows the holiday calendar; assuming today is not one keeps every
    ordinary day's answer."""
    assert is_open("Mo-Fr 08:00-18:00; PH off", WEDNESDAY_NOON) is True
    assert is_open("Mo-Fr,PH 08:00-18:00", WEDNESDAY_NOON) is True
    assert read_hours("Mo-Fr 08:00-18:00; PH off").unread == 0


# --- the sun -----------------------------------------------------------------


def test_sun_relative_hours_need_a_sun() -> None:
    assert is_open("sunrise-sunset", SATURDAY_NOON) is None
    assert is_open("sunrise-sunset", SATURDAY_NOON, SF) is True
    assert is_open("sunrise-sunset", MONDAY_MIDNIGHT, SF) is False
    assert is_open("dawn-dusk", datetime(2026, 9, 19, 6, 40), SF) is True


def test_a_sun_offset_moves_the_edge() -> None:
    # Sunrise in San Francisco on 2026-09-19 is about 06:55.
    assert is_open("sunrise-sunset", datetime(2026, 9, 19, 6, 30), SF) is False
    assert is_open("(sunrise-00:30)-(sunset+00:30)", datetime(2026, 9, 19, 6, 30), SF) is True


# --- what is not understood --------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    ['"by appointment"', "18:00+", "Th[4] 08:00-12:00", "week 01-10 08:00-12:00", "2026 Mo-Fr"],
)
def test_a_dialect_this_module_does_not_speak_is_unknown(spec: str) -> None:
    assert is_open(spec, WEDNESDAY_NOON) is None
    assert read_hours(spec).unread == 1


def test_an_unread_rule_can_never_be_what_makes_a_place_closed() -> None:
    """The three-valued OR. A known rule that says open stands; one that says closed
    becomes unknown when a rule nobody could read might open it (M18)."""
    spec = "Mo-Fr 07:00-19:00; Th[4] off"
    assert is_open(spec, WEDNESDAY_NOON) is True
    assert is_open(spec, SATURDAY_NOON) is None


def test_the_unknown_modifier_says_so() -> None:
    assert is_open("Mo-Fr 07:00-19:00 unknown", WEDNESDAY_NOON) is None


def test_a_rich_holiday_schedule_still_answers_an_ordinary_wednesday() -> None:
    """bay-urban's amenities carry strings like this one, half of which this module does
    not read; the half it does read is enough for a Wednesday."""
    spec = (
        "Mo-Sa 10:00-18:00; Su 11:00-17:00; Nov Th[4] off; Dec 24 10:00-16:00; "
        "Dec 25 off; Dec 31 10:00-25:00"
    )
    assert is_open(spec, WEDNESDAY_NOON) is True
    assert read_hours(spec).unread == 1


# --- next_open ---------------------------------------------------------------


def test_next_open_is_the_first_opening_after_now() -> None:
    assert next_open("Mo-Fr 09:00-17:00", WEDNESDAY_0730) == datetime(2026, 9, 16, 9, 0)
    assert next_open("Mo-Fr 09:00-17:00", WEDNESDAY_NOON) == WEDNESDAY_NOON


def test_next_open_respects_its_horizon() -> None:
    saturday = datetime(2026, 9, 19, 7, 30)
    assert next_open("Mo-Fr 09:00-17:00", saturday) is None
    assert next_open("Mo-Fr 09:00-17:00", saturday, horizon=timedelta(days=3)) == datetime(
        2026, 9, 21, 9, 0
    )


def test_next_open_follows_the_sun() -> None:
    opens = next_open("sunrise-sunset", datetime(2026, 9, 16, 5, 0), SF)
    assert opens is not None and opens.strftime("%H") == "06"
