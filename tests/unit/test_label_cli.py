"""`longrun label`: the person's door, driven the way a person drives it (ADR 0048).

Every verdict here goes to a ledger under `tmp_path` (`tests/conftest.py::
_labels_stay_out_of_home`), and every publish to a `--cases` directory under `tmp_path`:
nothing in this module can add a label to a real ledger or to `tests/eval`.

`synthetic-hazards` is the golden these judge, for the reasons `ui/gestures/seed.ts` gives:
143 KB, and the one route whose fixtures were written rather than downloaded. It is scored
once per module, through `label show`, and each test gets its own copy of what that wrote.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from longrun.cli.main import app
from longrun.core.labels.bounds import check_verdict, measured_from_digest, measured_from_plan
from longrun.core.labels.ledger import Ledger, labels_dir_from_env
from longrun.core.labels.records import Verdict
from longrun.core.models.plan import Plan
from longrun.goldens import DEFAULT_GOLDENS_DIR

GOLDEN = "synthetic-hazards"
GOLDENS = Path(__file__).resolve().parents[1] / "golden" / "routes"
REASON = (
    "Four LTS 4 crossings in two kilometres is more arterial than I would put in a long run "
    "on purpose."
)


@pytest.fixture(scope="module")
def shown(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """`label show` once, into a ledger of the module's own."""
    ledger = tmp_path_factory.mktemp("shown-ledger")
    result = CliRunner().invoke(
        app,
        ["label", "show", "--golden", GOLDEN, "--goldens", str(GOLDENS)],
        env={"LONGRUN_LABELS_DIR": str(ledger)},
    )
    assert result.exit_code == 0, result.output
    return ledger / "shown" / GOLDEN


@pytest.fixture
def ledger(shown: Path, monkeypatch: pytest.MonkeyPatch) -> Ledger:
    """This test's ledger, with the golden already shown and a labeler set."""
    root = labels_dir_from_env()
    shutil.copytree(shown, root / "shown" / GOLDEN)
    monkeypatch.setenv("LONGRUN_LABELER", "runner-1")
    return Ledger(root)


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(app, ["label", *args])


def _verdict(*extra: str) -> Any:
    return _invoke(
        "verdict",
        "--golden",
        GOLDEN,
        "--would-run",
        "no",
        "--reason",
        REASON,
        "--bound",
        "lts4_count_max=2",
        "--cases",
        str(Path(__file__).resolve().parents[1] / "eval" / "cases"),
        *extra,
    )


# --- show --------------------------------------------------------------------


def test_show_writes_the_sheet_and_the_plan_it_was_rendered_from(shown: Path) -> None:
    assert (shown / "sheet.html").read_text(encoding="utf-8").startswith("<!doctype html>")
    plan = Plan.model_validate_json((shown / "plan.json").read_text(encoding="utf-8"))
    assert plan.segments


def test_the_shown_plan_carries_the_numbers_its_golden_records(shown: Path) -> None:
    """The reason `show` runs the golden's own argv: a verdict written against this plan is
    re-checked against `expected.json`, so the two have to be the same numbers."""
    plan = Plan.model_validate_json((shown / "plan.json").read_text(encoding="utf-8"))
    digest = json.loads((GOLDENS / GOLDEN / "expected.json").read_text(encoding="utf-8"))

    from_plan, from_digest = measured_from_plan(plan), measured_from_digest(digest)
    for metric, value in from_digest.items():
        if value is None:
            assert from_plan[metric] is None, metric
        else:
            assert from_plan[metric] == pytest.approx(value, abs=1e-3), metric


@pytest.mark.parametrize("name", ["../synthetic-hazards", ".hidden", "no-such-route"])
def test_show_takes_a_goldens_name_and_nothing_that_reaches_outside(name: str) -> None:
    result = _invoke("show", "--golden", name, "--goldens", str(GOLDENS))

    assert result.exit_code == 2
    assert "golden" in result.output


def test_the_default_goldens_directory_is_the_one_the_suite_runs() -> None:
    assert (Path(__file__).resolve().parents[2] / DEFAULT_GOLDENS_DIR).is_dir()


