"""Acceptance metrics: the three numbers scope 7.1 asks every reference route to publish.

> Acceptance metrics: fraction of length at LTS >=3, count of LTS 4 segments, detour ratio
> vs. shortest legal route, published for a set of reference routes.

Two of the three have been computed on every plan since M1 and read by nothing:
`segment_hostility` writes `fraction_lts3_plus` and `lts4_count` into its route summary and
they have never reached a sheet, an API or a test. This module reads them.

The third did not exist, and it is the one the milestone actually needs. **Detour ratio is
the regulariser's own measurement** - scope 7.1 puts it there precisely so a fitter "can't
solve the problem by routing through every park" - so a tuner without it has no way to tell
a good vector from one that wins every pair by adding five kilometres.

`detour_ratio` is `None` when no router answered, never 1.0. A 1.0 would read as "this
route is as short as it could be", which is a measurement; `None` is "nobody measured",
which is the truth. Scope 3.6 applied to a ratio - the same rule that makes an unchecked
jurisdiction `unknown` rather than `absent`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from longrun.core.models.geometry import LatLon
from longrun.core.scorers._common import ROUTE_SUMMARY_ID

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    from longrun.core.models.geometry import Route
    from longrun.core.models.measurement import ScorerResult
    from longrun.core.routing.base import Router

#: What `distance_influence` has to reach before GraphHopper returns the shortest legal
#: line rather than a compromise with its own priority terms.
#:
#: **Measured, not chosen.** Against the live Bay Area LTS graph on two pairs, the answer
#: converges between 1e4 and 1e5 and does not move again at 1e6:
#:
#: | pair | 1e4 | 1e5 | 1e6 |
#: |---|---|---|---|
#: | Ferry Building -> de Young | 7605.6 m | 7567.5 m | 7567.5 m |
#: | Presidio -> Mission | 8370.3 m | 8349.9 m | 8349.9 m |
SHORTEST_DISTANCE_INFLUENCE = 100_000

#: The lowest `distance_influence` a query-time model may carry, because the server says
#: so: *"CustomModel in query can only use distance_influence bigger or equal to 70.0"*.
#: A query model can make distance matter more than the profile does and never less, which
#: is worth recording because it means "shortest legal" is always reachable from here and
#: "longest scenic" is not.
MIN_DISTANCE_INFLUENCE = 70


@dataclass(frozen=True)
class AcceptanceMetrics:
    """Scope 7.1's three numbers, plus what they were derived from.

    Every field that could be unmeasured is `None` rather than a default. A metric with a
    plausible zero in it is worse than a missing one: it survives review.
    """

    length_m: float
    fraction_lts3_plus: float | None = None
    lts4_count: int | None = None
    shortest_legal_m: float | None = None
    detour_ratio: float | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Whether all three of scope 7.1's metrics were actually measured."""
        return (
            self.fraction_lts3_plus is not None
            and self.lts4_count is not None
            and self.detour_ratio is not None
        )

    def as_dict(self) -> dict[str, Any]:
        """Rounded for a golden file and for `plan.json`.

        Rounded here rather than at the comparison, for the reason `expectation.digest`
        gives: raw f64 never compares equal across platforms, and M5.13 is the standing
        proof that this project gets bitten by exactly that.
        """
        return {
            "length_m": round(self.length_m, 1),
            "fraction_lts3_plus": (
                None if self.fraction_lts3_plus is None else round(self.fraction_lts3_plus, 4)
            ),
            "lts4_count": self.lts4_count,
            "shortest_legal_m": (
                None if self.shortest_legal_m is None else round(self.shortest_legal_m, 1)
            ),
            "detour_ratio": None if self.detour_ratio is None else round(self.detour_ratio, 4),
            "reasons": list(self.reasons),
        }

    def __str__(self) -> str:
        lts3 = "unknown" if self.fraction_lts3_plus is None else f"{self.fraction_lts3_plus:.1%}"
        lts4 = "unknown" if self.lts4_count is None else str(self.lts4_count)
        detour = "not measured" if self.detour_ratio is None else f"{self.detour_ratio:.3f}x"
        return f"LTS>=3 {lts3} of length; LTS 4 segments {lts4}; detour {detour}"


