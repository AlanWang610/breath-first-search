"""What the same route looks like if you leave at a different hour (scope 7.7).

Every other scorer answers "how bad is this route at the time you gave me". This one asks
the question a runner actually asks the night before — *when should I set off* — and it is
the only place in the plan where the answer is a table rather than a number.

Scope §7.7: *"sweep start times across the window; re-run time-dependent scorers; return a
table of heat, sun, daylight, service-hours, and transit outcomes"*. All five are here, and
each is read from the source the scorer that owns it reads:

| column | from | owned by |
|---|---|---|
| peak WBGT | forecast temperature and humidity | `heat` |
| sunlit fraction, mean irradiance | the corridor horizons and solar geometry | `sun_exposure` |
| minutes in darkness | solar elevation against civil twilight | `lighting` |
| water points open on arrival | `opening_hours` at the shifted ETA | `resupply_schedule` |
| finish served by transit | the stop's service span for that day | `transit` |

**The sweep is nearly free, and that is a design property rather than luck.** ADR 0003
chose a full horizon *profile* over a single ray partly because the profile is reusable
across start times: the skyline at a point does not change when you leave an hour later,
only the sun's place in it does. So `corridor_horizons` runs **once** and every candidate
start reuses it — which is what makes twelve candidates cost about what one does, and why
risk R5 named this scorer as the one that could have broken the ~3-minute budget.

**The window is the request's when the request gave one, and a fixed span either side
of the requested start when it did not** (M11.6, ADR 0033). A scorer receives
`(route, segments, ctx, etas)` and never the `PlanRequest`, so §6.1's `start_window`
reaches it on `ScorerContext` — the same route `utc_offset_hours` takes, and for the same
reason: it is a fact about the request that one scorer needs, and widening the scorer
contract would hand all twenty-one of them the request so that one could read one field.

The two cases are different questions and are kept apart. With no window the sweep asks
*"would earlier be better, and by how much"* around the hour the runner named. With one it
asks *"when, within the hours I can actually leave"*, and the answer must not reach outside
them — a recommendation to start at 05:30 is worth nothing to somebody who said they cannot
leave before seven.

Nothing here flags. The optimizer measures alternatives; choosing among them is
arbitration's business and, ultimately, the runner's.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np

from longrun.core.data.file_store import LayerNotFound
from longrun.core.data.forecast import route_forecast
from longrun.core.geo.raycast import is_sunlit
from longrun.core.geo.segments import corridor
from longrun.core.geo.solar import CIVIL_TWILIGHT_DEG, clear_sky, solar_positions, utc_offset_for
from longrun.core.geo.svf import corridor_horizons
from longrun.core.models.coverage import CoverageEntry
from longrun.core.models.geometry import Route, Segment
from longrun.core.models.measurement import ScorerResult, SegmentMeasurement
from longrun.core.models.request import TimeWindow
from longrun.core.scorers._common import ROUTE_SUMMARY_ID
from longrun.core.scorers.base import record_coverage, unavailable
from longrun.core.scorers.heat import wbgt_c
from longrun.core.scorers.resupply_schedule import is_open
from longrun.core.scorers.services import DEFAULT_BUFFER_M, all_kinds, category_of
from longrun.core.scorers.transit import STOPS_LAYER, in_service

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext

name = "start_time_optimizer"

#: How far either side of the requested start to look, and in what steps.
#:
#: Three hours each way at half-hour resolution is thirteen candidates. Wide enough that a
#: summer afternoon start can find a morning one; fine enough that the answer is actionable
#: — "an hour earlier" is advice, "somewhere between dawn and noon" is not.
WINDOW_HOURS = 3.0
STEP_MINUTES = 30.0

#: Prefix for a candidate's measurement id. Not an `s00007`-shaped segment id, and visibly
#: not one: these rows describe the whole route at an hour, not a piece of it at any hour.
CANDIDATE_PREFIX = "start@"

#: Seconds of sun below `CIVIL_TWILIGHT_DEG` that count as running in the dark.
DARK_ELEVATION_RAD = np.radians(CIVIL_TWILIGHT_DEG)


def candidate_starts(
    start: datetime,
    window_hours: float = WINDOW_HOURS,
    step_minutes: float = STEP_MINUTES,
    window: TimeWindow | None = None,
) -> list[datetime]:
    """Start times to evaluate: the request's window if it gave one, else a span either side.

    Kept on the same calendar day: a forecast is fetched per day, and a candidate that
    crossed midnight would silently compare one day's weather against another's.

    A supplied window is swept from `earliest` to `latest` inclusive, at the same half-hour
    resolution — the resolution argument is about what makes an answer actionable and does
    not change with the width of the window. `latest` is always a candidate even when it
    does not land on a step, because it is a time the runner named and the sweep would
    otherwise quietly stop short of it.

    The requested start is **not** added to a supplied window. `PlanRequest` refuses a start
    time and a window together, so there is no requested start to add; the anchor the ETAs
    were built from is whatever the caller chose to stand in for one.
    """
    if step_minutes <= 0:
        return [start]
    if window is not None:
        return _across(start, window, step_minutes)
    if window_hours <= 0:
        return [start]
    steps = int(round(window_hours * 60.0 / step_minutes))
    out = []
    for index in range(-steps, steps + 1):
        candidate = start + timedelta(minutes=index * step_minutes)
        if candidate.date() == start.date():
            out.append(candidate)
    return out


def _across(start: datetime, window: TimeWindow, step_minutes: float) -> list[datetime]:
    """Every step from `window.earliest` to `window.latest`, on `start`'s own day."""
    day = start.date()
    first = datetime.combine(day, window.earliest)
    last = datetime.combine(day, window.latest)
    out: list[datetime] = []
    candidate = first
    while candidate < last:
        out.append(candidate)
        candidate += timedelta(minutes=step_minutes)
    out.append(last)
    return out


