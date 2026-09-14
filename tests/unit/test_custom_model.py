"""The six scope 7.1 parameters, and whether they reach the router.

Most of this file exists for one assertion. An optimiser handed a parameter that does not
reach the router will assign it a value and report an improvement, and nothing downstream
can tell the difference - so `test_every_parameter_reaches_the_router` reads the emitted
body rather than trusting the model to be wired.

The other load-bearing test is the one that reads a deployment file: the hard excludes live
in the server profile and this module deliberately does not repeat them, which is only safe
for as long as they are actually there.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from longrun.core.models.geometry import LatLon
from longrun.core.models.profile import PreferenceEntry
from longrun.core.preferences.store import load_defaults
from longrun.core.routing.custom_model import (
    AVOID_HIGH_STRESS,
    MAX_MULTIPLIER,
    MIN_MULTIPLIER,
    NEUTRAL,
    PARAMETER_NAMES,
    CustomModelParams,
    describe,
    to_custom_model,
)
from longrun.core.routing.graphhopper import route_body

CONFIGS = Path(__file__).resolve().parents[2] / "deploy" / "graphhopper"

#: Ferry Building to the de Young - ADR 0001's own measured pair.
_ENDS = [LatLon(lat=37.7955, lon=-122.3937), LatLon(lat=37.7715, lon=-122.4686)]


def _conditions(model: dict) -> str:  # type: ignore[type-arg]
    return " ".join(term.get("if", "") for term in model.get("priority", []))


# --- the assertion this file exists for --------------------------------------


@pytest.mark.parametrize("name", PARAMETER_NAMES)
def test_every_parameter_reaches_the_router(name: str) -> None:
    """A knob attached to nothing is worse than a missing knob: a fitter will move it and
    report an improvement, and the route will not change."""
    params = CustomModelParams(**{name: 0.3})

    model = to_custom_model(params)
    body = route_body(_ENDS, custom_model=model)

    assert body["custom_model"] == model, "the model did not survive route_body"
    assert "0.3" in str(model), f"{name} was set to 0.3 and no term carries it"


def test_a_neutral_vector_emits_no_model_at_all() -> None:
    """Not `{"priority": []}`. `CachedRouter` keys on the request body, so a neutral plan
    must produce byte-identical bytes to a plan from before this module existed - which is
    what lets four goldens keep replaying cassettes recorded without one."""
    assert to_custom_model(NEUTRAL) == {}
    assert "custom_model" not in route_body(_ENDS, custom_model=to_custom_model(NEUTRAL))


def test_the_default_that_ships_is_neutral() -> None:
    """ADR 0021, and risk R1 is the measurement: M0 found `avoid` within 0.8% of `neutral`
    on four route pairs, so a non-neutral default would be an unmeasured assumption."""
    assert NEUTRAL.is_neutral
    assert to_custom_model() == {}
    assert not AVOID_HIGH_STRESS.is_neutral


# --- what is deliberately absent ---------------------------------------------


def test_the_hard_excludes_are_not_repeated_in_the_query_model() -> None:
    """A query-time model is merged with the profile's, so repeating them would change the
    request body - and therefore every cache key - for no change in where the route goes."""
    model = to_custom_model(AVOID_HIGH_STRESS)

    assert "MOTORWAY" not in _conditions(model)
    assert "PRIVATE" not in _conditions(model)


@pytest.mark.parametrize("config", sorted(CONFIGS.glob("config-*.yml")))
def test_the_server_profile_still_carries_the_hard_excludes(config: Path) -> None:
    """The other half of the decision above, and the reason it is safe. Scope 7.1 requires
    these four; this module declines to emit them because the deployment already does. If
    that ever stops being true, the excludes vanish silently - so it is asserted here
    against the file rather than remembered."""
    text = config.read_text(encoding="utf-8")

    assert "!foot_access" in text, "foot=no is what !foot_access blocks"
    assert "road_access == PRIVATE" in text
    assert "road_class == MOTORWAY" in text


@pytest.mark.parametrize("config", sorted(CONFIGS.glob("config-*.yml")))
def test_no_slope_term_because_no_deployed_graph_has_elevation(config: Path) -> None:
    """`graph.elevation.provider` is commented out everywhere, so `average_slope` is not an
    encoded value and a model naming it is rejected - which surfaces as "no route exists",
    the wrong answer to "wrong graph". Hills are scored from the DEM, never routed."""
    text = config.read_text(encoding="utf-8")
    active = [line for line in text.splitlines() if not line.strip().startswith("#")]

    assert not any("graph.elevation.provider" in line for line in active)

    profile = load_defaults().model_copy(
        update={"grade": PreferenceEntry(value=load_defaults().grade.value, weight=1.0)}
    )
    assert "slope" not in _conditions(to_custom_model(NEUTRAL, profile))


def test_a_graph_without_lts_drops_the_lts_terms_rather_than_failing() -> None:
    """A stock graph has no `lts` encoded value (ADR 0001) and the server rejects a model
    that names one."""
    model = to_custom_model(AVOID_HIGH_STRESS, has_lts=False)

    assert "lts" not in _conditions(model)
    assert model == {}


# --- the profile terms -------------------------------------------------------


def test_a_runner_who_says_dirt_gets_unpaved_and_paths_rewarded() -> None:
    """Scope 7.1: "surface ... terms are set from the preference profile at query time".
    Multiplied onto the fitted value rather than replacing it - a stated preference does
    not overrule a fitted one, and a fitted one does not overrule a person."""
    profile = load_defaults().model_copy(
        update={"surface": PreferenceEntry(value="dirt", weight=0.5)}
    )

    model = to_custom_model(NEUTRAL, profile)
    terms = {term["if"]: float(term["multiply_by"]) for term in model["priority"]}

    unpaved = next(value for condition, value in terms.items() if "DIRT" in condition)
    paths = next(value for condition, value in terms.items() if "FOOTWAY" in condition)
    assert unpaved > 1.0 and paths > 1.0


def test_a_runner_who_says_paved_gets_unpaved_penalised_and_no_path_bonus() -> None:
    profile = load_defaults().model_copy(
        update={"surface": PreferenceEntry(value="paved", weight=0.5)}
    )

    terms = {
        term["if"]: float(term["multiply_by"])
        for term in to_custom_model(NEUTRAL, profile)["priority"]
    }

    assert next(value for condition, value in terms.items() if "DIRT" in condition) < 1.0
    assert not any("FOOTWAY" in condition for condition in terms)


def test_the_default_profile_says_nothing_and_so_emits_nothing() -> None:
    """`surface` defaults to "mixed" at weight 0.1. A profile nobody has touched must not
    quietly start steering routes, or every plan this project has produced changes."""
    assert to_custom_model(NEUTRAL, load_defaults()) == {}


# --- the vector round trip ---------------------------------------------------


def test_a_vector_survives_the_round_trip_in_a_fixed_order() -> None:
    """`tuning.py` searches over tuples and never sees a field name, so the order has to be
    the same going in and coming out or the fitted vector is silently permuted."""
    params = CustomModelParams(lts2=0.9, lts3=0.5, lts4=0.2, unpaved=1.4, path_bonus=2.0)

    assert CustomModelParams.from_vector(params.as_vector()) == params
    assert params.as_vector()[PARAMETER_NAMES.index("lts4")] == 0.2


def test_a_vector_of_the_wrong_length_is_refused() -> None:
    with pytest.raises(ValueError, match="expected 6"):
        CustomModelParams.from_vector([1.0, 1.0, 1.0])


@pytest.mark.parametrize("value", [0.0, MIN_MULTIPLIER / 2, MAX_MULTIPLIER + 1])
def test_a_multiplier_outside_the_bounds_is_refused(value: float) -> None:
    """The bottom bound is the interesting one: 0.001 is an exclusion wearing a
    preference's clothes, and a fitter would reach for it to win a pair."""
    with pytest.raises(ValueError):
        CustomModelParams(lts4=value)


# --- what the sheet says -----------------------------------------------------


def test_a_neutral_model_says_so_in_words() -> None:
    """Risk R1 is open, so a plan has to say which parameters drew its route."""
    assert "unfitted" in describe(NEUTRAL)
    assert "lts3=0.2" in describe(AVOID_HIGH_STRESS)
    assert "lts2" not in describe(AVOID_HIGH_STRESS)


def test_a_multiplier_never_reaches_the_router_in_exponent_form() -> None:
    """`str(1e-05)` is "1e-05", which GraphHopper's expression parser rejects."""
    model = to_custom_model(CustomModelParams(lts4=MIN_MULTIPLIER))

    assert "e-" not in str(model)
