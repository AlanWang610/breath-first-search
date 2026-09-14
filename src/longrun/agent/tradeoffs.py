"""Scope 8.4's one-liner: what differs between two routes the arithmetic cannot separate.

The model's whole job at this call site is the *sentence*. It does not choose - ADR 0019 -
and `compare` still returns a `TradeOff` and parks the job whatever this says.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from longrun.agent import prompts
from longrun.agent.model import ask, build_agent

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import Budget
    from longrun.core.plan.arbitrate import Candidate

#: Long enough to name two differences and where they are; short enough to read on a
#: phone at the start of a run, which is where a sheet is read.
MAX_WORDS = 40


class Comparison(BaseModel):
    """One sentence, and nothing else."""

    sentence: str = Field(description="One line naming what differs and at what mile.")


def describer(budget: Budget | None = None) -> Any:
    """A `describe` callable for `compare`, or `None` when no model is configured.

    `None` is the deterministic path, and `compare` already owns the fallback string - so
    there is no Null object here reproducing it (ADR 0015).
    """
    from longrun.agent.model import ModelUnavailable

    try:
        agent = build_agent(Comparison, prompts.TRADE_OFF)
    except ModelUnavailable:
        return None

    def describe(a: Candidate, b: Candidate) -> str:
        answer = ask(agent, _prompt(a, b), budget=budget)
        if answer is None:
            # The same string `compare` would have produced. A model that refused, failed
            # or ran out of budget leaves the plan exactly as it would have been.
            return f"{a.label} and {b.label} score within the trade-off margin"
        return str(answer.sentence).strip()

    return describe


def _prompt(a: Candidate, b: Candidate) -> str:
    return "\n".join(
        [
            "Two routes, close enough that the scoring cannot separate them.",
            "",
            _describe_candidate(a),
            "",
            _describe_candidate(b),
            "",
            f"One sentence, at most {MAX_WORDS} words, comparing them for the runner.",
        ]
    )


def _describe_candidate(candidate: Candidate) -> str:
    lines = [f"{candidate.label}:"]
    for flag in candidate.worst(4):
        lines.append(
            f"  - {flag.scorer} {flag.reason_code} "
            f"({flag.kind.name.lower()}/{flag.tier.name.lower()}, severity {flag.severity:.2f})"
        )
    if len(lines) == 1:
        lines.append("  - nothing flagged")
    return "\n".join(lines)


__all__ = ["MAX_WORDS", "Comparison", "describer"]
