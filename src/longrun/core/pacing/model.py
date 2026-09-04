"""The pacing model and the ETA vector (scope 3.3, 7.3).

This is the second-highest fan-out module in the project. Scope 3.3 makes everything
time-aware — shade, daylight, store hours, transit and forecasts are all evaluated at the
projected arrival time at each point — so every scorer in scope 7.4, 7.5, 7.6 and 7.7
consumes what `pacing_model` returns.

**ETAs are aligned one-to-one with route points**, not with segments. Segments are derived
and can be recomputed at a different resolution; points are the route. Aligning to
segments would make re-segmentation after a reroute a re-interpolation problem.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from longrun.core.geo.dem import grades
from longrun.core.models.geometry import Route, Segment
from longrun.core.pacing.curves import POPULATION_CURVES, PacingCurves


class ETAVector(BaseModel):
    """Projected arrival time at every route point, plus how it was arrived at."""

    etas: list[datetime]
    moving_time_s: float = Field(ge=0)
    stopped_time_s: float = Field(default=0.0, ge=0)
    caveats: list[str] = Field(default_factory=list)

    @property
    def start(self) -> datetime:
        return self.etas[0]

    @property
    def finish(self) -> datetime:
        return self.etas[-1]

    @property
    def total_time(self) -> timedelta:
        return self.finish - self.start

    def at_distance(self, distance_m: float, route: Route) -> datetime:
        """The ETA at an arbitrary distance along the route, linearly interpolated."""
        points = route.points
        if distance_m <= points[0].cum_dist_m:
            return self.etas[0]
        for i in range(1, len(points)):
            if points[i].cum_dist_m >= distance_m:
                span = points[i].cum_dist_m - points[i - 1].cum_dist_m
                if span <= 0:
                    return self.etas[i]
                fraction = (distance_m - points[i - 1].cum_dist_m) / span
                delta = (self.etas[i] - self.etas[i - 1]) * fraction
                return self.etas[i - 1] + delta
        return self.etas[-1]

    def for_segments(self, segments: list[Segment], route: Route) -> list[datetime]:
        """ETA at each segment's midpoint.

        The midpoint, not the start: a scorer asking "what is the sun doing on this
        segment" wants the middle of it, and on a long segment the start can be many
        minutes early.
        """
        return [self.at_distance(s.cum_start_m + s.length_m / 2.0, route) for s in segments]


def pacing_model(
    route: Route,
    start_time: datetime,
    curves: PacingCurves = POPULATION_CURVES,
    elevations: list[float | None] | None = None,
    unpaved: list[bool] | None = None,
    stop_allowance_s: float = 0.0,
) -> ETAVector:
    """Project an arrival time at every point on the route.

    `stop_allowance_s` is spread evenly over the route rather than placed at the stops
    that cause it, because the crossings and stop-density scorers that know where those
    are run *after* this. Their output refines the vector on a later pass; the first pass
    only has to be right in aggregate.
    """
    points = route.points
    n = len(points)
    if elevations is not None and len(elevations) != n:
        raise ValueError(f"got {len(elevations)} elevations for {n} route points")
    if unpaved is not None and len(unpaved) != n:
        raise ValueError(f"got {len(unpaved)} surface flags for {n} route points")

    gradients = _gradients(route, elevations)
    total_m = route.length_m
    stop_per_m = stop_allowance_s / total_m if total_m > 0 else 0.0

    etas = [start_time]
    moving_s = 0.0
    stopped_s = 0.0

    for i in range(1, n):
        span_m = points[i].cum_dist_m - points[i - 1].cum_dist_m
        if span_m <= 0:
            etas.append(etas[-1])
            continue
        speed = curves.speed_ms(
            gradient=gradients[i],
            distance_m=points[i].cum_dist_m,
            unpaved=bool(unpaved[i]) if unpaved else False,
        )
        leg_moving = span_m / speed
        leg_stopped = span_m * stop_per_m
        moving_s += leg_moving
        stopped_s += leg_stopped
        etas.append(etas[-1] + timedelta(seconds=leg_moving + leg_stopped))

    return ETAVector(
        etas=etas,
        moving_time_s=moving_s,
        stopped_time_s=stopped_s,
        caveats=_caveats(curves, total_m, elevations),
    )


def _gradients(route: Route, elevations: list[float | None] | None) -> list[float]:
    """Gradient as a fraction at every point; flat where elevation is unknown."""
    if elevations is None:
        return [0.0] * len(route.points)
    return [(g / 100.0) if g is not None else 0.0 for g in grades(route, elevations)]


def _caveats(
    curves: PacingCurves, total_m: float, elevations: list[float | None] | None
) -> list[str]:
    """Everything the plan sheet must say about how trustworthy these ETAs are.

    Scope 12: pacing extrapolation beyond the longest recorded effort is a guess and is
    labelled as one. Producing the caveats here, rather than in the renderer, means a
    scorer cannot consume the ETAs while quietly dropping the warning.
    """
    out: list[str] = []
    if curves.provenance == "population":
        out.append(
            "Pacing uses a population grade-adjusted curve (Minetti) with a default "
            "fatigue drift; no run history was supplied."
        )
    if curves.extrapolates_beyond_history(total_m):
        longest = curves.longest_effort_m
        if longest is None:
            out.append(
                f"No recorded effort to compare against {total_m / 1000:.0f} km; "
                f"projected times are a guess."
            )
        else:
            out.append(
                f"Longest recorded effort is {longest / 1000:.0f} km against a "
                f"{total_m / 1000:.0f} km plan; projected times are unreliable."
            )
    if elevations is None:
        out.append("No elevation supplied; ETAs assume flat terrain throughout.")
    elif any(e is None for e in elevations):
        missing = sum(1 for e in elevations if e is None)
        out.append(
            f"Elevation missing at {missing} of {len(elevations)} points; those "
            f"stretches were paced as flat."
        )
    return out