def _shift(etas: list[datetime], to_start: datetime) -> list[datetime]:
    """The same pacing, started at a different time.

    The offsets between ETAs are the pacing model's output and do not change with the hour
    — a runner does not get faster by starting at six. Only the wall clock moves.
    """
    origin = etas[0]
    return [to_start + (eta - origin) for eta in etas]


def start_time_optimizer(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
) -> ScorerResult:
    """A table of what the route becomes at each candidate start time (scope 7.7)."""
    if not etas or len(etas) != len(route.points):
        return unavailable(name, "no ETA vector: a start-time sweep needs a pacing model")

    horizons = corridor_horizons(route, ctx)
    if not horizons.coverage.answered:
        return unavailable(
            name, f"no surface model: {horizons.coverage.describe()}", kind="surface_model"
        )

    lat, lon = route.points[0].lat, route.points[0].lon
    offset, how = utc_offset_for(lat, lon, etas[0], ctx.utc_offset_hours)
    day = etas[0].date()
    forecast = route_forecast(route, ctx, day)
    water = _water_points(route, ctx)
    stops = _stops(route, ctx)

    result = ScorerResult(name=name)
    rows: list[dict[str, Any]] = []
    for start in candidate_starts(etas[0], window=ctx.start_window):
        rows.append(_evaluate(route, _shift(etas, start), horizons, offset, forecast, water, stops))

    # One measurement per candidate rather than a table nested inside one. `values` holds
    # scalars by design - that is what keeps the plan sheet renderable and the golden
    # content hash meaningful - and a row of the table is exactly a measurement anyway.
    # The id follows the precedent `ROUTE_SUMMARY_ID` already set: a `segment_id` that
    # names something other than a segment, distinguishable at a glance from `s00007`.
    for row in rows:
        result.measurements.append(
            SegmentMeasurement(
                segment_id=f"{CANDIDATE_PREFIX}{row['start']}",
                values=dict(row),
                confidence=1.0,
            )
        )

    best = _best(rows)
    result.measurements.append(
        SegmentMeasurement(
            segment_id=ROUTE_SUMMARY_ID,
            values=_summary_values(rows, etas[0], ctx.start_window, best),
            confidence=1.0,
        )
    )

    for entry in horizons.coverage.entries():
        result.coverage.append(entry)
    record_coverage(result, source="pvlib", kind="start_time_sweep", confidence=1.0)
    if not forecast.answered:
        result.coverage.append(
            CoverageEntry(
                source="forecast",
                kind="start_time_heat",
                checked=False,
                reason="no forecast: the sweep compares sun and daylight only",
            )
        )
    if water is None:
        result.coverage.append(
            CoverageEntry(
                source="amenities",
                kind="start_time_services",
                checked=False,
                reason="no amenities layer: opening hours not compared across start times",
            )
        )
    if stops is None:
        result.coverage.append(
            CoverageEntry(
                source=STOPS_LAYER,
                kind="start_time_transit",
                checked=False,
                reason="no transit stops layer: the finish is not compared across start times",
            )
        )
    result.coverage.append(
        CoverageEntry(
            source="pvlib",
            kind="solar_geometry",
            checked=True,
            reason=f"UTC offset {offset:+.0f} h, {how}",
            confidence=0.7 if how.startswith("derived") else 1.0,
        )
    )
    return result