# --- verdict: what is refused, and that refusing writes nothing --------------


def test_nobody_is_recorded_until_somebody_says_who(shown: Path) -> None:
    root = labels_dir_from_env()
    shutil.copytree(shown, root / "shown" / GOLDEN)

    result = _verdict()

    assert result.exit_code == 2
    assert "LONGRUN_LABELER" in result.output
    assert Ledger(root).verdicts() == []


def test_a_verdict_is_about_a_plan_somebody_was_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LONGRUN_LABELER", "runner-1")

    result = _verdict()

    assert result.exit_code == 2
    assert "has not been shown" in result.output


def test_a_verdict_names_a_plan(ledger: Ledger, tmp_path: Path) -> None:
    missing = _verdict("--plan", str(tmp_path / "plan.json"))
    neither = _invoke("verdict", "--would-run", "no", "--reason", REASON)

    assert missing.exit_code == 2 and "no such plan" in missing.output
    assert neither.exit_code == 2 and "say which plan" in neither.output
    assert ledger.verdicts() == []


def test_a_stored_plan_seeded_from_a_golden_can_say_so(ledger: Ledger) -> None:
    """The UI judges a stored plan, not the one `show` wrote; `--golden` beside `--plan` is
    how it says which golden that plan came from, and `publish` checks the claim."""
    plan_path = ledger.root / "shown" / GOLDEN / "plan.json"

    result = _verdict("--plan", str(plan_path))

    assert result.exit_code == 0, result.output
    (record,) = ledger.verdicts()
    assert record.golden == GOLDEN


def test_a_stored_plan_cannot_claim_a_golden_that_does_not_exist(ledger: Ledger) -> None:
    plan_path = ledger.root / "shown" / GOLDEN / "plan.json"

    result = _invoke(
        "verdict",
        "--plan",
        str(plan_path),
        "--golden",
        "no-such-route",
        "--would-run",
        "no",
        "--reason",
        REASON,
        "--goldens",
        str(GOLDENS),
    )

    assert result.exit_code == 2
    assert ledger.verdicts() == []


def test_a_no_whose_bounds_all_hold_is_refused_and_nothing_is_written(ledger: Ledger) -> None:
    """The property-1 mistake, caught while the route is still on the screen."""
    result = _invoke(
        "verdict",
        "--golden",
        GOLDEN,
        "--would-run",
        "no",
        "--reason",
        REASON,
        "--bound",
        "lts4_count_max=10",
    )

    assert result.exit_code == 2
    assert "the envelope you would accept" in result.output
    assert "nothing was written" in result.output
    assert ledger.verdicts() == []


def test_a_bound_that_is_not_a_number_is_refused(ledger: Ledger) -> None:
    result = _verdict("--bound", "gain_m_max=lots")

    assert result.exit_code == 2
    assert ledger.verdicts() == []


# --- verdict: what is recorded -----------------------------------------------


def test_a_verdict_is_recorded_with_who_how_and_what_was_shown(ledger: Ledger) -> None:
    result = _verdict()

    assert result.exit_code == 0, result.output
    (record,) = ledger.verdicts()
    plan_json = (ledger.root / "shown" / GOLDEN / "plan.json").read_text(encoding="utf-8")
    assert record.by == "human" and record.labeler == "runner-1" and record.via == "cli"
    assert record.golden == GOLDEN and record.bounds == {"lts4_count_max": 2.0}
    assert record.measured == measured_from_plan(Plan.model_validate_json(plan_json))
    kept = ledger.verdicts_dir / f"{record.id}.plan.json"
    assert kept.read_text(encoding="utf-8") == plan_json


def test_recording_says_what_it_would_replace_in_tests_eval(ledger: Ledger) -> None:
    """synthetic-hazards holds a `by: synthetic` label; a person's answer replaces it on
    publish, and they are told so rather than finding out from the diff."""
    result = _verdict()

    assert "holds a synthetic label" in result.output
    assert "Yours agrees" in result.output


