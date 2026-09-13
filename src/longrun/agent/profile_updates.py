"""Scope 6.3: a conversational statement becomes a *proposed* profile entry.

Proposed, never applied. Scope 6.3 requires the runner to confirm before anything is
saved, and `store.merge` is the only write path that honours provenance - so this module
returns a proposal and writes nothing at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from longrun.agent import prompts
from longrun.agent.model import ask, build_agent

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import Budget
    from longrun.core.models.profile import PreferenceProfile

#: The axes a statement may touch. Safety floors are absent on purpose: scope 6.3 puts
#: them outside the profile and `floors.py` refuses to lower them, so a model proposing
#: one would be proposing something the store is built to reject.
SETTABLE = (
    "sun",
    "water_gap_max_min",
    "toilet_gap_max_min",
    "carry_capacity_ml",
    "traffic_tolerance",
    "surface",
    "grade",
    "detour_tolerance_pct",
    "stops_tolerance",
    "darkness_tolerance",
    "scenery_vs_directness",
)


class ProposedUpdate(BaseModel):
    """One change, with the words that prompted it."""

    axis: str = Field(description="The profile key this is about.")
    value: Any = Field(description="The value to set, in the shape that key takes.")
    because: str = Field(description="The part of what they said that this came from.")


def propose(
    statement: str, profile: PreferenceProfile, *, budget: Budget | None = None
) -> ProposedUpdate | None:
    """Read a statement, or return `None` - which is also what "nothing to change" is."""
    from longrun.agent.model import ModelUnavailable

    try:
        agent = build_agent(ProposedUpdate, prompts.PROFILE_UPDATE)
    except ModelUnavailable:
        return None

    current = ", ".join(f"{axis}={getattr(profile, axis).value!r}" for axis in SETTABLE)
    answer = ask(
        agent,
        f"Settable axes: {', '.join(SETTABLE)}.\nCurrent: {current}\n\nThey said: {statement}",
        budget=budget,
    )
    if answer is None or answer.axis not in SETTABLE:
        # An axis outside the list is a proposal about something that is not a preference -
        # a safety floor, usually - and the honest answer is no proposal rather than a
        # write the store would refuse three layers down.
        return None
    return answer


def apply(
    profile: PreferenceProfile, update: ProposedUpdate, *, on: Any
) -> tuple[PreferenceProfile, list[str]]:
    """Write a **confirmed** proposal through `merge`, as `stated`.

    Through `merge` rather than `apply_overrides`: `merge` is the only path that checks
    `supersedes` and reports what it refused, and an override's bare form deliberately
    preserves the existing provenance - which would silently file a runner's own words as
    a default.
    """
    from longrun.core.models.profile import PreferenceEntry, Provenance
    from longrun.core.preferences.store import merge

    entry = PreferenceEntry(value=update.value, provenance=Provenance.STATED, updated=on)
    incoming = profile.model_copy(update={update.axis: entry})
    return merge(profile, incoming)


__all__ = ["SETTABLE", "ProposedUpdate", "apply", "propose"]