def _summary_values(
    rows: list[dict[str, Any]],
    requested: datetime,
    window: TimeWindow | None,
    best: dict[str, str],
) -> dict[str, Any]:
    """The one row describing the sweep itself, in two shapes that read differently.

    **A plan with no window produces exactly the keys it produced before M11.6**, and that
    is a constraint rather than a preference: `expectation.measurements_hash` walks every
    key of every measurement, so a key added unconditionally moves the content hash of every
    golden route on a milestone that measured nothing new. It did, and the golden suite
    caught it — all six routes, on three keys whose values were `null`.

    So the window keys appear only when there *is* a window, following the rule
    `expectation._summary` already states for waypoint kinds: *"kinds with no finds are
    omitted rather than written as zero — 'no key' and 'zero' should read differently"*.
    `window_hours` present means the sweep used its own span around the requested start;
    `window_earliest` and `window_latest` present mean the runner named the hours and these
    were them. Neither shape is silent about which sweep ran, and a sheet printing
    `window_hours: 3.0` beside a 05:00–09:00 request is what this prevents.
    """
    values: dict[str, Any] = {
        "candidates": len(rows),
        "step_minutes": STEP_MINUTES,
        "requested_start": requested.strftime("%H:%M"),
        "coolest_start": best.get("coolest"),
        "shadiest_start": best.get("shadiest"),
    }
    if window is None:
        values["window_hours"] = WINDOW_HOURS
    else:
        values["window_from"] = "request"
        values["window_earliest"] = window.earliest.strftime("%H:%M")
        values["window_latest"] = window.latest.strftime("%H:%M")
    return values


