"""What changed between two routes (scope 7.8's `route_diff`).

Named in three docstrings since M1 and written nowhere until M5.7. Scope 7.8 says it is
"used for trade-off presentation and iteration review", which is what it is shaped for:
the question a reader has is *where* two routes disagree and by how much, not whether they
are byte-identical.

Distances are measured along each route rather than between corresponding points, because
the two point lists are not corresponding - a reroute changes the count, and a matched
route changes it again.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from longrun.core.geo.gpx import haversine_m
from longrun.core.models.geometry import Route

#: How far apart two routes must be, laterally, before this calls it a difference. Below
#: this they are the same street: a matched route is snapped to the graph and a routed one
#: is not, so two readings of the same line differ by metres.
SAME_LINE_M = 25.0


@dataclass(frozen=True)
class DiffSpan:
    """One stretch where the two routes are not on the same line."""

    start_m: float
    end_m: float
    max_offset_m: float

    @property
    def length_m(self) -> float:
        return self.end_m - self.start_m


@dataclass(frozen=True)
class RouteDiff:
    """Two routes, compared."""

    length_delta_m: float
    spans: list[DiffSpan] = field(default_factory=list)
    #: How much of the first route the second one also runs, 0 to 1.
    shared_fraction: float = 1.0

    def summary(self) -> str:
        if not self.spans:
            return f"same line; {self.length_delta_m:+.0f} m"
        return (
            f"{len(self.spans)} difference(s) over "
            f"{sum(s.length_m for s in self.spans) / 1000:.2f} km; "
            f"{self.length_delta_m:+.0f} m overall"
        )


def route_diff(a: Route, b: Route, *, tolerance_m: float = SAME_LINE_M) -> RouteDiff:
    """Where `a` and `b` part company, measured along `a`."""
    spans: list[DiffSpan] = []
    open_start: float | None = None
    worst = 0.0
    shared = 0
    previous = a.points[0].cum_dist_m

    for point in a.points:
        offset = _nearest_m(point.lat, point.lon, b)
        if offset > tolerance_m:
            if open_start is None:
                open_start = previous
                worst = offset
            worst = max(worst, offset)
        else:
            shared += 1
            if open_start is not None:
                spans.append(
                    DiffSpan(start_m=open_start, end_m=point.cum_dist_m, max_offset_m=worst)
                )
                open_start = None
        previous = point.cum_dist_m

    if open_start is not None:
        spans.append(
            DiffSpan(start_m=open_start, end_m=a.points[-1].cum_dist_m, max_offset_m=worst)
        )

    return RouteDiff(
        length_delta_m=b.length_m - a.length_m,
        spans=spans,
        shared_fraction=shared / len(a.points) if a.points else 1.0,
    )


def _nearest_m(lat: float, lon: float, other: Route) -> float:
    """Distance to the closest point of `other`.

    Point-to-point rather than point-to-segment, which over-reports on a sparse route by
    up to half a point spacing. `densify` keeps routed geometry under
    `DEFAULT_MAX_SPACING_M`, so the error is bounded and well under `SAME_LINE_M`; a
    hand-drawn GPX with kilometre spacing would need the line version.
    """
    return min(haversine_m(lat, lon, point.lat, point.lon) for point in other.points)


__all__ = ["SAME_LINE_M", "DiffSpan", "RouteDiff", "route_diff"]
