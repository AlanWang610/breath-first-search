"""The five LLM call sites (scope 4.1; ADR 0015, 0019).

No real model, except in the one `network`-marked test at the bottom. `TestModel` and
`FunctionModel` stand in everywhere else, and `conftest._block_model` makes a real call a
loud `RuntimeError` rather than a slow timeout.

What is worth testing here is not what a model says - that is M6's evaluation work - but
what happens when it says nothing, says something wrong, or is not there at all. Every one
of those has to leave a plan standing.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

import pytest

pytest.importorskip("pydantic_ai", reason="the `agent` extra is not installed")

from pydantic_ai import models  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402
from pydantic_ai.models.test import TestModel  # noqa: E402

from longrun.agent import intent, profile_updates, questions  # noqa: E402
from longrun.agent.model import (  # noqa: E402
    KEY_ENV_VAR,
    ModelSettings,
    ModelUnavailable,
    ask,
    build_agent,
    settings_from_env,
)
from longrun.core.models.context import Budget  # noqa: E402
from longrun.core.preferences.store import load_defaults  # noqa: E402

# --- no model at all ---------------------------------------------------------


def test_with_no_key_every_call_site_degrades_rather_than_raises() -> None:
    """ADR 0015: an absent model costs a plan its enrichment, never its plan."""
    assert not settings_from_env({}).available
    assert intent.parse("run me 20k", today=date(2026, 3, 15)) is None
    assert profile_updates.propose("I don't mind sun", load_defaults()) is None

    from longrun.agent.tradeoffs import describer

    assert describer() is None


def test_the_question_writer_falls_back_to_a_template() -> None:
    """The one call site whose deterministic path is prose rather than nothing, and it
    still names the three things scope 6.3 asks for: the option, what differs, the mile."""
    written = questions.write("surface", ["A", "B"], at_km=4.2)

    assert "surface" in written
    assert "4.2 km" in written
    assert "A or B" in written


def test_asking_for_a_model_that_is_not_configured_says_which_variable() -> None:
    with pytest.raises(ModelUnavailable, match=KEY_ENV_VAR):
        build_agent(intent.Intent, "x", ModelSettings(key=None))


# --- a model that misbehaves -------------------------------------------------


def _agent(fn: Any, output_type: Any) -> Any:
    from pydantic_ai import Agent

    return Agent(FunctionModel(fn), output_type=output_type, instructions="x")


def test_a_model_that_raises_costs_the_call_and_not_the_plan() -> None:
    """The budget is charged *before* the call: a call that was made and then failed still
    cost what it cost, and a counter that records only successes understates exactly the
    runs a reader would want to look at."""

    def explode(messages: Any, info: AgentInfo) -> Any:
        raise RuntimeError("the provider fell over")

    budget = Budget()
    with models.override_allow_model_requests(True):
        answer = ask(_agent(explode, intent.Intent), "anything", budget=budget)

    assert answer is None
    assert budget.model_calls_used == 1


def test_a_model_call_past_the_budget_is_simply_not_made() -> None:
    budget = Budget(model_calls_max=0)
    with models.override_allow_model_requests(True):
        from pydantic_ai import Agent

        answer = ask(Agent(TestModel(), output_type=intent.Intent), "x", budget=budget)

    assert answer is None
    assert budget.model_calls_used == 0


def test_a_proposal_about_a_safety_floor_is_refused() -> None:
    """Scope 6.3 puts floors outside the profile, and `floors.py` refuses to lower them.

    A model proposing one is proposing something the store is built to reject, so this
    drops it here rather than writing something three layers down will refuse - and
    `SETTABLE` is the list, which is why it has no heat threshold in it.
    """
    assert "wbgt_hard_c" not in profile_updates.SETTABLE
    assert "traffic_tolerance" in profile_updates.SETTABLE


def test_an_unreadable_start_time_becomes_no_start_time() -> None:
    """Better a default start the sheet states than an hour invented from a typo."""
    parsed = intent.Intent(start_name="a", end_name="b", start_time="half seven")
    request = intent.to_request(parsed, _resolver(), today=date(2026, 3, 15))

    assert request.start_time is None


def _resolver() -> Any:
    from longrun.core.models.geometry import LatLon

    return lambda name: LatLon(lat=37.77, lon=-122.41) if name else None


def test_a_name_that_does_not_resolve_leaves_the_request_in_repair_mode() -> None:
    """`PlanRequest` requires both endpoints in generate mode, so a request whose names did
    not resolve must not be built as one - it would fail validation with a message about a
    field the runner never mentioned."""
    parsed = intent.Intent(start_name="somewhere unfindable", end_name="also unfindable")
    request = intent.to_request(parsed, lambda name: None, today=date(2026, 3, 15))

    assert request.mode == "repair"
    assert request.start is None


def test_a_loop_request_ends_where_it_started() -> None:
    parsed = intent.Intent(start_name="home", loop=True, target_km=20.0)
    request = intent.to_request(parsed, _resolver(), today=date(2026, 3, 15))

    assert request.loop is True
    assert request.start == request.end


# --- the live wire -----------------------------------------------------------


@pytest.mark.network
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="no ANTHROPIC_API_KEY")
def test_a_real_model_reads_a_real_request() -> None:
    """One live call, run once, because a path that is written and never executed is the
    M0 mistake - "gates green" meaning "green on my laptop"."""
    parsed = intent.parse(
        "I want to run about 25 km from the Ferry Building to Ocean Beach on 15 March 2026, "
        "starting at 7am, avoiding the Great Highway.",
        today=date(2026, 3, 1),
    )

    assert parsed is not None
    assert parsed.target_km == pytest.approx(25.0, abs=2.0)
    assert parsed.start_name and "ferry" in parsed.start_name.lower()
    assert parsed.date == date(2026, 3, 15)
