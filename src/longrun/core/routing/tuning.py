"""Fitting the six priority parameters against pairwise route preferences (scope 7.1).

> **Tuning:** the ~6 parameters are fit against pairwise route preferences - the user's
> manual GPX edits (rejected vs. chosen segments) and the accepted-road set from history -
> by grid or Bayesian search maximizing agreement, with a detour-ratio regularizer so the
> optimizer can't solve the problem by routing through every park.

**Read `docs/tuning.md` before trusting a number this produces.** Both of the preference
sources scope 7.1 names are empty on a fresh install: no history has been ingested and
nobody has edited a generated route. A fit against labels this project generated for itself
would be circular - the scorers already read LTS, so maximising agreement with them
recovers the scorers' own weights and calls it a runner's preference. `fit` therefore
returns `NEUTRAL` and says so when it is given fewer than `MIN_PAIRS` real pairs, which is
the `MIN_SAMPLES_PER_BIN` shape from `pacing/history.py` and exists for the same reason.

## Why a recorded route is costed rather than re-routed (ADR 0020)

A grid over six parameters at five levels is 15,625 vectors. Re-routing each against a live
GraphHopper is hours; costing two already-drawn routes is milliseconds. So a vector is
scored by the priority it *implies* over routes that exist, which is the same arithmetic
the router runs inside its own search:

    cost(route) = sum over ways of  length / priority(way)

Lower cost is preferred, and a vector "agrees" with a pair when it costs the preferred
route lower. The limitation is real and is the ADR's subject: **the fit cannot discover a
route the router never proposed.** That is acceptable because the pairs *are* the
preference - a route nobody chose between is not evidence about anything.

## The regulariser is the load-bearing half

Scope 7.1 names its failure mode outright: an optimiser that can make footways free will
win every pair in any city with a greenway, by way of a five-kilometre detour. So the
objective is agreement *minus* a penalty on how far the vector's own choices stray beyond
`profile.detour_tolerance_pct` - which is a field that has existed since M1 and this is its
first reader.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from longrun.core.routing.custom_model import (
    AVOID_HIGH_STRESS,
    MIN_MULTIPLIER,
    NEUTRAL,
    PARAMETER_NAMES,
    CustomModelParams,
    effective_params,
)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable, Sequence

    from longrun.core.models.profile import PreferenceProfile

#: Levels each parameter is searched over. Five, spanning strong-avoid to strong-seek
#: around indifference, and symmetric in log space so "avoid twice as much" and "seek twice
#: as much" are the same distance from 1.0. 5**6 = 15,625 vectors, which is seconds.
DEFAULT_GRID: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0)

#: Below this many pairs, `fit` returns `NEUTRAL` with a caveat rather than six numbers.
#: A vector fitted to three pairs is noise that presents as a measurement, and a plan that
#: said "tuned" on that basis would be worse than one that said nothing.
MIN_PAIRS = 12

#: How hard the detour penalty bites, per unit of excess ratio. Set so that a vector buying
#: one pair's worth of agreement (1/N) with a 20% detour beyond tolerance loses more than
#: it gains - which is the trade scope 7.1 wants refused.
DETOUR_PENALTY_WEIGHT = 4.0


@dataclass(frozen=True)
class WayClass:
    """The attributes of a way that the six parameters can see.

    Deliberately the router's vocabulary and not the scorer's: `lts.py` knows about
    `sidewalk=*` and posted speeds and the router does not, so a class here is what
    `to_custom_model`'s conditions can actually match on. A tuner that fitted against
    attributes the router cannot read would produce a vector that does nothing.
    """

    lts: int | None = None
    unpaved: bool = False
    is_path: bool = False
    is_collector: bool = False

    def key(self) -> tuple[int | None, bool, bool, bool]:
        return (self.lts, self.unpaved, self.is_path, self.is_collector)


@dataclass(frozen=True)
class RouteSummary:
    """One route as metres-per-way-class, which is all a cost needs.

    Stored rather than the route: a labelled set built this way is self-contained YAML,
    needs no fixtures, no layer store and no router to replay, and survives a data vintage
    changing underneath it. There are at most 32 distinct classes, so this also collapses a
    thousand-way route into a handful of buckets and is what makes the grid cheap.
    """

    name: str
    length_m: float
    metres_by_class: dict[tuple[int | None, bool, bool, bool], float] = field(default_factory=dict)

    @classmethod
    def from_ways(cls, name: str, ways: Iterable[tuple[WayClass, float]]) -> RouteSummary:
        buckets: dict[tuple[int | None, bool, bool, bool], float] = {}
        total = 0.0
        for way_class, metres in ways:
            buckets[way_class.key()] = buckets.get(way_class.key(), 0.0) + metres
            total += metres
        return cls(name=name, length_m=total, metres_by_class=buckets)


def priority_of(key: tuple[int | None, bool, bool, bool], params: CustomModelParams) -> float:
    """The router's `priority` for one way class under one parameter vector.

    Multiplicative, because GraphHopper's `priority` rules multiply: a rule that matches
    scales what earlier rules left. So an unpaved path at LTS 1 gets both the unpaved
    multiplier and the path bonus, which is what the router would do to it.
    """
    lts, unpaved, is_path, is_collector = key
    priority = 1.0
    if lts == 2:
        priority *= params.lts2
    elif lts == 3:
        priority *= params.lts3
    elif lts is not None and lts >= 4:
        priority *= params.lts4
    if unpaved:
        priority *= params.unpaved
    if is_path:
        priority *= params.path_bonus
    if is_collector:
        priority *= params.missing_sidewalk
    return max(priority, MIN_MULTIPLIER)


def cost(summary: RouteSummary, params: CustomModelParams) -> float:
    """What this vector thinks the route costs. Lower is preferred.

    `length / priority` rather than `length * priority`: GraphHopper divides by priority,
    so a priority above 1.0 makes a way cheaper and therefore more attractive. Getting this
    backwards would fit every parameter to exactly the wrong sign and nothing downstream
    would notice, which is why `test_seeking_a_class_makes_a_route_using_it_cheaper` exists.
    """
    return sum(metres / priority_of(key, params) for key, metres in summary.metres_by_class.items())


@dataclass(frozen=True)
class PreferencePair:
    """Two routes between the same ends, one of which somebody preferred.

    `source` is not decoration. A pair derived from a runner's own GPX edit is evidence
    about that runner; one this project generated to test its own optimiser is not evidence
    about anybody, and `fit` counts them separately so a result cannot quietly rest on the
    second kind. `tests/eval`'s label carries the same field for the same reason.
    """

    a: RouteSummary
    b: RouteSummary
    preferred: str
    source: str = "synthetic"
    weight: float = 1.0
    #: Who made the choice, when a person did. ADR 0021 fits a vector "for the runner it
    #: was fitted for - never globally", which needs pairs to say whose they are.
    labeler: str | None = None

    def __post_init__(self) -> None:
        if self.preferred not in ("a", "b"):
            raise ValueError(f"preferred must be 'a' or 'b', got {self.preferred!r}")

    @property
    def chosen(self) -> RouteSummary:
        return self.a if self.preferred == "a" else self.b

    @property
    def rejected(self) -> RouteSummary:
        return self.b if self.preferred == "a" else self.a


@dataclass(frozen=True)
class TuningResult:
    """A fitted vector, and everything needed to decide whether to believe it."""

    params: CustomModelParams
    agreement: float
    penalty: float
    objective: float
    pairs_by_source: dict[str, int] = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)
    grid_size: int = 0

    @property
    def real_pairs(self) -> int:
        """Pairs from a person: an edit they made, or a road their history says they run.

        The number that decides whether this result means anything. Everything else is a
        test of the optimiser rather than a measurement of a preference.
        """
        return sum(count for source, count in self.pairs_by_source.items() if source != "synthetic")

    def __str__(self) -> str:
        from longrun.core.routing.custom_model import describe

        return (
            f"{describe(self.params)} | agreement {self.agreement:.1%} "
            f"| detour penalty {self.penalty:.3f} | {self.real_pairs} real pair(s)"
        )


def agreement_of(pairs: Sequence[PreferencePair], params: CustomModelParams) -> float:
    """How often this vector would reproduce the runner's choice. 1.0 is always, 0.5 is a
    coin flip.

    **A tie scores half, and getting this wrong distorts the whole fit.** The first version
    counted a tie as a disagreement, on the reasoning that a do-nothing vector should not
    score 100%. True, but the cost is worse than the disease: a neutral vector ties on every
    pair of equal-length routes, so it scored 0.0 on evidence containing no signal at all,
    and the search reliably preferred *any* discriminating vector to it. The optimiser was
    being paid to have opinions.

    Half is the honest reading. Agreement is the probability the vector reproduces the
    choice, and a vector indifferent between two routes reproduces it half the time. A
    do-nothing vector then scores 0.5 rather than 1.0 and still cannot beat one that agrees.
    """
    total = sum(pair.weight for pair in pairs)
    if total <= 0:
        return 0.0
    score = 0.0
    for pair in pairs:
        chosen, rejected = cost(pair.chosen, params), cost(pair.rejected, params)
        if chosen < rejected:
            score += pair.weight
        elif chosen == rejected:
            score += pair.weight * 0.5
    return score / total


def detour_penalty(
    pairs: Sequence[PreferencePair],
    params: CustomModelParams,
    *,
    tolerance_pct: float,
    weight: float = DETOUR_PENALTY_WEIGHT,
) -> float:
    """How far beyond the runner's detour tolerance this vector's own choices go.

    Scope 7.1's regulariser, and the thing that stops the optimiser "routing through every
    park". Measured on the route the vector *would choose* in each pair, against the
    shorter of the two on offer - so a vector that only ever picks the long way pays for
    it, and one that picks the long way when the long way is barely longer does not.

    `tolerance_pct` comes from `PreferenceProfile.detour_tolerance_pct`, which has existed
    since M1 and had no reader until this.
    """
    if not pairs:
        return 0.0
    allowed = 1.0 + tolerance_pct / 100.0
    excesses = []
    for pair in pairs:
        picked = pair.a if cost(pair.a, params) <= cost(pair.b, params) else pair.b
        shortest = min(pair.a.length_m, pair.b.length_m)
        if shortest <= 0:
            continue
        excesses.append(max(0.0, picked.length_m / shortest - allowed))
    if not excesses:
        return 0.0
    return weight * sum(excesses) / len(excesses)


def search(
    pairs: Sequence[PreferencePair],
    *,
    profile: PreferenceProfile | None = None,
    grid: Sequence[float] = DEFAULT_GRID,
    tolerance_pct: float = 10.0,
    penalty_weight: float = DETOUR_PENALTY_WEIGHT,
) -> tuple[CustomModelParams, float]:
    """The grid search `fit` runs, without `fit`'s refusal to run it on too little evidence.

    Separate so a held-out measurement can fit on part of the evidence and test on the rest
    (`holdout_agreement`): that is a question about the optimiser, asked of subsets far
    below `MIN_PAIRS`, and the gate that protects a *result* has no business refusing it.

    Every vector is costed with the multipliers the router would actually apply
    (`effective_params`), so a surface the runner stated is not learned a second time.
    """
    best: tuple[float, float, CustomModelParams] | None = None
    for values in itertools.product(grid, repeat=len(PARAMETER_NAMES)):
        params = CustomModelParams.from_vector(values)
        applied = effective_params(params, profile)
        score = agreement_of(pairs, applied) - detour_penalty(
            pairs, applied, tolerance_pct=tolerance_pct, weight=penalty_weight
        )
        # Ties go to the vector nearer neutral: with no evidence to separate two vectors,
        # the one that claims less is the honest answer, and it keeps the result stable.
        #
        # Measured in **log** space, because the grid is symmetric there and not in linear
        # space: 0.25 and 4.0 are the same claim in opposite directions, but |0.25 - 1| is
        # 0.75 against |4 - 1| = 3. A linear tie-break therefore made "avoid strongly" look
        # like a four-times smaller claim than "seek strongly", and the fit systematically
        # explained evidence by avoidance even where seeking fitted identically.
        distance = sum(abs(math.log(value)) for value in values)
        if best is None or (score, -distance) > (best[0], -best[1]):
            best = (score, distance, params)
    assert best is not None
    return best[2], best[0]


@dataclass(frozen=True)
class Holdout:
    """Agreement on pairs the fit did not see, against the vectors it has to beat.

    ADR 0021 said a fitted vector ships when its agreement is "meaningfully above 0.5".
    Measured in M23 that was the wrong bar: NEUTRAL costs a route by its length, so it is
    not a coin flip - it is "prefer the shorter line", which agrees with every runner who
    chose the shorter line. A fit earns its keep by beating *that*, on pairs it was not fitted
    to, which is what this reports.
    """

    folds: int
    pairs: int
    fitted: float
    neutral: float
    avoid_high_stress: float
    #: 95% Wilson interval on `fitted`, treating each held-out pair as one trial.
    interval: tuple[float, float]

    @property
    def beats_neutral(self) -> bool:
        """Whether the neutral vector's held-out agreement is below the interval's floor."""
        return self.interval[0] > self.neutral

    def __str__(self) -> str:
        low, high = self.interval
        verdict = "beats" if self.beats_neutral else "does not beat"
        return (
            f"held out over {self.folds} folds of {self.pairs} pair(s): fitted "
            f"{self.fitted:.1%} (95% {low:.1%}-{high:.1%}), neutral {self.neutral:.1%}, "
            f"avoid-high-stress {self.avoid_high_stress:.1%} - the fit {verdict} neutral"
        )


def wilson(proportion: float, trials: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval: honest at small n and at 0 or 1, where +-1.96 SE is not."""
    if trials <= 0:
        return (0.0, 1.0)
    denominator = 1 + z * z / trials
    centre = (proportion + z * z / (2 * trials)) / denominator
    half = z * math.sqrt(proportion * (1 - proportion) / trials + z * z / (4 * trials * trials))
    half /= denominator
    return (max(0.0, centre - half), min(1.0, centre + half))


