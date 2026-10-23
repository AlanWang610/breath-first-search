"""Recording a verdict: one function, for every door a person uses (ADR 0048).

`longrun label verdict` and `POST /api/plans/{id}/verdict` both end here. The API adds no
capability (scope 3.9), and the way that stops being true is a second copy of "measure,
check, record" in a handler that drifts from the first - so neither door has one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from longrun.core.labels.bounds import check_verdict, measured_from_plan
from longrun.core.labels.ledger import Ledger
from longrun.core.labels.records import Verdict, Via


class VerdictRefused(ValueError):
    """A verdict that was not recorded, and every reason why, in words a person can act on."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def record_verdict(
    ledger: Ledger,
    plan_json: str,
    *,
    golden: str | None,
    would_run: str,
    reason: str,
    bounds: Mapping[str, Any],
    gap: str | None,
    note: str | None,
    labeler: str,
    labelled_at: datetime,
    via: Via,
) -> Verdict:
    """Measure the plan as it was shown, check the verdict against it, and keep both.

    Raises `VerdictRefused` with every problem at once rather than the first: a person
    fixing a form should see the whole list, not discover it one submit at a time.
    """
    from longrun.core.models.plan import Plan

    plan = Plan.model_validate_json(plan_json)
    measured = measured_from_plan(plan)
    problems = check_verdict(
        would_run=would_run, reason=reason, bounds=bounds, gap=gap, measured=measured
    )
    if problems:
        raise VerdictRefused(problems)
    try:
        verdict = Verdict(
            subject=golden or plan.id,
            golden=golden,
            would_run=would_run,  # type: ignore[arg-type]  # checked by check_verdict
            reason=reason,
            bounds={name: float(limit) for name, limit in bounds.items()},
            gap=gap,
            note=note,
            measured=measured,
            plan_sha256=hashlib.sha256(plan_json.encode("utf-8")).hexdigest(),
            labeler=labeler,
            labelled_at=labelled_at,
            via=via,
        )
    except ValidationError as exc:
        raise VerdictRefused([error["msg"] for error in exc.errors()]) from None
    ledger.record_verdict(verdict, plan_json)
    return verdict


def verdicts_on(ledger: Ledger, plan_json: str) -> list[Verdict]:
    """Verdicts written about exactly this plan - the same bytes, not the same id.

    A stored plan's id survives every edit gesture while its route and numbers change, so
    "you judged this before" is only true of the content that was judged.
    """
    digest = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
    return [verdict for verdict in ledger.verdicts() if verdict.plan_sha256 == digest]


__all__ = ["VerdictRefused", "record_verdict", "verdicts_on"]
