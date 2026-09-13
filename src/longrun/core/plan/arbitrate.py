"""Arbitration between routes and between flags (scope 8.4).

Lexicographic by tier — safety, then physiological, then comfort — and only within a tier
does a weighted sum of severity apply. The ordering is the point: no weight a user can
set on shade may outrank a hard hostility flag, so the comparison is structured so that
such a trade is unrepresentable rather than merely discouraged.

The other rule with teeth: **same-tier conflicts are not resolved silently.** When two
alternatives are genuinely close, the loop returns a `TradeOff` and stops, rather than
picking one and hiding the choice. Scope 8.4 spells out why — "A adds 0.8 km and a
signalized crossing; B is fully shaded but has 600 m without sidewalk at mile 41" is a
decision the runner should make.

This module compares *already-scored* candidates and never imports a router, so the whole
of scope 8.4 is testable with hand-built `ScorerResult`s and no routing engine at all.
Proposing the candidates is the loop's job, not arbitration's: until M5.3 this docstring
claimed the module "takes `propose_alternatives` as an injected callback", which described
a parameter that did not exist anywhere in the tree.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pydantic import BaseModel, Field

from longrun.core.geo.segments import position_fraction
from longrun.core.models.geometry import Segment
from longrun.core.models.measurement import Flag, FlagKind, ScorerResult, Tier
from longrun.core.models.plan import TradeOff
from longrun.core.plan.weighting import weighted_severity

#: Within a tier, two candidates closer than this in weighted severity are a genuine
#: trade-off rather than a win. Deliberately generous: the cost of asking is one
#: question, and the cost of a silent wrong pick is a route the runner will not run.
TRADEOFF_MARGIN = 0.15


class RankedFlag(BaseModel):
    """A flag with the position weighting applied, ready to sort."""

    flag: Flag
    fraction: float = Field(ge=0.0, le=1.0)
    weighted: float = Field(ge=0.0)

    @property
    def sort_key(self) -> tuple[int, int, float, str]:
        """Lexicographic: tier first, then hard before soft, then weighted severity.

        The trailing segment id makes the order total, so a golden file cannot reorder
        between runs on ties.
        """
        return (
            int(self.flag.tier),
            -int(self.flag.kind),
            -self.weighted,
            self.flag.segment_id,
        )


class TierScore(BaseModel):
    """One tier's summary for a candidate route."""

    tier: Tier
    hard_count: int = 0
    weighted_sum: float = 0.0


class Candidate(BaseModel):
    """A scored route, reduced to what arbitration needs to compare it."""

    label: str
    tiers: dict[int, TierScore] = Field(default_factory=dict)
    ranked: list[RankedFlag] = Field(default_factory=list)

    def tier_key(self) -> tuple[float, ...]:
        """Comparable lexicographic key: hard failures then weighted sum, per tier.

        Lower is better. Hard counts precede weighted sums within each tier, so a hard
        safety flag can never be offset by any amount of comfort improvement.
        """
        key: list[float] = []
        for tier in (Tier.SAFETY, Tier.PHYSIOLOGICAL, Tier.COMFORT):
            score = self.tiers.get(int(tier), TierScore(tier=tier))
            key.extend((float(score.hard_count), score.weighted_sum))
        return tuple(key)

    def worst(self, n: int = 5) -> list[Flag]:
        return [r.flag for r in self.ranked[:n]]


class Arbitration(BaseModel):
    """The outcome of comparing candidates, or of ranking one route's flags."""

    winner: str | None = None
    trade_off: TradeOff | None = None
    ranked: list[RankedFlag] = Field(default_factory=list)
    residual: list[Flag] = Field(default_factory=list)

    @property
    def needs_input(self) -> bool:
        """True when the loop must stop and ask (scope 8.1 step 6)."""
        return self.trade_off is not None


def rank_flags(
    results: Sequence[ScorerResult],
    segments: Sequence[Segment],
    total_m: float,
    exclude: frozenset[str] = frozenset(),
) -> list[RankedFlag]:
    """Order every flag across every scorer, worst first.

    `exclude` carries locked segment ids: scope 8.1 step 6 collects worst segments
    "excluding locked ranges", since there is no point proposing a fix the loop is
    forbidden to apply.
    """
    by_id = {s.id: s for s in segments}
    ranked: list[RankedFlag] = []

    for result in results:
        for flag in result.flags:
            if flag.segment_id in exclude:
                continue
            segment = by_id.get(flag.segment_id)
            fraction = position_fraction(segment, total_m) if segment else 0.0
            ranked.append(
                RankedFlag(
                    flag=flag,
                    fraction=fraction,
                    weighted=weighted_severity(flag, fraction),
                )
            )

    return sorted(ranked, key=lambda r: r.sort_key)


