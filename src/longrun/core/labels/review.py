"""What the Review page may show of a task, before and after it is answered (ADR 0049).

Pure, and the only place that decides what leaves the ledger for a browser. Two rules live
here rather than in a component, because a component that forgot them would still render:

* **Blind.** Never which vector drew a line - `Candidate.drawn_by` does not leave the ledger.
* **Numbers after.** The shares of LTS 3+, unpaved and path are in the view only once the
  task has an answer. Before, a person sees the two lines, their lengths, and where they part.
"""

from __future__ import annotations

from typing import Any

from longrun.core.geo.gpx import haversine_m
from longrun.core.labels.pairs import Candidate, PairAnswer, PairTask
from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.plan.diff import route_diff


def route_of(candidate: Candidate) -> Route:
    """A candidate's line as a `Route`, cumulative distance recomputed along its points."""
    points: list[RoutePoint] = []
    total = 0.0
    for index, (lat, lon) in enumerate(candidate.points):
        if index:
            previous = candidate.points[index - 1]
            total += haversine_m(previous[0], previous[1], lat, lon)
        points.append(RoutePoint(lat=lat, lon=lon, cum_dist_m=total))
    return Route(id=candidate.id, points=points)


def _diverges(line: Route, other: Route) -> list[list[tuple[float, float]]]:
    """The stretches of `line` that `other` does not run, as point lists to draw."""
    out = []
    for span in route_diff(line, other).spans:
        stretch = [
            (p.lat, p.lon) for p in line.points if span.start_m <= p.cum_dist_m <= span.end_m
        ]
        if len(stretch) >= 2:
            out.append(stretch)
    return out


def task_view(task: PairTask, answer: PairAnswer | None) -> dict[str, Any]:
    """The task as the Review page may see it."""
    a, b = route_of(task.a), route_of(task.b)

    def line(candidate: Candidate, route: Route, other: Route) -> dict[str, Any]:
        return {
            "points": [list(point) for point in candidate.points],
            "length_m": candidate.length_m,
            "diverges": [[list(point) for point in stretch] for stretch in _diverges(route, other)],
        }

    return {
        "id": task.id,
        "region": task.region,
        "probe": task.repeat_of is not None,
        "a": line(task.a, a, b),
        "b": line(task.b, b, a),
        "answer": None
        if answer is None
        else {
            "prefer": answer.prefer,
            "strength": answer.strength,
            "reason": answer.reason,
            "labelled_at": answer.labelled_at.isoformat(),
            "via": answer.via,
        },
        "shares": None if answer is None else {"a": task.a.shares(), "b": task.b.shares()},
    }


__all__ = ["route_of", "task_view"]
