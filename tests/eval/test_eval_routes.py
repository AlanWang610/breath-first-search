"""Whether a plan is any good, as distinct from whether its numbers changed.

`tests/golden/` pins measurements. This pins *acceptance*: the README has said since M0
that the job here is "to catch arbitration regressions that leave every number in range but
the route unrunnable", and M5's loop is what made that failure reachable - it can now adopt
a candidate, lock a span and park on a question, and until this suite existed nothing
measured whether the route it produced was worth running.

**A human label arrives only through `longrun label`** (ADR 0048), which records it in the
runner's own ledger and publishes it here. `test_the_suite_reports_how_many_labels_a_person_
wrote` prints the count; it was zero for eleven milestones, and that was the honest state
rather than an oversight: a milestone that generates its own ground truth and then passes
against it has tested nothing (ADR 0022). What a case *can* do without a person is hold the
published acceptance metrics inside bounds that a judgement was written against, and refuse
a label that no number supports. The rules live in `core.labels.bounds`, because the door a
person writes through has to refuse the same labels this suite would.

Metrics are read from each golden's `expected.json` rather than re-derived. Those files are
the published record of what the scorers say, the goldens are already scope 7.1's reference
routes, and re-running five routes here would double a three-minute suite to measure numbers
that are already written down.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from longrun.core.labels.bounds import (
    BOUND_NAMES,
    MIN_REASON_WORDS,
    Measured,
    breaches,
    measured_from_digest,
    unmeasured,
)

CASES_DIR = Path(__file__).parent / "cases"
GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden" / "routes"

CASE_NAMES = sorted(path.name for path in CASES_DIR.iterdir() if (path / "label.yaml").is_file())


def _label(name: str) -> dict[str, Any]:
    data = yaml.safe_load((CASES_DIR / name / "label.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{name}: label.yaml is not a mapping"
    return data


def _measured(label: dict[str, Any]) -> Measured:
    """The published metrics for the route a case names.

    Read by the same function the label door uses on a live plan
    (`core.labels.bounds`), so a verdict accepted when it was written is checked here
    against the same numbers rather than a second reading of them.
    """
    return measured_from_digest(
        json.loads((GOLDEN_DIR / label["golden"] / "expected.json").read_text(encoding="utf-8"))
    )


def _breaches(label: dict[str, Any], measured: Measured) -> list[str]:
    return breaches(label.get("bounds") or {}, measured)


# --- the suite is not empty --------------------------------------------------


def test_the_eval_suite_is_not_empty() -> None:
    """The assertion whose absence let `tests/eval/` be a README for six milestones, and
    which `tests/golden/` learned to carry in M1.7 for exactly the same reason."""
    assert CASE_NAMES, "tests/eval/cases holds no case with a label.yaml"


def test_the_suite_reports_how_many_labels_a_person_wrote(capsys: Any) -> None:
    """Zero today, and printed rather than asserted.

    Asserting it were greater than zero would fail the build for an honest state of
    affairs; asserting nothing would let this directory quietly look like ground truth.
    Printing it puts the number in front of whoever runs the suite (ADR 0022).
    """
    authors = [_label(name).get("by") for name in CASE_NAMES]
    human = sum(1 for author in authors if author == "human")

    with capsys.disabled():
        print(f"\neval: {human} of {len(CASE_NAMES)} labels written by a person")

    assert all(author in ("human", "claude", "synthetic") for author in authors)


# --- what every case owes ----------------------------------------------------


@pytest.mark.parametrize("name", CASE_NAMES)
def test_every_case_names_its_author_and_its_reason(name: str) -> None:
    """A label with no reason is unreviewable a month later, and the reason is what lets a
    case survive a scorer change rather than being regenerated with it."""
    label = _label(name)

    assert label.get("would_run") in ("yes", "no", "unsure")
    assert label.get("by") in ("human", "claude", "synthetic")
    assert len(str(label.get("reason", "")).split()) >= MIN_REASON_WORDS, (
        "a reason must be a sentence"
    )
    if label["by"] == "human":
        # ADR 0022, amended: a person's label says which person, because ADR 0021 fits a
        # vector for one runner and never for everybody.
        assert label.get("labeler"), f"{name} is a human label that does not say whose"


@pytest.mark.parametrize("name", CASE_NAMES)
def test_every_bound_is_one_the_suite_actually_checks(name: str) -> None:
    """A misspelled bound is a bound that silently never runs - the one failure mode a
    suite of assertions cannot detect from inside itself."""
    label = _label(name)
    stated = set((label.get("bounds") or {}).keys())

    if not label.get("gap"):
        assert stated, f"{name} states no bounds and no gap:, so its label rests on nothing"
    assert stated <= BOUND_NAMES, f"{name}: unknown bound(s) {sorted(stated - BOUND_NAMES)}"


@pytest.mark.parametrize("name", CASE_NAMES)
def test_no_bound_names_a_number_its_route_does_not_have(name: str) -> None:
    """A bound on an unmeasured metric was skipped rather than failed until M23, which is
    the silent-bound failure `BOUND_NAMES` exists to prevent arriving by a second route:
    loop-bayarea stated `detour_ratio_max: 1.15` from M6 on, and no golden digest has ever
    carried a detour ratio for it to be checked against."""
    label = _label(name)
    missing = unmeasured(label.get("bounds") or {}, _measured(label))

    assert not missing, f"{name}: {missing} can never be checked; its route has no such value"


@pytest.mark.parametrize("name", CASE_NAMES)
def test_every_case_points_at_a_route_that_exists(name: str) -> None:
    assert (GOLDEN_DIR / _label(name)["golden"] / "expected.json").is_file()


# --- the four properties -----------------------------------------------------


@pytest.mark.parametrize("name", CASE_NAMES)
def test_the_metrics_stay_inside_the_bounds_the_label_was_written_against(name: str) -> None:
    """Property 3, and the change detector this suite exists to be: a rearbitration that
    doubles a route's LTS>=3 fraction fails here even when every scorer still agrees with
    its own golden."""
    label = _label(name)
    if label["would_run"] == "no":
        pytest.skip("a route labelled 'no' is checked by the test below instead")

    breaches = _breaches(label, _measured(label))

    assert not breaches, f"{name} is labelled runnable and now breaches: {breaches}"


@pytest.mark.parametrize("name", CASE_NAMES)
def test_a_route_nobody_would_run_fails_a_bound_that_says_why(name: str) -> None:
    """Property 1. A `would_run: no` whose numbers all look fine is a label nobody can
    check, and it would survive the very regression it was written to catch."""
    label = _label(name)
    if label["would_run"] != "no":
        pytest.skip("only routes labelled 'no' owe a breach")
    if label.get("gap"):
        pytest.skip("a gap: label says no number captures why; the test below checks it")

    assert _breaches(label, _measured(label)), (
        f"{name} is labelled unrunnable and no stated bound says why"
    )


@pytest.mark.parametrize("name", CASE_NAMES)
def test_a_gap_stays_a_gap_until_a_number_says_why(name: str) -> None:
    """Property 1's exception, and the reason it is not a hole in it.

    A person's `no` that no measured number captures is recorded rather than refused -
    that is a finding about the scorers, and refusing it would throw the finding away. But
    the exception has to close itself: once a bound the label states breaches, a number
    does say why, and the case should stop claiming otherwise. So this fails exactly when
    property 1 would start to pass.
    """
    label = _label(name)
    if not label.get("gap"):
        pytest.skip("only a gap: label owes this")

    assert label["would_run"] == "no", f"{name}: gap: is only for a 'no'"
    assert not _breaches(label, _measured(label)), (
        f"{name}: a stated bound now breaches ({_breaches(label, _measured(label))}), so a "
        "number says why this route is a 'no' - drop gap: and let property 1 check it"
    )


@pytest.mark.parametrize("name", CASE_NAMES)
def test_a_runnable_route_is_not_hiding_a_residual_hard_flag(name: str) -> None:
    """Property 2. Scope 8.4: residual flags are *always* listed. A plan worth running may
    carry residual flags - most do - but the count has to be present, because "none listed"
    and "nobody listed them" are the same text and different facts."""
    label = _label(name)
    if label["would_run"] != "yes":
        pytest.skip("only routes labelled 'yes' owe this")

    assert _measured(label)["residual_flags"] is not None, (
        f"{name} is labelled runnable and its plan lists no residual flags at all"
    )


def test_a_label_survives_yamls_opinion_about_the_word_no() -> None:
    """YAML 1.1 reads a bare `yes`/`no` as a boolean, so `would_run: no` arrives as `False`
    and matches none of the three legal values. Every label quotes it; this is what keeps
    the next one doing so, because the failure is a type error a long way from its cause.
    """
    for name in CASE_NAMES:
        value = _label(name)["would_run"]
        assert isinstance(value, str), f"{name}: would_run came back as {value!r}, not a string"