def holdout_agreement(
    pairs: Sequence[PreferencePair],
    *,
    folds: int = 5,
    profile: PreferenceProfile | None = None,
    grid: Sequence[float] = DEFAULT_GRID,
    penalty_weight: float = DETOUR_PENALTY_WEIGHT,
) -> Holdout:
    """K-fold agreement: fit on all but one fold, score the fold, pool the folds.

    Folds are fixed by the pairs' own names (candidate ids, random at seed time), sorted, so
    the same evidence splits the same way on every run - a measurement that moved between
    runs would be unreviewable, for `fit`'s reason.
    """
    ordered = sorted(pairs, key=lambda pair: (pair.a.name, pair.b.name, pair.preferred))
    k = max(2, min(folds, len(ordered)))
    tolerance = 10.0 if profile is None else float(profile.detour_tolerance_pct.value)
    neutral = effective_params(NEUTRAL, profile)
    avoid = effective_params(AVOID_HIGH_STRESS, profile)

    scored: list[tuple[float, float, float, float]] = []  # weight, fitted, neutral, avoid
    for fold in range(k):
        held = [pair for index, pair in enumerate(ordered) if index % k == fold]
        train = [pair for index, pair in enumerate(ordered) if index % k != fold]
        if not held or not train:
            continue
        fitted, _ = search(
            train,
            profile=profile,
            grid=grid,
            tolerance_pct=tolerance,
            penalty_weight=penalty_weight,
        )
        applied = effective_params(fitted, profile)
        for pair in held:
            scored.append(
                (
                    pair.weight,
                    agreement_of([pair], applied),
                    agreement_of([pair], neutral),
                    agreement_of([pair], avoid),
                )
            )

    total = sum(weight for weight, *_ in scored) or 1.0

    def pooled(column: int) -> float:
        return sum(row[0] * row[column] for row in scored) / total

    fitted_share = pooled(1)
    return Holdout(
        folds=k,
        pairs=len(scored),
        fitted=fitted_share,
        neutral=pooled(2),
        avoid_high_stress=pooled(3),
        interval=wilson(fitted_share, len(scored)),
    )


