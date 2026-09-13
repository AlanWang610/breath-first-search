"""Fitting the six priority parameters (scope 7.1).

Two tests carry this file. The first generates preference pairs *from* a known parameter
vector and asserts the fitter reproduces its choices, including on pairs it never saw - the
M1/M2/M5 discipline of hand-built inputs whose answer is known by construction, applied to
an optimiser. The second plants the bug scope 7.1 names in as many words: *"a detour-ratio
regularizer so the optimizer can't solve the problem by routing through every park"*.

**Recovery is asserted as behaviour, never as parameter identity**, and that is a finding
rather than a convenience. Avoiding LTS 3 and seeking footways are the same hypothesis on
evidence where every route is one or the other, so several vectors explain the same
preferences exactly. Asking the optimiser for the original six numbers back is asking it to
solve an unidentifiable problem; asking it to make the same choices is not. Two bugs in the
fitter surfaced from that one mistake - see the tie-scoring and tie-break tests below.

Everything else is about refusing to answer. `fit` is mostly a rule about not emitting six
numbers, because both preference sources scope 7.1 names are empty on a fresh install and a
vector fitted to nothing is an assumption wearing a measurement's clothes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from longrun.core.models.profile import PreferenceEntry
from longrun.core.preferences.store import load_defaults
from longrun.core.routing.custom_model import NEUTRAL, CustomModelParams
from longrun.core.routing.tuning import (
    MIN_PAIRS,
    PreferencePair,
    RouteSummary,
    WayClass,
    agreement_of,
    cost,
    detour_penalty,
    fit,
    priority_of,
)

QUIET = WayClass(lts=1)
BUSY = WayClass(lts=3)
NASTY = WayClass(lts=4)
PATH = WayClass(lts=1, is_path=True)
DIRT = WayClass(lts=1, unpaved=True)

#: A coarse grid, so a test that fits is milliseconds rather than seconds. The shape of the
#: answer is what is under test, not the resolution of the search.
GRID = (0.25, 1.0, 4.0)


def _route(name: str, *ways: tuple[WayClass, float]) -> RouteSummary:
    return RouteSummary.from_ways(name, ways)


# --- the arithmetic, whose sign is easy to get backwards ---------------------


def test_seeking_a_class_makes_a_route_using_it_cheaper() -> None:
    """GraphHopper divides by priority, so above 1.0 is *more* attractive. Getting this
    backwards would fit every parameter to exactly the wrong sign and nothing downstream
    would notice."""
    on_paths = _route("paths", (PATH, 1000.0))

    seeking = CustomModelParams(path_bonus=4.0)
    avoiding = CustomModelParams(path_bonus=0.25)

    assert cost(on_paths, seeking) < cost(on_paths, NEUTRAL) < cost(on_paths, avoiding)


def test_a_neutral_vector_costs_every_route_its_own_length() -> None:
    """The property that makes the cost comparable to a detour ratio at all."""
    route = _route("mixed", (QUIET, 500.0), (BUSY, 300.0), (PATH, 200.0))

    assert cost(route, NEUTRAL) == pytest.approx(route.length_m)


def test_multipliers_compound_the_way_the_routers_rules_do() -> None:
    """GraphHopper's `priority` rules multiply: an unpaved path takes both terms."""
    params = CustomModelParams(unpaved=2.0, path_bonus=3.0)
    unpaved_path = WayClass(lts=1, unpaved=True, is_path=True)

    assert priority_of(unpaved_path.key(), params) == pytest.approx(6.0)


def test_an_unknown_lts_takes_no_lts_multiplier() -> None:
    """Scope 12: a way with no usable tags is unknown, not level 1. Charging it the LTS 2
    multiplier would fit the parameters against missing data."""
    assert priority_of(WayClass(lts=None).key(), CustomModelParams(lts2=0.25)) == 1.0


# --- recovering a vector that is known by construction -----------------------