def _evaluate(
    route: Route,
    etas: list[datetime],
    horizons: Any,
    offset: float,
    forecast: Any,
    water: list[tuple[float, str | None]] | None,
    stops: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """One row of the table. Everything here is a lookup against work already done."""
    lat, lon = route.points[0].lat, route.points[0].lon
    position = solar_positions(etas, lat, lon, offset)
    sky = clear_sky(etas, lat, lon, offset)
    lit = is_sunlit(horizons.horizons, position.azimuth, position.elevation)

    dark = position.elevation < DARK_ELEVATION_RAD
    minutes = (etas[-1] - etas[0]).total_seconds() / 60.0
    ghi = np.asarray(sky.ghi, dtype=float)

    peak_wbgt: float | None = None
    for index, when in enumerate(etas):
        reading = forecast.at_distance(route.points[index].cum_dist_m, when)
        if reading is None or reading.temp_c is None or reading.relative_humidity_pct is None:
            continue
        value = wbgt_c(reading.temp_c, reading.relative_humidity_pct)
        peak_wbgt = value if peak_wbgt is None else max(peak_wbgt, value)

    open_now = None
    if water is not None:
        # Arrival at each water point, not at the start: a shop that shuts at 18:00 is open
        # for a runner who reaches it at 17:40 and shut for one who reaches it at 18:20.
        open_now = sum(
            1 for cum_m, hours in water if is_open(hours, _eta_at(route, etas, cum_m)) is not False
        )

    finish_served = None
    if stops is not None:
        finish_served = any(in_service(stop, etas[-1]) is True for stop in stops)

    return {
        "start": etas[0].strftime("%H:%M"),
        "finish": etas[-1].strftime("%H:%M"),
        "sunlit_fraction": round(float(np.mean(lit)), 3),
        "mean_clear_sky_w_m2": round(float(np.mean(ghi)), 1),
        "dark_minutes": round(float(np.mean(dark)) * minutes, 1),
        "peak_wbgt_c": None if peak_wbgt is None else round(peak_wbgt, 2),
        "water_open": open_now,
        "finish_served": finish_served,
    }


def _eta_at(route: Route, etas: list[datetime], cum_m: float) -> datetime:
    """The ETA at a distance along the route, to the nearest sampled point."""
    best, arrival = None, etas[0]
    for index, point in enumerate(route.points):
        gap = abs(point.cum_dist_m - cum_m)
        if best is None or gap < best:
            best, arrival = gap, etas[min(index, len(etas) - 1)]
    return arrival


def _best(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Which candidate wins on each axis, or nothing when the axis has no data.

    Reported rather than ranked: scope 3.2 keeps the sign out of a scorer, and "coolest"
    and "shadiest" routinely disagree. Choosing between them is arbitration's job.
    """
    out: dict[str, str] = {}
    warm = [r for r in rows if r["peak_wbgt_c"] is not None]
    if warm:
        out["coolest"] = min(warm, key=lambda r: r["peak_wbgt_c"])["start"]
    if rows:
        out["shadiest"] = min(rows, key=lambda r: r["sunlit_fraction"])["start"]
    return out


def _water_points(route: Route, ctx: ScorerContext) -> list[tuple[float, str | None]] | None:
    """Water and food points along the route as (distance, opening hours)."""
    from longrun.core.scorers._common import RouteFrame

    try:
        frame = ctx.layers.points_in_corridor(
            corridor(route, buffer_m=DEFAULT_BUFFER_M), all_kinds()
        )
    except (LayerNotFound, FileNotFoundError):
        return None

    positions = RouteFrame(route)
    out: list[tuple[float, str | None]] = []
    for _, row in frame.iterrows():
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        tags = {k: v for k, v in row.items() if k != "geometry"}
        if category_of(tags) is None:
            continue
        cum_m, offset_m = positions.locate(geometry.x, geometry.y)
        if offset_m > DEFAULT_BUFFER_M:
            continue
        hours = tags.get("opening_hours")
        out.append((cum_m, None if hours is None else str(hours)))
    return out


def _stops(route: Route, ctx: ScorerContext) -> list[dict[str, Any]] | None:
    from longrun.core.scorers.transit import SEARCH_BUFFER_M

    try:
        frame = ctx.layers.points_in_corridor(
            corridor(route, buffer_m=SEARCH_BUFFER_M), [], layer=STOPS_LAYER
        )
    except (LayerNotFound, FileNotFoundError):
        return None
    return [
        {k: v for k, v in row.items() if k != frame.geometry.name} for _, row in frame.iterrows()
    ]


score = start_time_optimizer

__all__ = [
    "CANDIDATE_PREFIX",
    "STEP_MINUTES",
    "WINDOW_HOURS",
    "candidate_starts",
    "name",
    "score",
    "start_time_optimizer",
]