def parameter_metres(summary: RouteSummary, parameter: str) -> float:
    """Metres of a route that one of the six parameters' terms would match."""

    def matches(key: tuple[int | None, bool, bool, bool]) -> bool:
        lts, unpaved, is_path, is_collector = key
        return {
            "lts2": lts == 2,
            "lts3": lts == 3,
            "lts4": lts == 4,
            "unpaved": unpaved,
            "path_bonus": is_path,
            "missing_sidewalk": is_collector,
        }[parameter]

    return sum(metres for key, metres in summary.metres_by_class.items() if matches(key))


def evidence_by_parameter(pairs: Sequence[PreferencePair]) -> dict[str, float]:
    """How many metres of difference between chosen and rejected each parameter has seen.

    A parameter no pair has ever separated two lines on is one the fit has no evidence
    about - and its fitted value is the grid's tie-break talking, not the runner. Printed
    beside the vector so a reader can tell the two apart.
    """
    return {
        name: round(
            sum(abs(parameter_metres(p.a, name) - parameter_metres(p.b, name)) for p in pairs), 1
        )
        for name in PARAMETER_NAMES
    }


def fit(
    pairs: Sequence[PreferencePair],
    *,
    profile: PreferenceProfile | None = None,
    grid: Sequence[float] = DEFAULT_GRID,
    min_pairs: int = MIN_PAIRS,
    penalty_weight: float = DETOUR_PENALTY_WEIGHT,
) -> TuningResult:
    """Grid-search the six parameters, maximising agreement minus the detour penalty.

    Deterministic: the grid is enumerated in a fixed order and ties are broken by
    preferring the vector closer to neutral, so the same pairs give the same answer on
    every run and every platform. A seeded random search would not, and a tuning result
    that moved between runs would be unreviewable.

    Returns `NEUTRAL` with a caveat when there are too few pairs from a person. That is
    the milestone's central honesty rule rather than a guard clause: scope 7.1's two
    preference sources are empty on a fresh install, and six numbers fitted to nothing
    would be an assumption wearing a measurement's clothes.
    """
    tolerance = 10.0 if profile is None else float(profile.detour_tolerance_pct.value)
    by_source: dict[str, int] = {}
    for pair in pairs:
        by_source[pair.source] = by_source.get(pair.source, 0) + 1
    real = sum(count for source, count in by_source.items() if source != "synthetic")

    caveats: list[str] = []
    if real < min_pairs:
        caveats.append(
            f"{real} preference pair(s) from a person, fewer than the {min_pairs} needed; "
            "returning the neutral vector rather than fitting one"
        )
        if by_source.get("synthetic"):
            caveats.append(
                f"{by_source['synthetic']} synthetic pair(s) were supplied and are not "
                "counted: they test the optimiser, not a runner's preference"
            )
        return TuningResult(
            params=NEUTRAL,
            agreement=agreement_of(pairs, effective_params(NEUTRAL, profile)) if pairs else 0.0,
            penalty=detour_penalty(
                pairs,
                effective_params(NEUTRAL, profile),
                tolerance_pct=tolerance,
                weight=penalty_weight,
            ),
            objective=0.0,
            pairs_by_source=by_source,
            caveats=caveats,
            grid_size=0,
        )

    params, score = search(
        pairs, profile=profile, grid=grid, tolerance_pct=tolerance, penalty_weight=penalty_weight
    )
    applied = effective_params(params, profile)
    agreement = agreement_of(pairs, applied)
    penalty = detour_penalty(pairs, applied, tolerance_pct=tolerance, weight=penalty_weight)
    if params.is_neutral:
        caveats.append("the best vector on this evidence is the neutral one")
    return TuningResult(
        params=params,
        agreement=agreement,
        penalty=penalty,
        objective=score,
        pairs_by_source=by_source,
        caveats=caveats,
        grid_size=len(grid) ** len(PARAMETER_NAMES),
    )


__all__ = [
    "Holdout",
    "evidence_by_parameter",
    "holdout_agreement",
    "parameter_metres",
    "search",
    "wilson",
    "DEFAULT_GRID",
    "DETOUR_PENALTY_WEIGHT",
    "MIN_PAIRS",
    "PreferencePair",
    "RouteSummary",
    "TuningResult",
    "WayClass",
    "agreement_of",
    "cost",
    "detour_penalty",
    "fit",
    "priority_of",
]