def _pairs_from(
    truth: CustomModelParams, count: int = 24, *, seed: int = 0
) -> list[PreferencePair]:
    """Pairs a runner holding exactly `truth` would have produced.

    The route shapes vary across the set rather than repeating two fixed ones, so the
    evidence pins down more than one explanation. With only "all busy" against "all path"
    on offer, avoiding busy roads and seeking paths are the *same hypothesis* and no
    optimiser can be asked to tell them apart - which is what the first version of this
    test asked for, and why it failed.
    """
    pairs = []
    for index in range(count):
        step = (index + seed) % 6
        a = _route(
            "a",
            (BUSY, 400.0 + 80.0 * step),
            (QUIET, 300.0),
            (PATH, 100.0 * step),
        )
        b = _route(
            "b",
            (QUIET, 500.0),
            (PATH, 300.0 + 60.0 * step),
            (DIRT, 50.0 * step),
        )
        preferred = "a" if cost(a, truth) < cost(b, truth) else "b"
        pairs.append(PreferencePair(a=a, b=b, preferred=preferred, source="edit"))
    return pairs


def test_it_reproduces_the_choices_its_evidence_came_from() -> None:
    """The test that makes the fitter believable.

    Recovery is asserted as *behaviour*, not as parameter identity. Several vectors can
    explain the same preferences exactly - avoiding LTS 3 and seeking paths are
    indistinguishable on evidence where every route is one or the other - so demanding the
    original six numbers back would be demanding the optimiser solve an unidentifiable
    problem. What can honestly be required is that the fitted vector makes the same choices.
    """
    truth = CustomModelParams(lts3=0.25, path_bonus=4.0)

    result = fit(_pairs_from(truth), grid=GRID, min_pairs=4)

    assert result.agreement == 1.0


def test_the_fitted_vector_generalises_to_pairs_it_never_saw() -> None:
    """The stronger half, and the one a fit against real edits would live or die by: a
    vector that memorised the training set would agree on it and fail here."""
    truth = CustomModelParams(lts3=0.25, path_bonus=4.0)
    fitted = fit(_pairs_from(truth, count=24), grid=GRID, min_pairs=4).params

    held_out = _pairs_from(truth, count=12, seed=3)

    assert agreement_of(held_out, fitted) == 1.0


def test_evidence_with_no_signal_fits_to_neutral_rather_than_a_grid_corner() -> None:
    """Half the pairs preferring each shape says nothing, and an optimiser that answered
    anyway would be reporting noise.

    This is the test that caught the tie-scoring bug: a tie used to count as a
    disagreement, so neutral scored 0.0 here and the search preferred any vector with an
    opinion. A tie scores half now, and indifference is worth exactly what it is.
    """
    a = _route("a", (BUSY, 1000.0))
    b = _route("b", (QUIET, 1000.0))
    pairs = [
        PreferencePair(a=a, b=b, preferred="a" if index % 2 else "b", source="edit")
        for index in range(20)
    ]

    result = fit(pairs, grid=GRID, min_pairs=4)

    assert result.params.is_neutral
    assert any("neutral" in caveat for caveat in result.caveats)


def test_the_same_evidence_gives_the_same_vector_every_time() -> None:
    """Deterministic by construction - a fixed grid order and a tie-break toward neutral.
    A tuning result that moved between runs would be unreviewable."""
    pairs = _pairs_from(CustomModelParams(lts4=0.25))

    first = fit(pairs, grid=GRID, min_pairs=4)
    second = fit(pairs, grid=GRID, min_pairs=4)

    assert first.params == second.params


def test_the_tie_break_treats_avoiding_and_seeking_as_equal_claims() -> None:
    """The grid is symmetric in log space and the tie-break used not to be: |0.25 - 1| is
    0.75 against |4 - 1| = 3, so "avoid four times" read as a smaller claim than "seek four
    times" and the fit explained everything by avoidance. Found by a test that asked for
    a seeking parameter and was handed an avoiding one that fitted just as well."""
    avoid = CustomModelParams(lts3=0.25)
    seek = CustomModelParams(path_bonus=4.0)

    quiet_path = _route("q", (PATH, 1000.0))
    busy = _route("b", (BUSY, 1000.0))

    # The two vectors are mirror images: each makes the same route cheaper by the same
    # factor, so neither is the smaller claim and the search may not prefer one on shape.
    assert cost(busy, avoid) / cost(quiet_path, avoid) == pytest.approx(
        cost(busy, seek) / cost(quiet_path, seek)
    )