def score_candidate(
    label: str,
    results: Sequence[ScorerResult],
    segments: Sequence[Segment],
    total_m: float,
) -> Candidate:
    """Reduce a scored route to its per-tier summary."""
    ranked = rank_flags(results, segments, total_m)
    tiers: dict[int, TierScore] = {
        int(t): TierScore(tier=t) for t in (Tier.SAFETY, Tier.PHYSIOLOGICAL, Tier.COMFORT)
    }
    for item in ranked:
        score = tiers[int(item.flag.tier)]
        score.weighted_sum += item.weighted
        if item.flag.kind is FlagKind.HARD:
            score.hard_count += 1
    return Candidate(label=label, tiers=tiers, ranked=ranked)


def compare(
    a: Candidate,
    b: Candidate,
    segment_id: str,
    describe: Callable[[Candidate, Candidate], str] | None = None,
    margin: float = TRADEOFF_MARGIN,
) -> Arbitration:
    """Choose between two scored alternatives, or surface the trade-off.

    A clear win in a higher tier settles it outright. Only when the candidates are
    separated solely within one tier, and by less than `margin`, is the choice handed
    back to the user.
    """
    key_a, key_b = a.tier_key(), b.tier_key()

    # Identical scores: pick deterministically rather than asking about nothing.
    if key_a == key_b:
        return _outcome(winner=a.label, ranked=a.ranked)

    decisive = _first_difference(key_a, key_b)
    if decisive is None or not _is_close(key_a, key_b, decisive, margin):
        winner = a if key_a < key_b else b
        return _outcome(winner=winner.label, ranked=winner.ranked)

    comparison = describe(a, b) if describe else f"{a.label} and {b.label} score within {margin}"
    return _outcome(
        ranked=(a if key_a <= key_b else b).ranked,
        trade_off=TradeOff(
            segment_id=segment_id,
            option_a=a.label,
            option_b=b.label,
            comparison=comparison,
        ),
    )


def arbitrate(
    candidates: Sequence[Candidate],
    *,
    segment_id: str,
    describe: Callable[[Candidate, Candidate], str] | None = None,
    margin: float = TRADEOFF_MARGIN,
) -> Arbitration:
    """Choose among any number of scored candidates - scope 8.1 step 6's decision.

    `compare` is pairwise, and `_is_close` is a **local** property: A and B may be close
    enough to be a trade-off while A beats C outright. So comparing a field of four in
    arbitrary pairs gives an answer - and, worse, a decision about whether to stop and ask
    the user at all - that depends on the pairing order.

    `Candidate.tier_key()` is a total order over the whole field and settles it. `compare`
    then does the one job it was written for: deciding whether the best two are too close
    to separate. Ties break on the label, so a golden cannot reorder between runs.
    """
    if not candidates:
        return Arbitration()
    ordered = sorted(candidates, key=lambda c: (c.tier_key(), c.label))
    if len(ordered) == 1:
        return _outcome(winner=ordered[0].label, ranked=ordered[0].ranked)
    return compare(ordered[0], ordered[1], segment_id, describe=describe, margin=margin)


def _outcome(
    *,
    ranked: Sequence[RankedFlag],
    winner: str | None = None,
    trade_off: TradeOff | None = None,
) -> Arbitration:
    """An arbitration that fills the `residual` it declares.

    Scope 8.4: "residual flags are always listed". The field has existed since M1 and
    `compare` never wrote it, so every arbitration reported an empty residual - which
    reads as "nothing is wrong with the winner" rather than "nobody looked".
    """
    return Arbitration(
        winner=winner,
        trade_off=trade_off,
        ranked=list(ranked),
        residual=[item.flag for item in ranked],
    )


def _first_difference(key_a: tuple[float, ...], key_b: tuple[float, ...]) -> int | None:
    """Index of the first differing element of two tier keys."""
    for i, (x, y) in enumerate(zip(key_a, key_b, strict=True)):
        if x != y:
            return i
    return None


def _is_close(
    key_a: tuple[float, ...], key_b: tuple[float, ...], index: int, margin: float
) -> bool:
    """Whether the decisive difference is a narrow one within a single tier.

    A difference in a hard-flag count is never close: those sit at even indices and one
    hard safety failure is categorically worse than none, however small the arithmetic
    gap looks.
    """
    if index % 2 == 0:
        return False
    return abs(key_a[index] - key_b[index]) < margin


def residual_flags(
    results: Sequence[ScorerResult],
    segments: Sequence[Segment],
    total_m: float,
) -> list[Flag]:
    """Every flag that survived, worst first (scope 8.4: "always listed").

    The plan sheet prints these whether or not anything was fixed. A route that could not
    be improved is not a route without problems.
    """
    return [r.flag for r in rank_flags(results, segments, total_m)]
