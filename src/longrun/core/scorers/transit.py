"""Getting to the start and home from the finish (scope 7.7).

The narrower half of the transit question, and the half a runner asks first. `bailouts`
asks whether you can leave a route *in the middle*; this asks whether the two ends work at
all — a 60 km point-to-point that finishes at a station whose last train left an hour ago
is a logistics failure the plan should have named before anyone set off.

Measures three things and decides none of them:

* whether the start is served by anything, and by what
* whether the finish is served **at the projected finish time**, which is where the
  service-span data earns its place
* how far each end is from its nearest stop

The flags are soft and COMFORT, following ADR 0011's reasoning: a run you have to arrange
a lift home from is a logistics problem, not a safety one, and lexicographic tiers mean
anything in SAFETY outranks every heat and traffic consideration on the route.

The service test is what `core.data.gtfs` precomputed for. Spans are stored per day type
because one combined span over-claims — a station running 05:00–24:00 on weekdays and
08:00–22:00 on Sunday would otherwise report a Sunday 06:00 finish as served.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from longrun.core.data.file_store import LayerNotFound
from longrun.core.geo.segments import corridor
from longrun.core.models.coverage import CoverageEntry
from longrun.core.models.geometry import Route, Segment
from longrun.core.models.measurement import (
    Flag,
    FlagKind,
    ScorerResult,
    SegmentMeasurement,
    Tier,
)
from longrun.core.scorers._common import ROUTE_SUMMARY_ID
from longrun.core.scorers.base import record_coverage, unavailable

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext

name = "transit"

STOPS_LAYER = "transit_stops"

#: How far from an endpoint a stop still counts as serving it. A kilometre is twelve
#: minutes' walk at the end of a long run, which is a different thing from twelve minutes'
#: walk at the start of one — hence one threshold and an honest distance reported beside it.
ENDPOINT_REACH_M = 1000.0

#: How wide to search for endpoint stops. Wider than the reach so the *nearest* stop can be
#: reported with its true distance even when nothing is within reach: "the closest station
#: is 2.4 km away" is a useful answer and "none found" is not.
SEARCH_BUFFER_M = 5000.0

SEVERITY_BY_CODE: dict[str, float] = {
    "finish_unserved": 0.7,
    "finish_after_last_departure": 0.8,
    "start_unserved": 0.3,
}

#: Day-type column prefix per `datetime.weekday()`.
DAY_TYPES: tuple[str, ...] = (
    "weekday",
    "weekday",
    "weekday",
    "weekday",
    "weekday",
    "saturday",
    "sunday",
)


def day_type(when: datetime) -> str:
    return DAY_TYPES[when.weekday()]


def seconds_of_day(when: datetime) -> int:
    return when.hour * 3600 + when.minute * 60 + when.second


def in_service(stop: dict[str, Any], when: datetime) -> bool | None:
    """Whether a stop is served at an instant, or None when the feed does not say.

    Three states, and the third is load-bearing: a stop loaded from a feed with no
    `calendar.txt` has an unreadable service pattern, and reporting that as "not served"
    would tell a runner to arrange a lift they may not need.
    """
    prefix = day_type(when)
    departures = stop.get(f"{prefix}_departures")
    first, last = stop.get(f"{prefix}_first_s"), stop.get(f"{prefix}_last_s")
    if departures is None:
        return None
    # Zero departures is checked *before* the span, and the order is the whole point: a
    # stop with no Sunday service has no Sunday span either, so testing the span first
    # turns a definite "not served on a Sunday" into "we cannot say". That reads on the
    # sheet as a station worth walking to.
    if int(departures) <= 0:
        return False
    if first is None or last is None:
        return None
    # Compared against the *service* day, so a 25:10 last departure is still reachable at
    # 01:10 by a runner finishing after midnight on the same service day.
    now = seconds_of_day(when)
    return int(first) <= now <= int(last) or int(first) <= now + 86_400 <= int(last)


def _stops(route: Route, ctx: ScorerContext) -> Any | None:
    try:
        return ctx.layers.points_in_corridor(
            corridor(route, buffer_m=SEARCH_BUFFER_M), [], layer=STOPS_LAYER
        )
    except (LayerNotFound, FileNotFoundError):
        return None


def nearest_stop(frame: Any, lat: float, lon: float, route: Route) -> tuple[dict, float] | None:
    """The closest stop to a point, with its distance in metres, or None if there are none.

    Distances go through the route's local metric CRS, never degrees — scope's units rule,
    and at this latitude a degree of longitude is 79% of a degree of latitude.
    """
    from shapely.geometry import Point

    from longrun.core.geo.projections import local_crs, transformer_to

    if frame is None or len(frame) == 0:
        return None

    to_local = transformer_to(local_crs(route))
    here = Point(*to_local.transform(lon, lat))

    best: tuple[dict, float] | None = None
    for _, row in frame.iterrows():
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        there = Point(*to_local.transform(geometry.x, geometry.y))
        distance = here.distance(there)
        if best is None or distance < best[1]:
            best = (dict(row.drop(labels=[frame.geometry.name])), distance)
    return best


def _describe(stop: dict[str, Any]) -> str:
    routes = str(stop.get("routes") or "").strip()
    modes = str(stop.get("modes") or "").strip()
    label = str(stop.get("name") or stop.get("stop_id") or "a stop")
    detail = routes or modes
    return f"{label} ({detail})" if detail else label


def transit(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
) -> ScorerResult:
    """Whether the two ends of the route are reachable by transit (scope 7.7)."""
    frame = _stops(route, ctx)
    if frame is None:
        return unavailable(name, "no transit stops layer: getting to and from not established")

    result = ScorerResult(name=name)

    start, finish = route.points[0], route.points[-1]
    start_at = etas[0] if etas else ctx.clock.now()
    finish_at = etas[-1] if etas else start_at

    values: dict[str, Any] = {"stops_in_corridor": int(len(frame))}
    for label, point, when in (("start", start, start_at), ("finish", finish, finish_at)):
        found = nearest_stop(frame, point.lat, point.lon, route)
        if found is None:
            values[f"{label}_stop_m"] = None
            values[f"{label}_served"] = False
            continue
        stop, distance = found
        served = in_service(stop, when)
        within = distance <= ENDPOINT_REACH_M
        values[f"{label}_stop_m"] = round(distance, 1)
        values[f"{label}_stop"] = _describe(stop)
        values[f"{label}_served"] = None if served is None else (served and within)

        if label == "finish" and within and served is False:
            result.flags.append(
                _flag(
                    "finish_after_last_departure",
                    f"{_describe(stop)} is {distance:.0f} m from the finish but has no "
                    f"service at {when:%H:%M} on a {when:%A}",
                )
            )
        elif label == "finish" and not within:
            result.flags.append(
                _flag(
                    "finish_unserved",
                    f"nearest transit to the finish is {_describe(stop)}, "
                    f"{distance / 1000:.1f} km away",
                )
            )
        elif label == "start" and not within:
            result.flags.append(
                _flag(
                    "start_unserved",
                    f"nearest transit to the start is {_describe(stop)}, "
                    f"{distance / 1000:.1f} km away",
                )
            )

    # Whether the *feed* could answer, distinct from whether the answer was no.
    unknown = values.get("finish_served") is None and values.get("finish_stop_m") is not None
    result.measurements.append(
        SegmentMeasurement(
            segment_id=ROUTE_SUMMARY_ID,
            values=values,
            confidence=0.5 if unknown else 1.0,
        )
    )
    if unknown:
        result.coverage.append(
            CoverageEntry(
                source=STOPS_LAYER,
                kind="transit_service",
                checked=False,
                reason="stops found but the feed carried no calendar; service not established",
            )
        )
    else:
        record_coverage(
            result,
            source=STOPS_LAYER,
            kind="transit_service",
            vintage=ctx.layers.vintage(STOPS_LAYER),
        )
    return result


def _flag(code: str, detail: str) -> Flag:
    return Flag(
        scorer=name,
        # A route-wide finding with no position: `arbitrate` gives an unrecognised segment
        # id the base position weight of 1.0, which is right for something at neither end
        # of scope 8.2's ramp.
        segment_id=ROUTE_SUMMARY_ID,
        kind=FlagKind.SOFT,
        tier=Tier.COMFORT,
        severity=SEVERITY_BY_CODE[code],
        reason_code=code,
        detail=detail,
    )


score = transit

__all__ = [
    "ENDPOINT_REACH_M",
    "SEARCH_BUFFER_M",
    "SEVERITY_BY_CODE",
    "STOPS_LAYER",
    "day_type",
    "in_service",
    "name",
    "nearest_stop",
    "score",
    "seconds_of_day",
    "transit",
]
