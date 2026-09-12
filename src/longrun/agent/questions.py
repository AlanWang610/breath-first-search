"""Scope 6.3's in-context question, phrased.

The fifth call site the package docstring counts and never names. Everything about *when*
to ask is deterministic and lives in `core/preferences/elicitation.py`: the axis must still
be `default`, two candidates must differ mainly on it, and the cap is three per plan. This
module only writes the words, and when there is no model it writes them from a template.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from longrun.agent import prompts
from longrun.agent.model import ask, build_agent

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import Budget


class Question(BaseModel):
    """One question, short enough to answer without scrolling."""

    text: str = Field(description="At most two sentences, naming the choice and the mile.")


def template(axis: str, options: list[str], *, at_km: float) -> str:
    """The deterministic phrasing, and the reason no Null class is needed.

    Plainer than a model would write, and it names the same three things scope 6.3 asks a
    question to name: which option, what differs, and at what mile.
    """
    pair = " or ".join(options) if options else "either route"
    return (
        f"At {at_km:.1f} km the routes differ mainly on {axis.replace('_', ' ')}. "
        f"Which would you rather: {pair}?"
    )


def write(
    axis: str, options: list[str], *, at_km: float, detail: str = "", budget: Budget | None = None
) -> str:
    """The question in words, from a model when there is one and a template when not."""
    from longrun.agent.model import ModelUnavailable

    try:
        agent = build_agent(Question, prompts.QUESTION)
    except ModelUnavailable:
        return template(axis, options, at_km=at_km)

    answer: Any = ask(
        agent,
        "\n".join(
            [
                f"Profile axis: {axis}",
                f"Options: {', '.join(options)}",
                f"At: {at_km:.1f} km into the run",
                f"What differs: {detail or 'not stated'}",
            ]
        ),
        budget=budget,
    )
    if answer is None:
        return template(axis, options, at_km=at_km)
    return str(answer.text).strip()


__all__ = ["Question", "template", "write"]