def _hostility_summary(results: Sequence[ScorerResult]) -> dict[str, Any] | None:
    """`segment_hostility`'s route-level row, or `None` if it did not run.

    Looked up by scorer name and `ROUTE_SUMMARY_ID` rather than by position: the registry's
    order is dependency order and has changed twice, and a positional read would have gone
    silently wrong both times.
    """
    for result in results:
        if result.name != "segment_hostility":
            continue
        for measurement in result.measurements:
            if measurement.segment_id == ROUTE_SUMMARY_ID:
                return dict(measurement.values)
    return None


def shortest_legal_length(
    router: Router,
    waypoints: Sequence[LatLon],
    *,
    custom_model: dict[str, Any] | None = None,
) -> float:
    """The length of the shortest route the legal excludes allow, in metres.

    The denominator scope 7.1 names, and the only honest one. Against a shortest path that
    ignored legality, a route that had to use the one legal bridge would read as a detour
    it chose, when it is the line it was given - so the excludes stay on and only the
    preference terms come off.

    Raises whatever the router raises. A caller that wants a metric rather than an
    exception uses `metrics_with_router`, which is the one that degrades.
    """
    model = dict(custom_model or {})
    model["distance_influence"] = SHORTEST_DISTANCE_INFLUENCE
    return router.route(list(waypoints), custom_model=model).length_m


def acceptance_metrics(
    route: Route,
    results: Sequence[ScorerResult] = (),
    *,
    shortest_m: float | None = None,
    reasons: Sequence[str] = (),
) -> AcceptanceMetrics:
    """Scope 7.1's three metrics for one route.

    Pure: takes a length that somebody else measured rather than routing. The routing call
    belongs to `metrics_with_router`, which keeps this testable with no server and keeps
    the one function that can spend an API call visible.
    """
    notes = list(reasons)
    summary = _hostility_summary(results)
    if summary is None:
        notes.append("segment_hostility did not run, so no LTS metrics")

    fraction = None if summary is None else summary.get("fraction_lts3_plus")
    count = None if summary is None else summary.get("lts4_count")

    detour: float | None = None
    if shortest_m is None:
        notes.append("no router answered, so the detour ratio is unmeasured")
    elif shortest_m <= 0:
        notes.append("the shortest legal route measured zero, so the detour ratio is undefined")
    else:
        detour = route.length_m / shortest_m

    return AcceptanceMetrics(
        length_m=route.length_m,
        fraction_lts3_plus=None if fraction is None else float(fraction),
        lts4_count=None if count is None else int(count),
        shortest_legal_m=shortest_m,
        detour_ratio=detour,
        reasons=notes,
    )


def metrics_with_router(
    route: Route,
    results: Sequence[ScorerResult],
    router: Router | None,
    waypoints: Sequence[LatLon] | None = None,
    *,
    custom_model: dict[str, Any] | None = None,
) -> AcceptanceMetrics:
    """`acceptance_metrics` plus the one routing call, degrading rather than raising.

    Every failure becomes a reason on the metrics, because this runs inside a plan and a
    plan that died for want of a denominator would have traded a whole sheet for one
    number. The same shape `hazards.py` established and every scorer since has copied.
    """
    ends = (
        list(waypoints)
        if waypoints
        else [LatLon(lat=point.lat, lon=point.lon) for point in (route.points[0], route.points[-1])]
    )
    if router is None:
        return acceptance_metrics(route, results, reasons=["no router configured"])
    try:
        shortest = shortest_legal_length(router, ends, custom_model=custom_model)
    except Exception as exc:  # noqa: BLE001 - a metric is never worth failing a plan for
        return acceptance_metrics(
            route, results, reasons=[f"shortest legal route: {type(exc).__name__}: {exc}"]
        )
    return acceptance_metrics(route, results, shortest_m=shortest)


__all__ = [
    "MIN_DISTANCE_INFLUENCE",
    "SHORTEST_DISTANCE_INFLUENCE",
    "AcceptanceMetrics",
    "acceptance_metrics",
    "metrics_with_router",
    "shortest_legal_length",
]