def test_a_stored_plan_with_no_golden_stays_in_the_ledger(ledger: Ledger) -> None:
    plan_path = ledger.root / "shown" / GOLDEN / "plan.json"

    result = _invoke(
        "verdict",
        "--plan",
        str(plan_path),
        "--would-run",
        "no",
        "--reason",
        REASON,
        "--bound",
        "lts4_count_max=2",
    )

    assert result.exit_code == 0, result.output
    (record,) = ledger.verdicts()
    assert record.golden is None
    assert "stays in the ledger" in result.output


# --- publish -----------------------------------------------------------------


def _publish(cases: Path, *extra: str) -> Any:
    return _invoke(
        "publish",
        "--cases",
        str(cases),
        "--pairs",
        str(cases.parent / "pairs"),
        "--goldens",
        str(GOLDENS),
        *extra,
    )


def test_a_dry_run_prints_the_case_and_writes_nothing(ledger: Ledger, tmp_path: Path) -> None:
    _verdict()
    cases = tmp_path / "cases"

    result = _publish(cases, "--dry-run")

    assert result.exit_code == 0, result.output
    assert "would write" in result.output and "by: human" in result.output
    assert not cases.exists()


def test_a_published_case_is_one_the_eval_suite_accepts(ledger: Ledger, tmp_path: Path) -> None:
    """Read back the way `tests/eval` reads it, and checked by the same rule."""
    _verdict()
    cases = tmp_path / "cases"

    result = _publish(cases)

    assert result.exit_code == 0, result.output
    text = (cases / GOLDEN / "label.yaml").read_text(encoding="utf-8")
    label = yaml.safe_load(text)
    digest = json.loads((GOLDENS / GOLDEN / "expected.json").read_text(encoding="utf-8"))
    assert label["would_run"] == "no" and isinstance(label["would_run"], str)
    assert label["by"] == "human" and label["labeler"] == "runner-1"
    assert "T" not in str(label["labelled_on"]), "the date, not the minute"
    assert (
        check_verdict(
            would_run=label["would_run"],
            reason=label["reason"],
            bounds=label["bounds"],
            gap=label.get("gap"),
            measured=measured_from_digest(digest),
        )
        == []
    )


def test_publishing_twice_writes_nothing_the_second_time(ledger: Ledger, tmp_path: Path) -> None:
    _verdict()
    cases = tmp_path / "cases"
    _publish(cases)

    again = _publish(cases)

    assert "wrote 0 case(s)" in again.output


def test_the_latest_verdict_on_a_golden_is_the_one_published(
    ledger: Ledger, tmp_path: Path
) -> None:
    _verdict()
    first = ledger.verdicts()[0]
    later = first.model_copy(
        update={
            "would_run": "no",
            "reason": REASON + " Second look.",
            "labelled_at": datetime(2099, 1, 1, tzinfo=UTC),
        }
    )
    ledger.record_verdict(later)
    cases = tmp_path / "cases"

    _publish(cases)

    assert "Second look." in (cases / GOLDEN / "label.yaml").read_text(encoding="utf-8")


def test_a_verdict_on_numbers_the_golden_no_longer_has_is_not_published(
    ledger: Ledger, tmp_path: Path
) -> None:
    """A plan edited after it was seeded from the golden, or a golden that moved: either
    way the person judged a route the suite would not be checking."""
    _verdict()
    (record,) = ledger.verdicts()
    edited = Verdict.model_validate(
        {
            **record.model_dump(),
            "measured": {**record.measured, "lts4_count": 9},
            "labelled_at": datetime(2099, 1, 1, tzinfo=UTC),
        }
    )
    ledger.record_verdict(edited)
    cases = tmp_path / "cases"

    result = _publish(cases)

    assert "skipped synthetic-hazards" in result.output
    assert "lts4_count: judged 9, golden 4" in result.output
    assert not (cases / GOLDEN).exists()


def test_status_lists_the_latest_verdict_on_each_route(ledger: Ledger) -> None:
    _verdict()

    result = _invoke("status")

    assert result.exit_code == 0
    assert GOLDEN in result.output and "publishable" in result.output
