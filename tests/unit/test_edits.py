"""Editing a plan that already exists (scope 7.8, 10.3).

Scope 10.3's direct manipulation is five gestures - lock, unlock, choose an alternative,
add a via point, draw an avoid polygon - and every one of them is a write to a stored plan
rather than a new one. These tests are the CLI-shaped half of that: `ui/README.md` claims
"what is missing is the endpoints and the map interactions, not the capability", and this
file is where that claim is either true or gets fixed.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from longrun.core.models.geometry import LatLon
from longrun.core.models.request import LockedRange, PlanRequest
from longrun.core.plan.scratchpad import Scratchpad


def _request() -> PlanRequest:
    return PlanRequest(
        date=date(2026, 3, 15),
        start=LatLon(lat=37.77, lon=-122.42),
        end=LatLon(lat=37.79, lon=-122.40),
    )


def _pad() -> Scratchpad:
    return Scratchpad(plan_id="p", request=_request())


# --- M11.1: a lock says who wrote it -----------------------------------------


def test_a_lock_a_runner_set_and_one_the_loop_set_are_different_locks() -> None:
    """The discriminator "unlock what I locked" needs, and did not have.

    Both used to be a `LockedRange` with prose in `reason`, so the only way to tell them
    apart was to parse `"round 3: rerouted"` - a string the sheet is free to rephrase.
    """
    pad = _pad()
    pad.lock(0.0, 100.0, reason="I always run this bit")
    pad.lock(200.0, 300.0, reason="round 1: rerouted", source="loop")

    assert [lock.source for lock in pad.locked] == ["user", "loop"]


def test_a_lock_that_came_in_on_the_request_is_the_runners() -> None:
    """`PlanRequest.locked` is the request, so nobody but the runner put it there."""
    request = _request().model_copy(update={"locked": [LockedRange(start_m=0.0, end_m=50.0)]})
    assert request.locked[0].source == "user"


def test_a_lock_source_outside_the_two_that_exist_is_refused() -> None:
    """A third author would silently widen what `unlock(source=...)` leaves behind."""
    with pytest.raises(ValidationError):
        LockedRange(start_m=0.0, end_m=10.0, source="api")  # type: ignore[arg-type]
