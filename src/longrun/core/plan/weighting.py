"""Position weighting: hostile conditions late in a run cost more (scope 8.2).

w(d) = 1.0 through the first 40% of distance, then rises linearly to 2.0 at the finish.

Three properties of that shape are load-bearing:

* **Piecewise linear, never stepped.** A stepped weight creates a boundary the optimizer
  games — it will shove a hostile segment to just before the step and call the route
  improved. A continuous ramp has no such edge to exploit.
* **It lives in the scoring loop, not the router.** The router cannot know distance-along-
  route before the route exists. So: route with static weights, score with w(d), then
  reroute the segments that came out flagged late.
* **It is not applied to legality.** An illegal segment is illegal at kilometre 2 and at
  kilometre 80. Multiplying a hard safety failure by position would imply it is more
  acceptable early, which it is not.

Heat gets a lower ceiling than hostility because the ETA vector already carries time of
day: a late segment is usually also the hot part of the afternoon, and applying the full
ramp on top would count the same fact twice.
"""

from __future__ import annotations

from longrun.core.models.measurement import Flag

#: Fraction of the route over which weight stays flat.
FLAT_FRACTION = 0.4

#: Weight at the finish for hostility.
MAX_WEIGHT = 2.0

#: Lower ceiling for heat, to avoid double-counting time of day.
HEAT_MAX_WEIGHT = 1.5

#: Scorers whose flags are position-weighted, and the ceiling each uses.
POSITION_WEIGHTED: dict[str, float] = {
    "segment_hostility": MAX_WEIGHT,
    "crossings": MAX_WEIGHT,
    "stop_density": MAX_WEIGHT,
    "surface_profile": MAX_WEIGHT,
    "heat_stress": HEAT_MAX_WEIGHT,
    "trail_status": MAX_WEIGHT,
}

#: Scorers that emit no flags at all, so no position weight applies. Not an oversight:
#: `air_quality` is ADR 0006's decision (section 8.3 has no air-quality row, and inventing
#: a threshold would attach a sign to a measurement, which section 3.2 forbids), and the
#: rest measure things the plan sheet reports without ever crossing a threshold.
MEASUREMENT_ONLY = frozenset(
    {
        "air_quality",
        "microclimate",
        "services_along",
        "crew_points",
        "start_time_optimizer",
    }
)

#: Scorers that *do* flag and whose position weighting nobody has chosen, so they take the
#: flat 1.0 default.
#:
#: Listed rather than left to fall through, which is the whole point: an unlisted scorer is
#: indistinguishable from one someone decided about, and `trail_status` sat in that state
#: from M1 until M4 found it. Whether darkness at kilometre 80 should outweigh darkness at
#: kilometre 2 is a real question and a `tuning.py` one (M6); until then the answer here is
#: "no, and that was noticed rather than inherited".
FLAT_BY_DEFAULT = frozenset(
    {
        "sun_exposure",
        "lighting",
        "resupply_schedule",
        "transit",
        "bailouts",
        "cell_coverage",
    }
)

#: Never position-weighted: legality does not become acceptable early in a run.
#:
#: `trail_status` is deliberately absent and belongs in `POSITION_WEIGHTED` instead. The
#: three M4 scorers split on what kind of claim they make: a closed road and a locked gate
#: are categorical facts about the ground, true at kilometre 2 and at kilometre 80 alike,
#: while a trail alert is a *condition you endure* — mud, ice, overgrowth — which compounds
#: with fatigue exactly as `surface_profile` does. Until M4 it was in neither collection and
#: so behaved as never-weighted while being documented as neither, which
#: `test_every_scorer_has_a_declared_weighting` now prevents recurring.
NEVER_WEIGHTED = frozenset({"legality", "hazards", "closures", "access_hours"})


def position_weight(fraction: float, ceiling: float = MAX_WEIGHT) -> float:
    """w(d) for a point at `fraction` of the way along the route.

    `fraction` is clamped, so a caller need not special-case a segment midpoint that
    rounds fractionally past 1.0 on the final segment.
    """
    f = min(max(fraction, 0.0), 1.0)
    if f <= FLAT_FRACTION:
        return 1.0
    progress = (f - FLAT_FRACTION) / (1.0 - FLAT_FRACTION)
    return 1.0 + progress * (ceiling - 1.0)


def weight_for(scorer: str, fraction: float) -> float:
    """The position weight a given scorer's flag receives at a given point."""
    if scorer in NEVER_WEIGHTED:
        return 1.0
    ceiling = POSITION_WEIGHTED.get(scorer)
    if ceiling is None:
        return 1.0
    return position_weight(fraction, ceiling=ceiling)


def weighted_severity(flag: Flag, fraction: float) -> float:
    """A flag's severity after position weighting.

    Deliberately not clamped back into [0, 1]: the weighted value is a ranking quantity,
    and squashing it at 1.0 would erase the distinction the weighting exists to create.
    """
    return flag.severity * weight_for(flag.scorer, fraction)