# --- the regulariser, which is the half scope 7.1 spells out -----------------


def _park_pairs(count: int = 20) -> list[PreferencePair]:
    """Pairs an optimiser can win every one of, by routing through the park.

    The park route is 1.8x the direct one. A vector that makes paths free agrees with all
    twenty, and produces a runner a 80% detour they never asked for.
    """
    direct = _route("direct", (BUSY, 1000.0))
    park = _route("park", (PATH, 1800.0))
    return [PreferencePair(a=direct, b=park, preferred="b", source="edit") for _ in range(count)]


def _picks_the_park(params: CustomModelParams) -> bool:
    """Whether this vector would send the runner the long way round.

    Asserted on rather than on `path_bonus`, because seeking paths and avoiding busy roads
    are the same hypothesis on this evidence and the search may express it either way. What
    scope 7.1 cares about is not which parameter moved - it is that the runner gets an 80%
    detour, and that is a question about the route the vector picks.
    """
    direct, park = _route("direct", (BUSY, 1000.0)), _route("park", (PATH, 1800.0))
    return cost(park, params) < cost(direct, params)


def test_without_the_regulariser_the_optimiser_routes_through_every_park() -> None:
    """The control. Turn the penalty off and the failure scope 7.1 names appears, which is
    what makes the test below an experiment rather than an assertion."""
    result = fit(_park_pairs(), grid=GRID, min_pairs=4, penalty_weight=0.0)

    assert result.agreement == 1.0
    assert _picks_the_park(result.params)


def test_the_detour_regulariser_refuses_that_trade() -> None:
    """Scope 7.1's own words. The vector can still buy every pair; it may not buy them at
    80% extra distance, which is eight times the profile's stated tolerance."""
    result = fit(_park_pairs(), grid=GRID, min_pairs=4)

    assert not _picks_the_park(result.params)
    assert result.agreement < 1.0


def test_a_detour_inside_the_runners_tolerance_is_not_penalised() -> None:
    """The regulariser must not refuse every detour - only the ones beyond what the runner
    said they would accept. `detour_tolerance_pct` is 10% by default."""
    direct = _route("direct", (BUSY, 1000.0))
    slight = _route("slight", (PATH, 1050.0))
    pairs = [PreferencePair(a=direct, b=slight, preferred="b", source="edit") for _ in range(20)]

    result = fit(pairs, grid=GRID, min_pairs=4)

    assert cost(slight, result.params) < cost(direct, result.params)
    assert result.penalty == 0.0


def test_the_tolerance_comes_from_the_profile_and_not_from_a_constant() -> None:
    """`detour_tolerance_pct` has been on the profile since M1 with no reader. This is it."""
    generous = load_defaults().model_copy(
        update={"detour_tolerance_pct": PreferenceEntry(value=100.0)}
    )
    params = CustomModelParams(path_bonus=4.0)

    strict = detour_penalty(_park_pairs(), params, tolerance_pct=10.0)
    loose = detour_penalty(
        _park_pairs(), params, tolerance_pct=float(generous.detour_tolerance_pct.value)
    )

    assert strict > loose == 0.0


# --- refusing to answer ------------------------------------------------------


def test_too_few_real_pairs_returns_neutral_and_says_how_many() -> None:
    """The milestone's central honesty rule. Six numbers fitted to three pairs would be
    noise that presents as a measurement."""
    result = fit(_pairs_from(CustomModelParams(lts3=0.25), count=3), grid=GRID)

    assert result.params is NEUTRAL
    assert result.grid_size == 0
    assert any(f"fewer than the {MIN_PAIRS}" in caveat for caveat in result.caveats)


def test_synthetic_pairs_do_not_count_toward_the_minimum() -> None:
    """A pair this project generated to test its own optimiser is not evidence about any
    runner, and a fit that quietly rested on those would be circular."""
    pairs = [
        PreferencePair(a=_route("a", (BUSY, 1000.0)), b=_route("b", (QUIET, 900.0)), preferred="b")
        for _ in range(50)
    ]

    result = fit(pairs, grid=GRID)

    assert result.real_pairs == 0
    assert result.params is NEUTRAL
    assert any("not counted" in caveat for caveat in result.caveats)


