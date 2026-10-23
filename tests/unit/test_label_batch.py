"""A batch answered on a hosted page, and the answers it sends home (ADR 0050).

What is pinned: the page's half carries nothing the Review tab would not show (no vector
names, no probe flags) and asks in the session's order; an import records a person's answers
through the same function as every other door, as `via: page` at the time they clicked; a
file imported twice records nothing new, and an answer the ledger already holds stands.
Every ledger here is under the test's own directory (`conftest._labels_stay_out_of_home`).
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.unit.test_label_pairs import NOW, _task
from typer.testing import CliRunner

from longrun.cli.main import app
from longrun.core.labels.answers import AnswerRefused
from longrun.core.labels.batch import (
    AnswersFile,
    Batch,
    batches_dir,
    import_answers,
    make_batch,
    page_payload,
)
from longrun.core.labels.ledger import Ledger, labels_dir_from_env
from longrun.core.labels.pairs import PairTask, probe
from longrun.core.labels.pairs_io import read_pairs


def _batch() -> Batch:
    original = _task("t1", "unpaved", 0.5)
    return make_batch(
        "20261009-abc123",
        [probe(original, "p1", NOW), original, _task("t2", "path_bonus", 0.6)],
        NOW,
    )


def _answer(task: PairTask, prefer: str, strength: str | None = "clear", **extra: Any) -> dict:
    return {
        "task_id": task.id,
        "a_id": task.a.id,
        "b_id": task.b.id,
        "prefer": prefer,
        "strength": strength,
        "reason": None,
        "labelled_at": (NOW + timedelta(minutes=5)).isoformat(),
        **extra,
    }


def _file(batch: Batch, *answers: dict) -> AnswersFile:
    return AnswersFile.model_validate(
        {"kind": "longrun-pair-answers", "version": 1, "batch": batch.id, "answers": answers}
    )


def _tasks(batch: Batch) -> dict[str, PairTask]:
    return {task.id: task for task in batch.tasks}


# --- the page's half ---------------------------------------------------------------


def test_the_page_never_learns_which_vector_drew_a_line_or_which_question_is_asked_twice() -> None:
    text = json.dumps(page_payload(_batch()))

    for leak in ("drawn_by", "neutral", "seek-paths", "repeat_of", "probe", "stratum", "split"):
        assert leak not in text, f"the page payload carries {leak!r}"


def test_the_page_asks_in_the_sessions_order_with_the_probe_last() -> None:
    payload = page_payload(_batch())

    # Round-robin across strata in name order (`pairs.order`), so path_bonus's one question
    # comes before unpaved's; the probe of t1 waits for everything else.
    assert [q["id"] for q in payload["questions"]] == ["t2", "t1", "p1"]


def test_the_page_holds_each_lines_shares_and_candidate_ids_for_after_an_answer() -> None:
    batch = _batch()
    (first, *_) = page_payload(batch)["questions"]
    task = _tasks(batch)[first["id"]]

    assert first["shares"] == {"a": task.a.shares(), "b": task.b.shares()}
    assert (first["a"]["id"], first["b"]["id"]) == (task.a.id, task.b.id)
    assert first["a"]["points"] and "diverges" in first["a"]


# --- the answers coming home -------------------------------------------------------


def test_an_import_records_the_persons_answers_at_the_time_they_clicked(tmp_path: Path) -> None:
    batch, ledger = _batch(), Ledger(tmp_path)
    tasks = _tasks(batch)

    report = import_answers(
        ledger,
        batch,
        _file(batch, _answer(tasks["t1"], "b"), _answer(tasks["t2"], "none", None)),
        labeler="runner-1",
    )

    assert (len(report.recorded), report.tasks_added, report.refused) == (2, 2, [])
    held = ledger.answer("t1")
    assert held is not None
    assert (held.prefer, held.via, held.labeler) == ("b", "page", "runner-1")
    assert held.labelled_at == NOW + timedelta(minutes=5)
    assert [t.id for t in ledger.tasks()] == ["t1", "t2"], "an unanswered task stays out"
    (pair,) = read_pairs(ledger.pairs_dir)
    assert (pair.preferred, pair.source) == ("b", "judged")


def test_a_file_imported_twice_records_nothing_the_second_time(tmp_path: Path) -> None:
    batch, ledger = _batch(), Ledger(tmp_path)
    answers = _file(batch, _answer(_tasks(batch)["t1"], "a", "slight", reason=" steep "))

    import_answers(ledger, batch, answers, labeler="runner-1")
    again = import_answers(ledger, batch, answers, labeler="runner-1")

    assert (again.recorded, again.already, again.kept_first) == ([], ["t1"], [])
    assert len(ledger.answers()) == 1


def test_an_answer_the_ledger_holds_differently_is_kept_and_reported(tmp_path: Path) -> None:
    batch, ledger = _batch(), Ledger(tmp_path)
    task = _tasks(batch)["t1"]
    import_answers(ledger, batch, _file(batch, _answer(task, "a")), labeler="runner-1")

    later = import_answers(ledger, batch, _file(batch, _answer(task, "b")), labeler="runner-1")

    assert later.kept_first == ["t1"]
    assert ledger.answer("t1").prefer == "a"  # type: ignore[union-attr]


def test_an_answer_about_other_lines_or_another_batch_is_refused(tmp_path: Path) -> None:
    batch, ledger = _batch(), Ledger(tmp_path)
    task = _tasks(batch)["t1"]

    swapped = import_answers(
        ledger,
        batch,
        _file(batch, _answer(task, "a", a_id=task.b.id, b_id=task.a.id)),
        labeler="r1",
    )
    stranger = import_answers(
        ledger, batch, _file(batch, _answer(_task("zz", "unpaved", 0.5), "a")), labeler="r1"
    )
    other = _file(batch, _answer(task, "a")).model_copy(update={"batch": "20261009-other"})

    assert swapped.refused[0][0] == "t1" and stranger.refused[0][0] == "zz"
    with pytest.raises(AnswerRefused, match="not 20261009-abc123"):
        import_answers(ledger, batch, other, labeler="r1")
    assert ledger.answers() == [] and ledger.tasks() == []


def test_a_choice_without_a_strength_is_refused_in_the_ledgers_words(tmp_path: Path) -> None:
    batch, ledger = _batch(), Ledger(tmp_path)

    report = import_answers(
        ledger, batch, _file(batch, _answer(_tasks(batch)["t1"], "a", None)), labeler="r1"
    )

    assert report.refused == [("t1", ["say how strongly: slight or clear"])]
    assert ledger.answers() == []


# --- the CLI -----------------------------------------------------------------------


def test_batch_then_import_through_the_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ledger = Ledger(labels_dir_from_env())
    original = _task("t1", "unpaved", 0.5)
    for task in (original, _task("t2", "path_bonus", 0.6), probe(original, "p1", NOW)):
        ledger.add_task(task)

    made = CliRunner().invoke(app, ["label", "batch"])

    assert made.exit_code == 0, made.output
    assert "3 question(s), 1 of them asked twice" in made.output
    (page_path,) = batches_dir(ledger.root).glob("*.page.json")
    page = json.loads(page_path.read_text(encoding="utf-8"))
    assert (batches_dir(ledger.root) / f"{page['batch']}.json").is_file()
    answers_path = tmp_path / "answers.json"
    first = page["questions"][0]
    answers_path.write_text(
        json.dumps(
            {
                "kind": "longrun-pair-answers",
                "version": 1,
                "batch": page["batch"],
                "answers": [
                    {
                        "task_id": first["id"],
                        "a_id": first["a"]["id"],
                        "b_id": first["b"]["id"],
                        "prefer": "b",
                        "strength": "clear",
                        "reason": "the trail",
                        "labelled_at": "2026-10-09T17:03:11Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    nobody = CliRunner().invoke(app, ["label", "import", str(answers_path)])
    monkeypatch.setenv("LONGRUN_LABELER", "runner-1")
    imported = CliRunner().invoke(app, ["label", "import", str(answers_path)])
    again = CliRunner().invoke(app, ["label", "import", str(answers_path)])

    assert nobody.exit_code == 2 and "LONGRUN_LABELER" in nobody.output
    assert imported.exit_code == 0, imported.output
    assert "recorded 1 answer(s) by runner-1" in imported.output
    assert again.exit_code == 0 and "1 already in the ledger" in again.output
    (answer,) = ledger.answers()
    assert (answer.task_id, answer.via, answer.reason) == (first["id"], "page", "the trail")


def test_a_limited_batch_keeps_the_probes_of_the_questions_it_kept(tmp_path: Path) -> None:
    ledger = Ledger(labels_dir_from_env())
    first, second = _task("t1", "unpaved", 0.5), _task("t2", "unpaved", 0.9)
    for task in (first, second, probe(first, "p1", NOW), probe(second, "p2", NOW)):
        ledger.add_task(task)

    result = CliRunner().invoke(app, ["label", "batch", "--limit", "1", "--out", str(tmp_path)])

    assert result.exit_code == 0, result.output
    (page_path,) = tmp_path.glob("*.page.json")
    page = json.loads(page_path.read_text(encoding="utf-8"))
    assert [q["id"] for q in page["questions"]] == ["t1", "p1"]


def test_a_batch_can_be_aimed_at_the_parameters_left_unpinned(tmp_path: Path) -> None:
    ledger = Ledger(labels_dir_from_env())
    street, trail = _task("t1", "lts4", 0.5), _task("t2", "path_bonus", 0.5)
    for task in (street, trail, probe(street, "p1", NOW), probe(trail, "p2", NOW)):
        ledger.add_task(task)

    aimed = CliRunner().invoke(
        app, ["label", "batch", "--stratum", "lts4", "--stratum", "lts3", "--out", str(tmp_path)]
    )
    typo = CliRunner().invoke(app, ["label", "batch", "--stratum", "lts5"])

    assert aimed.exit_code == 0, aimed.output
    (page_path,) = tmp_path.glob("*.page.json")
    page = json.loads(page_path.read_text(encoding="utf-8"))
    assert [q["id"] for q in page["questions"]] == ["t1", "p1"]
    assert typo.exit_code == 2 and "lts5" in typo.output


def test_import_says_where_it_looked_for_a_batch_it_cannot_find(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LONGRUN_LABELER", "runner-1")
    answers_path = tmp_path / "answers.json"
    answers_path.write_text(
        json.dumps({"kind": "longrun-pair-answers", "version": 1, "batch": "gone", "answers": []}),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["label", "import", str(answers_path)])

    assert result.exit_code == 2
    assert "no batch gone" in result.output and "--tasks" in result.output
