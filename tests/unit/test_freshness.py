"""The freshness classification, and the `prior` table a partial pass depends on.

A refresh re-runs the time-dependent scorers and carries the rest. Both halves of that are
claims about scorers that nothing in the code enforces, so they are enforced here: a scorer
nobody classified fails CI rather than being carried by default, and a scorer listed as
time-independent has to actually ignore the clock.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from longrun.core.scorers.freshness import TIME_DEPENDENT, TIME_INDEPENDENT, rescore_set
from longrun.core.scorers.registry import (
    NOT_YET_IMPLEMENTED,
    PRIOR_DEPENDENCIES,
    SCORERS,
    closure,
    load_scorer,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "longrun"


def test_every_scorer_is_classified_as_time_dependent_or_not() -> None:
    """The test the classification exists for.

    An unclassified scorer is one a refresh carries by default, which means reporting last
    month's answer as this week's. Being in TIME_INDEPENDENT is still a decision - just a
    recorded one.
    """
    unclassified = sorted(set(SCORERS) - TIME_DEPENDENT - TIME_INDEPENDENT)
    assert not unclassified, (
        f"these scorers have no freshness decision, so a refresh would carry them without "
        f"anyone having chosen that: {unclassified}. Put each in TIME_DEPENDENT or in "
        f"TIME_INDEPENDENT."
    )


def test_the_freshness_collections_do_not_overlap() -> None:
    """A name in both is a contradiction resolved silently by whichever is consulted first."""
    assert not (TIME_DEPENDENT & TIME_INDEPENDENT)


def test_no_classification_names_a_scorer_that_does_not_exist() -> None:
    """Without this, a rename leaves a ghost in the table and the renamed scorer falls
    through unclassified - while the exhaustiveness test above still passes."""
    known = set(SCORERS) | set(NOT_YET_IMPLEMENTED)
    assert not sorted((TIME_DEPENDENT | TIME_INDEPENDENT) - known)


def _scorer_source(name: str) -> tuple[Path, ast.Module]:
    module_path = SCORERS[name]
    path = SRC.parent / (module_path.replace(".", "/") + ".py")
    return path, ast.parse(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", sorted(TIME_INDEPENDENT))
def test_a_time_independent_scorer_really_ignores_the_clock_and_the_etas(name: str) -> None:
    """The half of the classification that decides what a refresh never runs again.

    Asserted rather than trusted, because the failure is silent: a scorer that grows a
    seasonal rule and stays on this list is one a refresh stops re-running, and nothing else
    in the suite would notice. `etas` may appear only as the parameter it is handed.
    """
    path, tree = _scorer_source(name)
    # `etas` as a parameter is an `ast.arg`, never an `ast.Name` load - so walking the whole
    # module for a load is exactly the question, and needs no handle on the function. These
    # modules define `score` as an alias (`score = surface_profile`) anyway.
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "etas" and isinstance(node.ctx, ast.Load):
            pytest.fail(
                f"{path.name} reads `etas`, so its answer can change with the hour, but it "
                f"is listed TIME_INDEPENDENT and a refresh would never re-run it."
            )
        if isinstance(node, ast.Attribute) and node.attr == "clock":
            pytest.fail(f"{path.name} reaches for a clock while listed TIME_INDEPENDENT.")

    # It is handed an ETA vector like every scorer; the classification is that it ignores it.
    assert "etas" in inspect.signature(load_scorer(SCORERS[name])).parameters


def test_the_refresh_set_is_closed_over_prior_dependencies() -> None:
    """Today TIME_DEPENDENT is already closed. This reports the day it stops being, at CI
    time, rather than as a `StalePrior` in a user's terminal."""
    assert rescore_set() == TIME_DEPENDENT


def test_every_scorer_that_takes_a_prior_declares_what_it_reads() -> None:
    """A new scorer that grows `prior=` and forgets the table gets a silently stale prior on
    every refresh. `_call` finds the parameter the same way."""
    undeclared = []
    for name, module_path in SCORERS.items():
        func = load_scorer(module_path)
        if func is None:
            continue
        if "prior" in inspect.signature(func).parameters and name not in PRIOR_DEPENDENCIES:
            undeclared.append(name)
    assert not undeclared, (
        f"these scorers read earlier results and do not say which: {undeclared}. Add them to "
        f"PRIOR_DEPENDENCIES, or a partial re-score will feed them a stale prior."
    )


def test_no_prior_dependency_points_forward_in_the_registry_order() -> None:
    """`SCORERS` is an ordered dict and its order is the dependency order - which the
    registry states in prose and nothing checked. A forward edge yields an empty `prior`
    lookup today, silently."""
    position = {name: index for index, name in enumerate(SCORERS)}
    for name, dependencies in PRIOR_DEPENDENCIES.items():
        assert name in position, f"{name} is not a scorer"
        for dependency in dependencies:
            assert dependency in position, f"{name} declares an unknown prior {dependency}"
            assert position[dependency] < position[name], (
                f"{name} reads {dependency}, which runs after it - so `prior` never holds it"
            )


def test_closure_pulls_in_what_a_scorer_reads() -> None:
    assert closure({"heat_stress"}) == frozenset({"sun_exposure", "heat_stress"})
    assert closure({"resupply_schedule"}) == frozenset(
        {"resupply_schedule", "heat_stress", "sun_exposure"}
    )
    assert closure({"legality"}) == frozenset({"legality"})
