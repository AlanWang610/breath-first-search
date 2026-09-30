"""The label core: bounds a person's verdict is checked against, and the ledger it lands in.

`tests/eval` asserts the same four properties over committed cases. These pin the function
both of them call, from the side a person meets it on: the moment a verdict is written, when
"your bounds say this route is fine" is still a sentence about the route on the screen.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from longrun.core.labels.bounds import (
    BOUND_NAMES,
    METRICS,
    check_verdict,
    measured_from_digest,
    unmeasured,
)
from longrun.core.labels.ledger import (
    DEFAULT_LABELS_DIR,
    Ledger,
    labeler_from_env,
    labels_dir_from_env,
)
from longrun.core.labels.pairs_io import dump_pairs, read_pairs
from longrun.core.labels.records import Verdict
from longrun.core.routing.tuning import PreferencePair, RouteSummary, WayClass

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden" / "routes"

REASON = "A flat riverside loop on paths the whole way, with one crossing I would wait at anyway."

#: A route that measures fine on everything a bound can name.
MEASURED: dict[str, Any] = {
    "fraction_lts3_plus": 0.05,
    "lts4_count": 1,
    "hard_flags": 0,
    "detour_ratio": None,
    "scored_fraction": 0.8,
    "length_m": 12000.0,
    "gain_m": 40.0,
    "unpaved_fraction": 0.0,
    "max_dry_gap_min": 30.0,
    "max_water_gap_m": 2000.0,
    "worst_exit_m": 300.0,
    "unlit_dark_m": 0.0,
    "residual_flags": 2,
}


def _digest(name: str) -> dict[str, Any]:
    return json.loads((GOLDEN_DIR / name / "expected.json").read_text(encoding="utf-8"))


def _check(**overrides: Any) -> list[str]:
    arguments: dict[str, Any] = {
        "would_run": "yes",
        "reason": REASON,
        "bounds": {"fraction_lts3_plus_max": 0.15},
        "gap": None,
        "measured": MEASURED,
    }
    arguments.update(overrides)
    return check_verdict(**arguments)


def _verdict(**overrides: Any) -> Verdict:
    fields: dict[str, Any] = {
        "subject": "boston-winter",
        "golden": "boston-winter",
        "would_run": "yes",
        "reason": REASON,
        "bounds": {"fraction_lts3_plus_max": 0.15},
        "measured": MEASURED,
        "labeler": "runner-1",
        "labelled_at": datetime(2026, 9, 29, 7, 30),
        "via": "cli",
    }
    fields.update(overrides)
    return Verdict(**fields)


# --- bound names -------------------------------------------------------------


def test_every_metric_can_be_bounded_from_either_side_and_nothing_else_can() -> None:
    assert {"fraction_lts3_plus_max", "hard_flags_max", "gain_m_max", "length_m_min"} <= BOUND_NAMES
    assert len(BOUND_NAMES) == 2 * len(METRICS)
    assert "residual_flags_max" not in BOUND_NAMES


def test_the_digest_reading_matches_what_the_golden_records() -> None:
    """Read off real files, so a renamed summary key fails here rather than turning every
    bound on it into one that silently never runs."""
    phoenix = measured_from_digest(_digest("phoenix-heat"))
    boston = measured_from_digest(_digest("boston-winter"))

    assert phoenix["hard_flags"] == 97
    assert phoenix["fraction_lts3_plus"] == 0.0
    assert boston["fraction_lts3_plus"] == 0.023
    assert boston["scored_fraction"] == round(1894.241 / 2457.049, 3)
    assert boston["length_m"] == 2457.0
    # No golden plan has a detour ratio: `repair` never routes, and a golden's router is
    # a port nothing listens on.
    assert phoenix["detour_ratio"] is None and boston["detour_ratio"] is None


def test_a_scorer_with_no_summary_leaves_its_metric_unmeasured_rather_than_zero() -> None:
    """boston-winter's resupply scorer wrote no route summary. A zero there would be a
    plausible number that survives review; `None` is a bound that cannot run, and says so."""
    boston = measured_from_digest(_digest("boston-winter"))

    assert boston["max_dry_gap_min"] is None
    assert unmeasured({"max_dry_gap_min_max": 60}, boston) == ["max_dry_gap_min_max"]


# --- the four properties, at the moment of writing ---------------------------


def test_a_verdict_that_holds_is_accepted() -> None:
    assert _check() == []


def test_a_no_whose_bounds_all_hold_is_refused_and_the_message_says_what_a_bound_is() -> None:
    """Property 1, and the mistake it was written for: the natural way to write a `no` is
    to describe what is wrong, which makes every bound hold."""
    problems = _check(would_run="no", bounds={"fraction_lts3_plus_min": 0.01})

    assert any("the envelope you would accept" in p for p in problems)


def test_a_no_that_breaches_its_own_bound_is_accepted() -> None:
    assert _check(would_run="no", bounds={"gain_m_max": 20}) == []


def test_a_yes_already_outside_its_bounds_is_refused() -> None:
    problems = _check(bounds={"lts4_count_max": 0})

    assert problems == ["labelled 'yes' but already outside its bounds: lts4_count 1 > 0"]


def test_a_yes_needs_the_plan_to_have_listed_its_residual_flags() -> None:
    problems = _check(measured={**MEASURED, "residual_flags": None})

    assert any("residual flags" in p for p in problems)


def test_a_reason_has_to_be_a_sentence() -> None:
    assert any("at least 12" in p for p in _check(reason="too busy"))


def test_a_misspelled_bound_is_refused_before_anything_is_checked() -> None:
    problems = _check(bounds={"fraction_lts3plus_max": 0.1})

    assert len(problems) == 1 and "unknown bound" in problems[0]


def test_a_bound_has_to_be_a_number() -> None:
    """`true` is an int to Python, and a YAML `yes` typed into a bound would compare."""
    assert any("must be numbers" in p for p in _check(bounds={"lts4_count_max": True}))


def test_a_bound_on_a_number_the_route_does_not_have_is_refused() -> None:
    problems = _check(bounds={"detour_ratio_max": 1.2})

    assert any("could never be checked" in p for p in problems)


def test_no_bounds_and_no_gap_is_a_label_that_rests_on_nothing() -> None:
    assert any("at least one bound" in p for p in _check(bounds={}))


# --- gap: --------------------------------------------------------------------


def test_a_no_that_nothing_measures_is_recorded_with_gap_rather_than_refused() -> None:
    """The finding worth keeping: a runner said no for a reason no scorer reads."""
    assert _check(would_run="no", bounds={}, gap="the path floods at high tide") == []


def test_a_gap_is_only_for_a_no() -> None:
    assert any("is for a 'no'" in p for p in _check(gap="scenery"))


def test_a_gap_that_a_stated_bound_already_explains_is_refused() -> None:
    problems = _check(would_run="no", bounds={"gain_m_max": 20}, gap="too hilly")

    assert any("drop gap:" in p for p in problems)


def test_a_blank_gap_is_refused() -> None:
    assert any("blank" in p for p in _check(would_run="no", bounds={}, gap="  "))


# --- the record --------------------------------------------------------------


def test_a_verdict_can_only_say_a_person_wrote_it() -> None:
    """The type is the guard: this path cannot represent a `claude` label at all."""
    assert _verdict().by == "human"
    with pytest.raises(ValidationError):
        _verdict(by="claude")


@pytest.mark.parametrize("name", ["", "has space", "../up", "a" * 41, "-leading"])
def test_a_labeler_has_to_be_safe_in_a_path_and_a_file_name(name: str) -> None:
    with pytest.raises(ValidationError):
        _verdict(labeler=name)


def test_the_same_verdict_twice_is_one_record() -> None:
    assert _verdict().id == _verdict().id
    assert _verdict().id != _verdict(would_run="unsure").id


# --- the ledger --------------------------------------------------------------


def test_the_suite_never_writes_to_a_runners_real_ledger() -> None:
    """The conftest guard, checked from inside a test rather than trusted."""
    assert labels_dir_from_env() != DEFAULT_LABELS_DIR
    assert "labels" in labels_dir_from_env().parts


def test_the_ledger_lives_in_the_runners_home_unless_told_otherwise(tmp_path: Path) -> None:
    assert labels_dir_from_env({}) == Path.home() / ".longrun" / "labels"
    assert labels_dir_from_env({"LONGRUN_LABELS_DIR": str(tmp_path)}) == tmp_path


def test_nobody_is_labelling_until_somebody_says_who() -> None:
    assert labeler_from_env({}) is None
    assert labeler_from_env({"LONGRUN_LABELER": " runner-1 "}) == "runner-1"


def test_a_verdict_and_the_plan_it_judged_are_kept_together(tmp_path: Path) -> None:
    plan_json = '{"id": "p1"}'
    verdict = _verdict(plan_sha256=hashlib.sha256(plan_json.encode()).hexdigest())
    ledger = Ledger(tmp_path)

    path = ledger.record_verdict(verdict, plan_json)

    assert path.name == f"{verdict.id}.json"
    assert (tmp_path / "verdicts" / f"{verdict.id}.plan.json").read_text() == plan_json
    assert ledger.verdicts() == [verdict]


def test_a_plan_that_is_not_the_one_the_verdict_names_is_refused(tmp_path: Path) -> None:
    verdict = _verdict(plan_sha256="0" * 64)

    with pytest.raises(ValueError, match="names plan"):
        Ledger(tmp_path).record_verdict(verdict, '{"id": "p1"}')


def test_an_empty_ledger_has_no_verdicts(tmp_path: Path) -> None:
    assert Ledger(tmp_path / "nowhere").verdicts() == []


# --- pairs on disk -----------------------------------------------------------


def test_a_pair_keeps_whose_it_is_through_the_round_trip(tmp_path: Path) -> None:
    a = RouteSummary.from_ways("a", [(WayClass(lts=3), 500.0)])
    b = RouteSummary.from_ways("b", [(WayClass(lts=1, is_path=True), 560.0)])
    pair = PreferencePair(a=a, b=b, preferred="b", source="judged", weight=0.5, labeler="r1")
    (tmp_path / "p.yaml").write_text(dump_pairs([pair]), encoding="utf-8")

    (restored,) = read_pairs(tmp_path)

    assert restored.labeler == "r1"
    assert restored.source == "judged" and restored.weight == 0.5
    assert restored.b.metres_by_class == b.metres_by_class


def test_a_pair_file_holds_numbers_and_no_coordinates() -> None:
    """ADR 0020's summary-only format is also what makes a pair publishable from a public
    repository when the two routes start at somebody's door."""
    a = RouteSummary.from_ways("a", [(WayClass(lts=2), 500.0)])
    text = dump_pairs([PreferencePair(a=a, b=a, preferred="a", source="judged")])

    assert "lat" not in text and "lon" not in text and "coordinates" not in text
