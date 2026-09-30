"""Turning a pair of ends into questions worth a person's time (ADR 0049).

One request - two points where somebody might run between - is drawn under every vector in
`SEED_VECTORS`, plus the router's own alternatives to the neutral line. Lines that are the
same route, or the same mixture of way classes, are dropped; of the pairs that remain, the
ones the six-parameter grid is most split on are kept, one per stratum, a couple per request
(two questions about the same two ends are correlated evidence, not twice the evidence).

The router is injected and nothing here reads a clock or draws a random number: the caller
supplies ids and the time, which is what lets a test pin exactly which pairs a set of lines
becomes.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from longrun.core.geo.gpx import haversine_m
from longrun.core.labels.pairs import (
    MIN_CLASS_DIFFERENCE_M,
    Candidate,
    PairTask,
    class_difference_m,
    distinct,
    split_fraction,
    stratum,
)
from longrun.core.models.geometry import LatLon, Route
from longrun.core.plan.diff import SAME_LINE_M
from longrun.core.routing.base import Router, RoutingError
from longrun.core.routing.custom_model import (
    AVOID_HIGH_STRESS,
    NEUTRAL,
    CustomModelParams,
    to_custom_model,
)
from longrun.core.routing.tuning import RouteSummary

#: The vectors a request is drawn under: the shipped one, the one documented flag, and a
#: seek and a shun for the two parameters a runner's ground varies most on. docs/tuning.md
#: measured the LTS terms moving a city route by under 1%, so where every street is calm
#: the pairs come from surface and paths - which is what Marin's lines showed in M23 (path
#: share 28% neutral, 92% seeking paths).
SEED_VECTORS: dict[str, CustomModelParams] = {
    "neutral": NEUTRAL,
    "avoid-stress": AVOID_HIGH_STRESS,
    "seek-paths": CustomModelParams(path_bonus=4.0),
    "shun-paths": CustomModelParams(path_bonus=0.25),
    "seek-unpaved": CustomModelParams(unpaved=4.0),
    "shun-unpaved": CustomModelParams(unpaved=0.25),
}

#: The router's own alternatives to the neutral line, as a total including the primary.
NEUTRAL_ALTERNATIVES = 3

#: How far the router may move an end before the question is about somewhere else.
#:
#: Generous on purpose. Both lines of a pair start where the router snapped, so a snap does
#: not make a question wrong - it makes it about the nearest trail rather than the sampled
#: point, and the task records the snapped ends. What this refuses is a point in the bay
#: that snapped to another shore. 150 m, the first value, threw away five of eight
#: sampled Marin requests whose points had landed on open hillside.
MAX_SNAP_M = 500.0

#: Questions per request. More than this from one pair of ends is one answer counted twice.
PER_REQUEST = 2


@dataclass
class Seeded:
    """What one request became, and enough to say why when it became nothing."""

    tasks: list[PairTask] = field(default_factory=list)
    lines: int = 0
    distinct_lines: int = 0
    notes: list[str] = field(default_factory=list)


def shared_fraction(a: Route, b: Route, tolerance_m: float = SAME_LINE_M) -> float:
    """How much of `a` runs along `b`, 0 to 1 - `plan.diff.route_diff`'s measure, in numpy.

    `route_diff` is point-to-point over every pair of points, which is right for comparing
    one edit and minutes of work for comparing eight lines per request. Equirectangular
    metres about the first point are exact enough at a runner's scale; the tolerance is
    25 m.
    """
    if not a.points or not b.points:
        return 0.0
    lat0 = np.radians(a.points[0].lat)
    scale = np.array([111_320.0 * np.cos(lat0), 110_540.0])
    pa = np.array([(p.lon, p.lat) for p in a.points]) * scale
    pb = np.array([(p.lon, p.lat) for p in b.points]) * scale
    shared = 0
    for start in range(0, len(pa), 512):
        chunk = pa[start : start + 512]
        nearest = np.sqrt(((chunk[:, None, :] - pb[None, :, :]) ** 2).sum(axis=2)).min(axis=1)
        shared += int((nearest <= tolerance_m).sum())
    return shared / len(pa)


def _snapped_m(ends: tuple[LatLon, LatLon], route: Route) -> float:
    first, last = route.points[0], route.points[-1]
    return max(
        haversine_m(ends[0].lat, ends[0].lon, first.lat, first.lon),
        haversine_m(ends[1].lat, ends[1].lon, last.lat, last.lon),
    )


def seed_request(
    router: Router,
    ends: tuple[LatLon, LatLon],
    *,
    region: str | None,
    new_id: Callable[[], str],
    created_at: datetime,
    has_lts: bool = True,
    per_request: int = PER_REQUEST,
) -> Seeded:
    """The questions one pair of ends is worth asking, possibly none."""
    out = Seeded()
    lines: list[tuple[str, Route, RouteSummary]] = []
    for name, params in SEED_VECTORS.items():
        try:
            drawn = router.route_summaries(
                list(ends),
                custom_model=to_custom_model(params, has_lts=has_lts),
                alternatives=NEUTRAL_ALTERNATIVES if name == "neutral" else 0,
                has_lts=has_lts,
                name=name,
            )
        except RoutingError as exc:
            out.notes.append(f"{name}: {exc}")
            continue
        lines.extend((summary.name, route, summary) for route, summary in drawn)
    out.lines = len(lines)
    if not lines:
        return out

    snap = _snapped_m(ends, lines[0][1])
    start, end = lines[0][1].points[0], lines[0][1].points[-1]
    snapped_ends = (
        (round(start.lat, 6), round(start.lon, 6)),
        (round(end.lat, 6), round(end.lon, 6)),
    )
    if snap > MAX_SNAP_M:
        out.notes.append(f"the router moved an end {snap:.0f} m; the question would be elsewhere")
        return out

    kept: list[tuple[str, Route, RouteSummary]] = []
    for line in lines:
        if all(_distinct(line, other) for other in kept):
            kept.append(line)
    out.distinct_lines = len(kept)

    options = []
    for x, y in itertools.combinations(kept, 2):
        split = split_fraction(x[2], y[2])
        options.append((abs(split - 0.5), -class_difference_m(x[2], y[2]), x, y))
    options.sort(key=lambda option: (option[0], option[1], option[2][0], option[3][0]))

    used_strata: set[str] = set()
    for _, _, x, y in options:
        if len(out.tasks) >= per_request:
            break
        kind = stratum(x[2], y[2])
        if kind in used_strata:
            continue
        used_strata.add(kind)
        first, second = (
            Candidate.of(new_id(), name, route, summary) for name, route, summary in (x, y)
        )
        # A and B by candidate id, which is random: never by the vector that drew a line,
        # or "neutral" would always be A and the question would stop being blind.
        a, b = sorted((first, second), key=lambda c: c.id)
        out.tasks.append(
            PairTask(
                id=new_id(),
                region=region,
                ends=snapped_ends,
                a=a,
                b=b,
                stratum=kind,
                split=round(split_fraction(a.summary(), b.summary()), 4),
                created_at=created_at,
            )
        )
    if not out.tasks:
        out.notes.append(
            f"{out.lines} line(s), {out.distinct_lines} distinct: nothing worth asking here"
        )
    return out


def _distinct(x: tuple[str, Route, RouteSummary], y: tuple[str, Route, RouteSummary]) -> bool:
    # The class check first: it is arithmetic on two dicts, and the shared fraction is not.
    # Two lines with the same mixture of classes are the same pair to the fitter, wherever
    # they go, so most near-duplicates never reach the geometry at all.
    if class_difference_m(x[2], y[2]) < MIN_CLASS_DIFFERENCE_M:
        return False
    # The smaller of the two directions: a line that is the other plus a detour runs all of
    # the other (1.0 one way) and is still a different route to choose.
    shared = min(shared_fraction(x[1], y[1]), shared_fraction(y[1], x[1]))
    return distinct(shared, x[2], y[2])


__all__ = [
    "MAX_SNAP_M",
    "NEUTRAL_ALTERNATIVES",
    "PER_REQUEST",
    "SEED_VECTORS",
    "Seeded",
    "seed_request",
    "shared_fraction",
]
