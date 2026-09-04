"""Population defaults used when no run history was uploaded (scope 6.2).

Minetti et al. (2002), "Energy cost of walking and running at extreme uphill and downhill
slopes", J Appl Physiol 93:1039-1046, gives the metabolic cost of running as a polynomial
in gradient. Holding metabolic power constant, the ratio of costs gives the speed a runner
can sustain on a slope relative to the flat — which is exactly the grade adjustment scope
6.2 derives from a user's own history when it exists.

Everything here is a population average and is labelled as such in the plan sheet. Scope
12 is explicit that pacing extrapolation is a guess; these numbers are the guess.
"""

from __future__ import annotations

#: Minetti's polynomial coefficients, highest power first, for J/(kg*m) against gradient.
_MINETTI = (155.4, -30.4, -43.3, 46.3, 19.5, 3.6)

#: Cost of level running, J/(kg*m). The constant term of the polynomial.
FLAT_COST = _MINETTI[-1]

#: Minetti measured -0.45 to +0.45. Beyond that the polynomial diverges fast, so it is
#: clamped rather than extrapolated: a 60% wall is not 8x harder than a 45% one.
MIN_GRADIENT = -0.45
MAX_GRADIENT = 0.45

#: A moderate long-run pace on the flat, 6:00/km. Only a starting point: any real plan
#: should be driven by the user's own curve.
DEFAULT_FLAT_SPEED_MS = 1000.0 / 360.0

#: Percent slowdown per 10 km beyond `FATIGUE_ONSET_M`, applied to sustainable speed.
DEFAULT_FATIGUE_DRIFT_PCT_PER_10KM = 4.0

#: Distance before fatigue drift begins to apply.
FATIGUE_ONSET_M = 10_000.0

#: Ceiling on the downhill speed bonus, as a multiple of flat speed.
#:
#: Minetti's curve assumes constant metabolic power, and on that assumption a -20%
#: descent works out at twice flat speed — around 3:00/km for a 6:00/km runner. Nobody
#: sustains that: descending is limited by eccentric loading, footing and nerve, not by
#: aerobic cost. Left uncapped this produces wildly optimistic ETAs on any mountain
#: route, which then propagate into every time-dependent scorer. Scope 6.2 derives real
#: descent sensitivity per user from history; this is the population stand-in.
MAX_DOWNHILL_SPEED_FACTOR = 1.25


def minetti_cost(gradient: float) -> float:
    """Metabolic cost of running at a gradient, in J/(kg*m).

    `gradient` is rise over run as a fraction, not a percentage: 0.05 is a 5% climb.
    """
    g = min(max(gradient, MIN_GRADIENT), MAX_GRADIENT)
    cost = 0.0
    for coefficient in _MINETTI:
        cost = cost * g + coefficient
    return cost


def grade_factor(gradient: float) -> float:
    """Speed multiplier relative to the flat, at constant metabolic power.

    Below 1 on a climb, above 1 on a gentle descent. Minetti's own curve already turns
    back past roughly -20%, because braking on a steep descent costs energy again — that
    shape is why the polynomial beats a linear rule of thumb. On top of it, the bonus is
    capped at `MAX_DOWNHILL_SPEED_FACTOR`, since the metabolic model does not know that
    a runner is limited by their quadriceps rather than their lungs.
    """
    return min(FLAT_COST / minetti_cost(gradient), MAX_DOWNHILL_SPEED_FACTOR)


def fatigue_factor(
    distance_m: float,
    drift_pct_per_10km: float = DEFAULT_FATIGUE_DRIFT_PCT_PER_10KM,
    onset_m: float = FATIGUE_ONSET_M,
) -> float:
    """Speed multiplier from accumulated distance (scope 6.2).

    A linear drift after an onset distance. Crude, and deliberately so: the real curve is
    derived per user from their longest efforts, and a more elaborate default would look
    more authoritative than it is.
    """
    if distance_m <= onset_m:
        return 1.0
    beyond_10km = (distance_m - onset_m) / 10_000.0
    return max(1.0 - beyond_10km * drift_pct_per_10km / 100.0, 0.2)