def test_the_result_counts_pairs_by_where_they_came_from() -> None:
    """So a reader can tell a fit resting on a runner's edits from one resting on their
    watch, without reading the pairs."""
    pairs = [
        *_pairs_from(CustomModelParams(lts3=0.25), count=6),
        *[
            PreferencePair(
                a=_route("a", (BUSY, 1000.0)),
                b=_route("b", (QUIET, 950.0)),
                preferred="b",
                source="history",
            )
            for _ in range(7)
        ],
    ]

    result = fit(pairs, grid=GRID, min_pairs=4)

    assert result.pairs_by_source == {"edit": 6, "history": 7}
    assert result.real_pairs == 13


def test_no_pairs_at_all_is_answered_rather_than_crashed() -> None:
    result = fit([], grid=GRID)

    assert result.params is NEUTRAL
    assert result.agreement == 0.0


def test_a_tie_scores_half_rather_than_nothing_or_everything() -> None:
    """Agreement is how often the vector would reproduce the choice, and a vector
    indifferent between two routes reproduces it half the time.

    Both alternatives are wrong in a way that shows up elsewhere in this file. Scoring a
    tie as agreement reports 100% for a vector that expresses nothing; scoring it as
    disagreement - which this did first - makes neutral score 0.0 on signal-free evidence,
    so the search pays any vector willing to have an opinion.
    """
    same = _route("a", (QUIET, 1000.0))
    pairs = [PreferencePair(a=same, b=_route("b", (QUIET, 1000.0)), preferred="a")]

    assert agreement_of(pairs, NEUTRAL) == 0.5
    assert agreement_of(pairs, CustomModelParams(lts2=0.25)) == 0.5


def test_a_pair_must_name_which_side_was_preferred() -> None:
    with pytest.raises(ValueError, match="preferred"):
        PreferencePair(a=_route("a"), b=_route("b"), preferred="neither")


def test_the_summary_line_leads_with_how_many_real_pairs_there_were() -> None:
    """Because that is the number that decides whether the vector means anything."""
    assert "0 real pair(s)" in str(fit([], grid=GRID))


# --- the command, and the round trip through YAML ----------------------------


def test_a_stored_pair_survives_the_round_trip_through_yaml(tmp_path: Path) -> None:
    """YAML has no tuple and a list is not hashable, so a way class is a `|`-joined string
    on disk. The one that matters is `none`: an LTS nobody could determine must come back
    as `None` and not as the four-character string, or every unmatched way silently becomes
    a level the fitter charges a multiplier for."""
    import yaml as yaml_module

    from longrun.cli.tune import _class_key, _pairs_in

    summary = RouteSummary.from_ways("r", [(WayClass(), 400.0), (WayClass(lts=3), 600.0)])
    (tmp_path / "p.yaml").write_text(
        yaml_module.safe_dump(
            {
                "pairs": [
                    {
                        "source": "edit",
                        "preferred": "b",
                        "a": {
                            "name": "a",
                            "length_m": summary.length_m,
                            "metres_by_class": {
                                _class_key(key): value
                                for key, value in summary.metres_by_class.items()
                            },
                        },
                        "b": {
                            "name": "b",
                            "length_m": 900.0,
                            "metres_by_class": {"1|false|true|false": 900.0},
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    restored = _pairs_in(tmp_path)

    assert len(restored) == 1
    assert restored[0].a.metres_by_class == summary.metres_by_class
    assert None in {key[0] for key in restored[0].a.metres_by_class}


def test_the_command_declines_to_fit_and_says_how_many_pairs_it_had(tmp_path: Path) -> None:
    """What `longrun tune` prints on a fresh install, which is the honest answer and is
    what `docs/tuning.md` publishes."""
    from typer.testing import CliRunner

    from longrun.cli.main import app

    result = CliRunner().invoke(app, ["tune", "--pairs", str(tmp_path)])

    assert result.exit_code == 0
    assert "unfitted" in result.output
    assert "fewer than the 12 needed" in result.output
