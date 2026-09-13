"""When a preference may be asked about, and when it may not (scope 6.3).

The rule is almost entirely about *refusing* to ask, so most of these assert silence. The
scope says why: "questionnaire answers to situations the user hasn't faced would be stored
as `stated` with the same authority as real ones; in-context elicitation keeps `stated`
meaningful."
"""

from __future__ import annotations

from datetime import date

import pytest

from longrun.core.geo.segments import segment_route
from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.models.measurement import Flag, FlagKind, ScorerResult, Tier
from longrun.core.models.profile import PreferenceEntry, Provenance
from longrun.core.plan.arbitrate import score_candidate
from longrun.core.preferences.elicitation import (
    MAX_QUESTIONS_PER_PLAN,
    SCORER_AXIS,
    dominant_axis,
    first_use_questions,
    is_default,
    question_for,
    record_answer,
)
from longrun.core.preferences.store import load_defaults

ROUTE = Route(
    id="r",
    points=[
        RoutePoint(lat=37.77, lon=-122.41 + i * 0.001139, cum_dist_m=i * 100.0) for i in range(41)
    ],
)
SEGMENTS = segment_route(ROUTE, max_len_m=1000.0)


def _candidate(label: str, flags: list[Flag]):  # type: ignore[no-untyped-def]
    return score_candidate(label, [ScorerResult(name="x", flags=flags)], SEGMENTS, ROUTE.length_m)


def _flag(scorer: str, severity: float, segment: int = 1) -> Flag:
    return Flag(
        scorer=scorer,
        segment_id=SEGMENTS[segment].id,
        kind=FlagKind.SOFT,
        tier=Tier.COMFORT,
        severity=severity,
        reason_code="x",
    )


# --- the inversion -----------------------------------------------------------


def test_one_axis_dominating_is_found_and_named() -> None:
    """`arbitrate` can say two candidates differ by 0.04 in the COMFORT tier and cannot say
    which preference caused it. This is the mapping back, and without it a question about
    a difference would be a question about the wrong thing."""
    busy = _candidate("A", [_flag("segment_hostility", 0.9)])
    calm = _candidate("B", [_flag("segment_hostility", 0.1)])

    found = dominant_axis(busy, calm)

    assert found is not None
    axis, share, detail = found
    assert axis == "traffic_tolerance"
    assert share > 0.9
    assert "B" in detail and "A" in detail


def test_routes_differing_for_several_reasons_are_not_asked_about() -> None:
    """The common case, and the honest answer: a question about one of four differences
    would misdescribe the choice the runner is actually making."""
    a = _candidate("A", [_flag("segment_hostility", 0.8), _flag("surface_profile", 0.1, 2)])
    b = _candidate("B", [_flag("segment_hostility", 0.1), _flag("surface_profile", 0.8, 2)])

    assert dominant_axis(a, b) is None


def test_a_scorer_no_preference_explains_is_not_in_the_table() -> None:
    """`legality` and `closures` are not preferences: a closed road is not a matter of
    taste, and scope 6.3 keeps safety floors outside the profile entirely."""
    assert "legality" not in SCORER_AXIS
    assert "closures" not in SCORER_AXIS
    assert "heat_stress" not in SCORER_AXIS


def test_two_candidates_with_nothing_mappable_are_not_asked_about() -> None:
    a = _candidate("A", [_flag("legality", 0.9)])
    b = _candidate("B", [_flag("legality", 0.1)])

    assert dominant_axis(a, b) is None


# --- the conditions ----------------------------------------------------------


def _pair() -> tuple:  # type: ignore[type-arg]
    return (
        _candidate("A", [_flag("segment_hostility", 0.9)]),
        _candidate("B", [_flag("segment_hostility", 0.1)]),
    )


def test_an_axis_already_stated_is_not_asked_about_again() -> None:
    """The load-bearing half of the rule. A runner who has said what they think should not
    be asked to say it again, and one whose history inferred it should not either."""
    profile = load_defaults().model_copy(
        update={
            "traffic_tolerance": PreferenceEntry(value=1, provenance=Provenance.STATED),
        }
    )
    a, b = _pair()

    assert question_for(a, b, profile, asked=0, at_km=2.0) is None
    assert not is_default(profile, "traffic_tolerance")


def test_an_axis_inferred_from_history_is_not_asked_about_either() -> None:
    """Scope 6.3: "if history is uploaded, `surface` and `grade` are inferred and not
    asked"."""
    profile = load_defaults().model_copy(
        update={"surface": PreferenceEntry(value="dirt", provenance=Provenance.INFERRED)}
    )
    assert not is_default(profile, "surface")


def test_the_cap_is_three_per_plan() -> None:
    profile = load_defaults()
    a, b = _pair()

    assert question_for(a, b, profile, asked=MAX_QUESTIONS_PER_PLAN - 1, at_km=2.0) is not None
    assert question_for(a, b, profile, asked=MAX_QUESTIONS_PER_PLAN, at_km=2.0) is None


def test_a_permitted_question_names_the_option_the_difference_and_the_mile() -> None:
    """Scope 6.3: "the question is then concrete (which option, what differs, at what
    mile)". All three, or it is the questionnaire the rule forbids."""
    question = question_for(*_pair(), load_defaults(), asked=0, at_km=4.2)

    assert question is not None
    assert question.options == ["A", "B"]
    assert question.at_km == 4.2
    assert "traffic tolerance" in question.detail


# --- the two exceptions -------------------------------------------------------


def test_only_two_axes_may_be_asked_up_front() -> None:
    """ "Because they affect nearly every plan" - and the exception is narrow on purpose: a
    third would be the questionnaire the rule exists to prevent."""
    assert first_use_questions(load_defaults()) == ["traffic_tolerance", "carry_capacity_ml"]


def test_an_axis_already_answered_drops_out_of_the_first_use_list() -> None:
    profile = load_defaults().model_copy(
        update={"traffic_tolerance": PreferenceEntry(value=1, provenance=Provenance.STATED)}
    )
    assert first_use_questions(profile) == ["carry_capacity_ml"]


# --- writing the answer -------------------------------------------------------


def test_an_answer_is_stored_as_stated() -> None:
    profile, refused = record_answer(load_defaults(), "surface", "dirt", on=date(2026, 3, 15))

    assert profile.surface.value == "dirt"
    assert profile.surface.provenance is Provenance.STATED
    assert not refused


def test_an_answer_that_breaches_a_floor_is_refused_rather_than_clamped() -> None:
    """`_check_floors` runs on `merge`'s path, so an answer of "I don't mind traffic at
    all" does not quietly become LTS 4 - which is a hard flag and a safety floor."""
    from longrun.core.preferences.floors import FloorViolation

    with pytest.raises((FloorViolation, ValueError)):
        record_answer(load_defaults(), "traffic_tolerance", 4, on=date(2026, 3, 15))
