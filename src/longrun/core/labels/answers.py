"""Recording a person's answer to a pair, for every door they use (ADR 0048, 0049).

`longrun label pair` and `POST /api/labels/tasks/{id}/answer` both end here, for the reason
`verdicts.record_verdict` gives: a second copy of the rule in a handler drifts, and the copy
that drifts is the one a person is looking at.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from pydantic import ValidationError

from longrun.core.labels.ledger import Ledger
from longrun.core.labels.pairs import PairAnswer, PairTask, answer_problems
from longrun.core.labels.records import Via


class AnswerRefused(ValueError):
    """An answer that was not recorded, and why, in words a person can act on."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def record_pair_answer(
    ledger: Ledger,
    task_id: str,
    *,
    prefer: str,
    strength: str | None,
    reason: str | None,
    labeler: str,
    labelled_at: datetime,
    via: Via,
) -> PairAnswer:
    """Keep an answer, and the pair it is evidence for when it names a line.

    Raises `AnswerRefused` for an unknown task or a malformed answer, and
    `ledger.AlreadyAnswered` for a task that has one: a first answer stands.
    """
    task = ledger.task(task_id)
    if task is None:
        raise AnswerRefused([f"no task {task_id!r} in the ledger"])
    problems = answer_problems(prefer, strength)
    if problems:
        raise AnswerRefused(problems)
    try:
        answer = PairAnswer(
            task_id=task.id,
            prefer=prefer,  # type: ignore[arg-type]  # checked by answer_problems
            strength=strength,  # type: ignore[arg-type]
            reason=(reason or "").strip() or None,
            a_id=task.a.id,
            b_id=task.b.id,
            labeler=labeler,
            labelled_at=labelled_at,
            via=via,
        )
    except ValidationError as exc:
        raise AnswerRefused([error["msg"] for error in exc.errors()]) from None
    return ledger.record_answer(task, answer)


def probe_agreement(tasks: Iterable[PairTask], answers: Iterable[PairAnswer]) -> tuple[int, int]:
    """(agreeing, answered) over the probes whose original is answered too.

    A probe is its original with A and B swapped, so agreement is a flipped letter, or the
    same "none" or "cant". Two answers a person would give differently on a second look are
    what this measures; a fit resting on answers that disagree with themselves half the time
    is resting on noise, and the status line says how often.
    """
    by_task = {answer.task_id: answer for answer in answers}
    flipped = {"a": "b", "b": "a", "none": "none", "cant": "cant"}
    agreeing = total = 0
    for task in tasks:
        if task.repeat_of is None:
            continue
        original, again = by_task.get(task.repeat_of), by_task.get(task.id)
        if original is None or again is None:
            continue
        total += 1
        agreeing += int(flipped[original.prefer] == again.prefer)
    return agreeing, total


def next_task(ledger: Ledger) -> PairTask | None:
    """The first unanswered task in the order a session should ask them."""
    from longrun.core.labels.pairs import order

    answered = {answer.task_id for answer in ledger.answers()}
    queue = order(task for task in ledger.tasks() if task.id not in answered)
    return queue[0] if queue else None


__all__ = ["AnswerRefused", "next_task", "probe_agreement", "record_pair_answer"]
