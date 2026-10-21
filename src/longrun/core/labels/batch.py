"""A batch of pair questions answered on a hosted page, and the answers coming home (ADR 0050).

A page on claude.ai cannot reach `~/.longrun/labels/`, and nothing but the person should carry
an answer there (ADR 0048). So a batch travels in two halves, and the answers come back by a
command the person runs:

* **The page's half** is `page_payload`: each task as `review.task_view` lets a browser see
  it, in the order a session asks (`pairs.order`), plus the shares the page shows *after* an
  answer. Never `drawn_by`, and never which question is a probe - the Review tab's rules,
  applied to a page that holds the whole batch at once instead of fetching one task.
* **This machine's half** is the `Batch` itself, full tasks included, kept beside the ledger
  in `batches/<id>.json`.
* **The answers** come back as a file the page offers to save. `import_answers` adds each
  answered task to the ledger and records the answer through `answers.record_pair_answer`,
  the function every other door ends in, with the time the person clicked and `via: page`.

A first answer stands here too: an answer the ledger already holds is reported, never
replaced, and importing the same file twice records nothing the second time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from longrun.core.labels.answers import AnswerRefused, record_pair_answer
from longrun.core.labels.ledger import TASK_ID_PATTERN, Ledger
from longrun.core.labels.pairs import PairAnswer, PairTask, Prefer, Strength, order
from longrun.core.labels.review import task_view

PAGE_KIND = "longrun-pair-batch"
ANSWERS_KIND = "longrun-pair-answers"
FORMAT_VERSION = 1


class Batch(BaseModel):
    """The questions one page asks, as this machine keeps them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=TASK_ID_PATTERN.pattern)
    created_at: AwareDatetime
    tasks: list[PairTask]


def make_batch(batch_id: str, tasks: list[PairTask], created_at: Any) -> Batch:
    """The tasks in the order a session asks them, probes last (`pairs.order`)."""
    return Batch(id=batch_id, created_at=created_at, tasks=order(tasks))


def page_payload(batch: Batch) -> dict[str, Any]:
    """What the page may hold: two lines per question, and the shares it shows after an answer.

    Built from `review.task_view`, so the line geometry and the stretches where the lines
    part are the Review tab's own. `region` and `probe` are dropped: the page needs neither,
    and a page that said which questions are asked twice would tell the person which of
    their answers are being checked.
    """
    questions = []
    for task in batch.tasks:
        view = task_view(task, None)
        questions.append(
            {
                "id": task.id,
                "a": {"id": task.a.id, **view["a"]},
                "b": {"id": task.b.id, **view["b"]},
                "shares": {"a": task.a.shares(), "b": task.b.shares()},
            }
        )
    return {
        "kind": PAGE_KIND,
        "version": FORMAT_VERSION,
        "batch": batch.id,
        "created_at": batch.created_at.isoformat(),
        "questions": questions,
    }


class PageAnswer(BaseModel):
    """One answer as the page writes it into the file it offers to save."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    task_id: str
    a_id: str
    b_id: str
    prefer: Prefer
    strength: Strength | None = None
    reason: str | None = None
    labelled_at: AwareDatetime


class AnswersFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: Literal["longrun-pair-answers"]
    version: Literal[1]
    batch: str
    answers: list[PageAnswer]


@dataclass
class ImportReport:
    recorded: list[PairAnswer] = field(default_factory=list)
    tasks_added: int = 0
    #: Answers the ledger already holds, word for word: a file imported twice.
    already: list[str] = field(default_factory=list)
    #: Answers the ledger holds differently. The ledger's stands (ADR 0049).
    kept_first: list[str] = field(default_factory=list)
    refused: list[tuple[str, list[str]]] = field(default_factory=list)


def _same(held: PairAnswer, given: PageAnswer) -> bool:
    return (held.prefer, held.strength, held.reason, held.labelled_at) == (
        given.prefer,
        given.strength,
        (given.reason or "").strip() or None,
        given.labelled_at,
    )


def import_answers(
    ledger: Ledger, batch: Batch, answers: AnswersFile, *, labeler: str
) -> ImportReport:
    """Record a page's answers in the ledger, adding each answered task first.

    Only answered tasks are added: an unanswered one stays in the batch file, to be asked by
    the page and imported later, rather than appearing in `label queue` as a second copy of
    a question the page is still asking.
    """
    if answers.batch != batch.id:
        raise AnswerRefused([f"these answers are for batch {answers.batch}, not {batch.id}"])
    report = ImportReport()
    tasks = {task.id: task for task in batch.tasks}
    for given in sorted(answers.answers, key=lambda a: (a.labelled_at, a.task_id)):
        task = tasks.get(given.task_id)
        if task is None:
            report.refused.append((given.task_id, [f"no task {given.task_id!r} in this batch"]))
            continue
        if (given.a_id, given.b_id) != (task.a.id, task.b.id):
            report.refused.append((task.id, ["the answer is about lines this task does not have"]))
            continue
        held_task = ledger.task(task.id)
        if held_task is not None and held_task != task:
            report.refused.append((task.id, ["the ledger holds a different task under this id"]))
            continue
        held = ledger.answer(task.id)
        if held is not None:
            (report.already if _same(held, given) else report.kept_first).append(task.id)
            continue
        if held_task is None:
            ledger.add_task(task)
            report.tasks_added += 1
        try:
            report.recorded.append(
                record_pair_answer(
                    ledger,
                    task.id,
                    prefer=given.prefer,
                    strength=given.strength,
                    reason=given.reason,
                    labeler=labeler,
                    labelled_at=given.labelled_at,
                    via="page",
                )
            )
        except AnswerRefused as refused:
            report.refused.append((task.id, refused.problems))
    return report


def batches_dir(labels_dir: Path) -> Path:
    """Beside the ledger, never inside it: a batch is questions, and the ledger is answers."""
    return labels_dir.parent / "batches"


__all__ = [
    "ANSWERS_KIND",
    "FORMAT_VERSION",
    "PAGE_KIND",
    "AnswersFile",
    "Batch",
    "ImportReport",
    "PageAnswer",
    "batches_dir",
    "import_answers",
    "make_batch",
    "page_payload",
]
