"""What changed between two routes, and between two scorings (scope 7.8).

Named in three docstrings since M1 and written nowhere until M5.7. Scope 7.8 says it is
"used for trade-off presentation and iteration review", which is what it is shaped for:
the question a reader has is *where* two routes disagree and by how much, not whether they
are byte-identical.

Distances are measured along each route rather than between corresponding points, because
the two point lists are not corresponding - a reroute changes the count, and a matched
route changes it again.

**`result_diff` is the other half, and it is the one a refresh needs.** `core/plan`'s own
module docstring has described this file as *"route_diff with per-difference score deltas"*
since M1; only the geometry half was ever built. A refresh does not re-route - it passes no
router, so the line is identical by construction - which makes `route_diff` on one provably
vacuous: it returns "same line; +0 m" every time, after comparing every point of one route
against every point of the other. Worse than useless, because a reader who sees "same line"
beside a refresh may conclude nothing changed when the trail has just closed. What a refresh
changes is *time*, so what it reports has to be measured over flags, not over geometry.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from longrun.core.geo.gpx import haversine_m
from longrun.core.models.geometry import Route
from longrun.core.models.measurement import FlagKind, ScorerResult

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


@dataclass(frozen=True)
class ScorerDelta:
    """One scorer's flags, before and after a re-scoring."""

    name: str
    #: Whether the "after" result was carried rather than measured. A carried scorer cannot
    #: have moved, and saying so is the point: "no change" and "not re-checked" are
    #: different claims, and a refresh that rendered them identically would be the failure
    #: `carried_from` exists to prevent.
    carried: bool = False
    hard_before: int = 0
    hard_after: int = 0
    soft_before: int = 0
    soft_after: int = 0
    #: Reason codes flagged after and not before, and vice versa.
    appeared: tuple[str, ...] = ()
    resolved: tuple[str, ...] = ()

    @property
    def moved(self) -> bool:
        return bool(self.appeared or self.resolved) or (
            self.hard_before,
            self.soft_before,
        ) != (self.hard_after, self.soft_after)

    def line(self) -> str:
        parts: list[str] = []
        if self.hard_after != self.hard_before:
            parts.append(f"{self.hard_after - self.hard_before:+d} hard")
        if self.soft_after != self.soft_before:
            parts.append(f"{self.soft_after - self.soft_before:+d} soft")
        if self.appeared:
            parts.append(f"new: {', '.join(self.appeared)}")
        if self.resolved:
            parts.append(f"gone: {', '.join(self.resolved)}")
        return f"{self.name}: {'; '.join(parts) if parts else 'unchanged'}"


@dataclass(frozen=True)
class ResultDiff:
    """Two scorings of the same route, compared."""

    scorers: tuple[ScorerDelta, ...] = ()

    @property
    def moved(self) -> tuple[ScorerDelta, ...]:
        return tuple(delta for delta in self.scorers if delta.moved)

    def summary(self) -> str:
        moved = self.moved
        if not moved:
            return "no scorer changed its flags"
        return f"{len(moved)} scorer(s) changed: " + "; ".join(d.line() for d in moved)


def result_diff(before: Sequence[ScorerResult], after: Sequence[ScorerResult]) -> ResultDiff:
    """What two scorings of one route disagree about.

    Keyed on `reason_code` alone rather than on `(reason_code, segment_id)`. Segment ids are
    stable across a refresh in practice - segmentation is deterministic from the route and
    its way ids, and a refresh does not move the route - but they *shift* if the ways layer
    was reloaded underneath, which is exactly the case where you least want the report to
    dissolve into a list of every segment renumbering. The code says what changed; the flags
    themselves carry the segment.
    """
    older = {result.name: result for result in before}
    newer = {result.name: result for result in after}
    # `after`'s order first, because it is the order the plan now holds; anything only the
    # older scoring had is appended rather than dropped, since a scorer that has stopped
    # reporting is itself a change worth seeing.
    names = list(newer) + [name for name in older if name not in newer]

    deltas: list[ScorerDelta] = []
    for name in names:
        was, now = older.get(name), newer.get(name)
        codes_before = {f.reason_code for f in was.flags} if was else set()
        codes_after = {f.reason_code for f in now.flags} if now else set()
        deltas.append(
            ScorerDelta(
                name=name,
                carried=bool(now and now.carried),
                hard_before=_count(was, FlagKind.HARD),
                hard_after=_count(now, FlagKind.HARD),
                soft_before=_count(was, FlagKind.SOFT),
                soft_after=_count(now, FlagKind.SOFT),
                appeared=tuple(sorted(codes_after - codes_before)),
                resolved=tuple(sorted(codes_before - codes_after)),
            )
        )
    return ResultDiff(scorers=tuple(deltas))


def _count(result: ScorerResult | None, kind: FlagKind) -> int:
    return sum(1 for flag in result.flags if flag.kind is kind) if result else 0


__all__ = [
    "SAME_LINE_M",
    "DiffSpan",
    "ResultDiff",
    "RouteDiff",
    "ScorerDelta",
    "result_diff",
    "route_diff",
]
