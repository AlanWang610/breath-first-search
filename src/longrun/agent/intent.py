"""Scope 8.1 step 1: a sentence becomes a `PlanRequest`.

The one call site with no deterministic fallback, and ADR 0015 says so plainly: a
structured request works without a model, and a sentence does not. That is degrading to
*less*, honestly, rather than to something invented.

Place names are left as names. This call site does not know where anything is, and a model
guessing coordinates would produce a plan for somewhere nobody asked about - `geocode`
resolves them afterwards, and can say when it could not.
"""

from __future__ import annotations

from datetime import date as date_type
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from longrun.agent import prompts
from longrun.agent.model import ask, build_agent

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import Budget
    from longrun.core.models.request import PlanRequest


class Intent(BaseModel):
    """What a request said, before anything is resolved or defaulted.

    Every field is optional, and that is the point: an unstated constraint must arrive as
    `None` and not as a plausible default, because a default invented here becomes a
    constraint the runner never asked for and will not see.
    """

    start_name: str | None = Field(None, description="Where the run starts, as written.")
    end_name: str | None = Field(None, description="Where it ends, as written.")
    via_names: list[str] = Field(default_factory=list)
    loop: bool = Field(False, description="True when it returns to the start.")
    date: date_type | None = None
    start_time: str | None = Field(None, description="HH:MM, only if the request said one.")
    target_km: float | None = None
    avoid: list[str] = Field(default_factory=list, description="Named things to keep off.")
    notes: str | None = Field(None, description="Anything said that no field above holds.")


def parse(text: str, *, today: date_type, budget: Budget | None = None) -> Intent | None:
    """Read a request, or return `None` when there is no model to read it with."""
    from longrun.agent.model import ModelUnavailable

    try:
        agent = build_agent(Intent, prompts.INTENT)
    except ModelUnavailable:
        return None
    return ask(agent, f"Today is {today.isoformat()}.\n\nRequest: {text}", budget=budget)


def to_request(intent: Intent, resolve: Any, *, today: date_type) -> PlanRequest:
    """Turn an intent into a request, resolving names through `resolve`.

    `resolve` is a callable taking a name and returning a `LatLon` or `None` - the
    geocoder, injected, so this module stays testable without one and without a network.
    """
    from longrun.core.models.request import PlanRequest

    start = resolve(intent.start_name) if intent.start_name else None
    end = resolve(intent.end_name) if intent.end_name else None
    via = [point for name in intent.via_names if (point := resolve(name)) is not None]

    if intent.loop and start is not None and end is None:
        end = start

    return PlanRequest(
        # Generate mode needs both endpoints and `PlanRequest` enforces it, so a request
        # whose names did not resolve stays in repair mode rather than failing validation
        # with a message about a field the runner never mentioned.
        mode="generate" if start is not None and end is not None else "repair",
        date=intent.date or today,
        start=start,
        end=end,
        via=via,
        loop=intent.loop,
        start_time=_time(intent.start_time),
        target_distance_km=intent.target_km,
        avoid_names=list(intent.avoid),
    )


def _time(value: str | None) -> Any:
    from datetime import time as time_type

    if not value:
        return None
    try:
        hour, _, minute = value.partition(":")
        return time_type(int(hour), int(minute or 0))
    except ValueError:
        # A time that cannot be read is no time at all. Better a default start the sheet
        # states than an hour invented from a typo.
        return None


__all__ = ["Intent", "parse", "to_request"]
