"""Tide predictions from NOAA CO-OPS (scope 7.6, 14).

Scope §7.6 names *"NOAA CO-OPS tides"* as the source behind `access_hours`'s tide conflicts,
and `core/export/attribution.py` has carried a `noaa_coops` licence row with **no consumer**
since M2. This is the consumer.

`forecast.py` is the structural precedent — a keyless time-series HTTP client with a
per-site coverage record — and four of its rules are copied here deliberately rather than
re-derived:

*Everything goes through `cache.fetch`, with a named tool and a stable args hash.* Two keys,
`noaa_coops.stations` and `noaa_coops.predictions`, never one `tides` key. A cassette
recorded for one is then still valid when the other changes.

*The budget is charged **inside** the producer.* A cache replay is not an external call, so
a golden route that replays a cassette spends nothing — which is what the golden budget
assertion checks.

*Nothing raises out to the scoring loop.* Every failure — no network, a cassette miss, an
error body, a station too far away — lands in `StationTides.reason` and from there in the
coverage manifest. A tide that could not be established is a thing a runner must be told,
and an exception five frames down tells them nothing.

*Coordinates are rounded before they reach the key*, at `COORD_PRECISION`, so a cassette
recorded on one machine replays on another.

**Two decisions are this module's own.**

**Times come back in the station's local clock (`time_zone=lst_ldt`), not in UTC.** A plan's
ETAs are naive local wall-clock — `ScorerContext.utc_offset_hours` exists precisely because
nothing in `core/` may assume otherwise — so a UTC series would have to be shifted by an
offset the context is allowed to leave `None`. Asking NOAA for local time removes the
conversion instead of getting it right, and the bug it prevents is already live elsewhere in
the tree: `parse_open_meteo` builds naive UTC stamps and `parse_nws_gridpoint` builds
tz-aware ones, and `SiteForecast.at` compares both against a naive local ETA. A station
within `STATION_REACH_M` of the route is in the route's own timezone; a route that straddles
a timezone boundary within 50 km of the coast would be wrong by an hour, and that is stated
here rather than discovered.

**`interval=hilo`, not a six-minute water level.** The question §7.6 asks is "is this stretch
passable at arrival", and answering it from a level would need the *path's* elevation in the
tide's own datum. The route's elevation is 3DEP in NAVD88 and the prediction is in MLLW;
converting between them is VDatum, a separate service with its own key-less-but-fragile API
and its own vertical uncertainty at the shoreline. So this returns the high and low waters
and `access_hours` tests proximity to high water, which needs no datum at all. What that
gives up is the case of a way that floods only on a spring tide, and `TideExtreme.height_m`
is carried — unused by this milestone — so that a later rule has the number it would need.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from longrun.core.data.cache import COORD_PRECISION, STATIC_DAY, CacheMiss, fetch
from longrun.core.models.context import BudgetExceeded
from longrun.core.models.coverage import CoverageEntry

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

    from longrun.core.models.context import ScorerContext

#: The source name in the coverage manifest and in `attribution.LICENCES`. Pre-registered
#: there since M2 with no consumer, which is why a new source here cannot trip
#: `test_golden_routes.py`'s `LICENCE NOT RECORDED` assertion - so that safety net does not
#: force the licence question and it has been checked by hand instead. The licence was
#: right - CO-OPS is a work of the US government - and the row carried no `attribution`, so
#: `SourceLicence.line()` would have fallen back to the source name and printed
#: `noaa_coops (US public domain)` on the first sheet that used a tide. Fixed there, in M16.
SOURCE = "noaa_coops"

#: Station metadata. `type=tidepredictions` is the subset that has a predicted tide at all —
#: asking for every CO-OPS station returns currents meters and water-level-only gauges that
#: `datagetter` then refuses, which reads downstream as "the nearest station returned no
#: predictions" for a station that was never going to have any.
NOAA_STATIONS_ROOT = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"

#: The data endpoint. `prod` is in the path rather than assumed: CO-OPS also serves a
#: `test` tree with the same shape and different numbers.
NOAA_DATAGETTER_ROOT = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

#: CO-OPS asks callers to identify themselves in an `application` parameter. Unlike NWS's
#: user agent this is not enforced and needs no contact address, so it is a constant rather
#: than an environment variable — there is no credential here and nothing to configure.
APPLICATION = "longrun-planner"

HTTP_TIMEOUT_S = 10.0

#: How far a station may be from a stretch and still govern it.
#:
#: **The number that does not exist elsewhere in this codebase, and the reason the gate seam
#: does not fit.** `access_hours.GATE_REACH_M` is 200 m because a gate is a point on the
#: ground the runner arrives at. A tide station is a *reference gauge*: the nearest one to a
#: given beach is routinely tens of kilometres away, and the tide it predicts is the tide at
#: that beach to within minutes, because the thing propagating is an ocean-scale wave and not
#: a local condition. 50 km is roughly the spacing of the US primary and secondary station
#: network on an open coast; past it the phase error inside an estuary grows faster than the
#: network thins, and the honest answer becomes "no station governs this stretch".
STATION_REACH_M = 50_000.0

#: How close to predicted high water counts as a conflict, either side.
#:
#: By the rule of twelfths a semidiurnal tide moves one twelfth of its range in the hour
#: adjacent to high water and two twelfths in the next, so at 90 minutes the water is still
#: within about a sixth of the range of its maximum. A way tagged as covered at high water is
#: therefore covered, or as good as, across this whole window. It is a judgement and it is the
#: least evidenced number in this module: it treats every tidal way as flooding at the same
#: point in the cycle, which is what having no path elevation in the tide's datum costs.
HIGH_WATER_WINDOW_MIN = 90.0

#: Days of predictions to ask for. One call covers a run that starts before midnight and
#: finishes after it, and a second day costs nothing on an endpoint that is charging for the
#: request rather than the rows.
PREDICTION_DAYS = 2

ExtremeKind = Literal["high", "low"]


class TideStation(BaseModel):
    """One CO-OPS gauge with a predicted tide."""

    model_config = {"frozen": True}

    id: str
    name: str
    lat: float
    lon: float


class TideExtreme(BaseModel):
    """One predicted high or low water, at the station's local clock time."""

    model_config = {"frozen": True}

    time: datetime
    height_m: float | None = None
    kind: ExtremeKind


class StationTides(BaseModel):
    """What one stretch's nearest station said, or why it said nothing.

    The three answers scope §3.6 distinguishes live in the combination of two fields.
    `station is None` with a `reason` is *not checked*. A station and an empty `extremes`
    with a `reason` is *checked and it could not answer*. A station and a populated
    `extremes` is *measured* — including when nothing on the route conflicts, which is the
    "measured as none" a reader must be able to tell from the other two.
    """

    station: TideStation | None = None
    distance_m: float | None = None
    extremes: list[TideExtreme] = Field(default_factory=list)
    reason: str | None = None

    @property
    def answered(self) -> bool:
        return bool(self.extremes)

    def high_water_conflict(
        self, when: datetime, window_min: float = HIGH_WATER_WINDOW_MIN
    ) -> TideExtreme | None:
        """The high water this arrival falls inside the window of, or None.

        Returns the *nearest* conflicting high water rather than the first, so the detail a
        flag carries names the water the runner is actually meeting.
        """
        window = timedelta(minutes=window_min)
        highs = [
            extreme
            for extreme in self.extremes
            if extreme.kind == "high" and abs(extreme.time - when) <= window
        ]
        if not highs:
            return None
        return min(highs, key=lambda e: abs(e.time - when))

    def minutes_to_next_low(self, when: datetime) -> float | None:
        """Minutes until the next predicted low water after `when`.

        What a runner does with a tide conflict, and the tide's answer to the gate scorer's
        `earliest_feasible_start_shift_min`: not "start earlier" but "wait". `None` when the
        series does not reach a low water after this arrival, which is a real answer — two
        days of predictions end somewhere.
        """
        lows = [e.time for e in self.extremes if e.kind == "low" and e.time >= when]
        if not lows:
            return None
        return round((min(lows) - when).total_seconds() / 60.0, 1)

    def coverage(self, kind: str, jurisdiction: str | None = None) -> CoverageEntry:
        """One manifest line for this stretch's tide lookup."""
        if self.answered and self.station is not None:
            distance = "" if self.distance_m is None else f", {self.distance_m / 1000:.1f} km away"
            return CoverageEntry(
                source=SOURCE,
                kind=kind,
                checked=True,
                jurisdiction=jurisdiction,
                reason=(
                    f"{self.station.name} ({self.station.id}){distance}: "
                    f"{len(self.extremes)} predicted high and low waters"
                ),
            )
        return CoverageEntry(
            source=SOURCE,
            kind=kind,
            checked=False,
            jurisdiction=jurisdiction,
            reason=self.reason or "no tide prediction for this stretch",
        )


# --- keys and parsing -------------------------------------------------------


def stations_args() -> dict[str, Any]:
    """Cache-key inputs for the station list. Named so a stability test can pin the hash."""
    return {"type": "tidepredictions", "units": "metric"}


def predictions_args(station_id: str, day: date, days: int = PREDICTION_DAYS) -> dict[str, Any]:
    """Cache-key inputs for one station's predictions.

    The station id and the span are in the key and the `application` string is not: a
    cassette recorded by this project must not stop replaying because somebody renamed the
    caller, and the parameter changes nothing about the answer.
    """
    return {
        "station": str(station_id),
        "begin_date": day.strftime("%Y%m%d"),
        "range_days": int(days),
        "product": "predictions",
        "interval": "hilo",
        "datum": "MLLW",
        "units": "metric",
        "time_zone": "lst_ldt",
    }


def parse_stations(payload: Any) -> list[TideStation]:
    """CO-OPS station metadata into stations, skipping rows with no usable position."""
    rows = (payload or {}).get("stations") if isinstance(payload, dict) else None
    out: list[TideStation] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            lat, lon = float(row["lat"]), float(row["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        identifier = str(row.get("id") or "").strip()
        if not identifier:
            continue
        out.append(
            TideStation(id=identifier, name=str(row.get("name") or identifier), lat=lat, lon=lon)
        )
    return out


def parse_predictions(payload: Any) -> list[TideExtreme]:
    """A `datagetter` hi/lo payload into extremes, in time order.

    CO-OPS reports a refused request as **HTTP 200 with an `error` object**, so a caller
    that only checked the status code would read a rejection as an empty tide. That is the
    single most likely way this client would go quietly wrong, and it is why an error body
    returns nothing here and the caller turns "nothing" into a reason.
    """
    if not isinstance(payload, dict) or payload.get("error"):
        return []
    out: list[TideExtreme] = []
    for row in payload.get("predictions") or []:
        if not isinstance(row, dict):
            continue
        try:
            when = datetime.strptime(str(row["t"]).strip(), "%Y-%m-%d %H:%M")
        except (KeyError, TypeError, ValueError):
            continue
        marker = str(row.get("type") or "").strip().upper()
        if marker not in ("H", "L"):
            continue
        try:
            height = float(row["v"])
        except (KeyError, TypeError, ValueError):
            height = None
        out.append(TideExtreme(time=when, height_m=height, kind="high" if marker == "H" else "low"))
    return sorted(out, key=lambda e: e.time)


def nearest_station(
    stations: list[TideStation], lat: float, lon: float
) -> tuple[TideStation, float] | None:
    """The closest station and its great-circle distance in metres, or None if there are none.

    Great-circle rather than along-route, which is the opposite of `RouteForecast.nearest`'s
    rule and for a reason that does not transfer: a forecast site is a sample *of the route*
    and the question is where the runner will be, while a tide station is an external gauge
    and the question is how far the tidal wave has to travel. A station 300 m across a
    harbour mouth predicts this beach; the route's own kilometre marks have nothing to say
    about that.
    """
    from longrun.core.geo.gpx import haversine_m

    if not stations:
        return None
    best = min(stations, key=lambda s: haversine_m(lat, lon, s.lat, s.lon))
    return best, haversine_m(lat, lon, best.lat, best.lon)


# --- fetching ---------------------------------------------------------------


def _get_json(url: str, params: dict[str, Any]) -> Any:
    import httpx

    response = httpx.get(
        url,
        params=params,
        headers={"Accept": "application/json"},
        timeout=HTTP_TIMEOUT_S,
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.json()


def _producer(ctx: ScorerContext, call: Callable[[], Any]) -> Callable[[], Any]:
    """Charge the budget on a real call, and only on a real call.

    Inside the producer rather than around `fetch`, exactly as `forecast._producer` does:
    scope §6.4 caps external calls per plan and replaying a cassette is not one.
    """

    def run() -> Any:
        ctx.budget.spend_api_call()
        return call()

    return run


def _describe(exc: Exception) -> str:
    """A short cause for a coverage line. No cache key: see `forecast._describe`."""
    if isinstance(exc, CacheMiss):
        return "not in the cassette"
    if isinstance(exc, BudgetExceeded):
        return "external API budget exhausted for this plan"
    return f"{type(exc).__name__}: {exc}"


def tide_stations(ctx: ScorerContext) -> list[TideStation]:
    """Every CO-OPS station with a predicted tide.

    Keyed on `STATIC_DAY`: the gauge network does not change from one plan date to the next,
    and keying it by day would re-download the whole list daily and multiply every cassette
    that holds it. Raises whatever `fetch` raises — the one caller catches, because a
    station list that could not be read and a station list with no station near the route
    are different answers and only the caller knows which stretch is asking.
    """
    args = stations_args()
    payload = fetch(
        ctx.cache,
        f"{SOURCE}.stations",
        args,
        STATIC_DAY,
        _producer(ctx, lambda: _get_json(NOAA_STATIONS_ROOT, args)),
    )
    return parse_stations(payload)


def station_tides(
    ctx: ScorerContext,
    lat: float,
    lon: float,
    day: date,
    *,
    reach_m: float = STATION_REACH_M,
) -> StationTides:
    """Predicted high and low waters near one point, or a reason there are none.

    Never raises. Every branch that fails returns a `StationTides` whose `reason` is fit to
    print in the coverage manifest, because the caller is a scorer inside the plan loop and
    §3.6's answer to "the source could not be consulted" is a recorded entry rather than an
    exception.
    """
    rounded_lat, rounded_lon = round(lat, COORD_PRECISION), round(lon, COORD_PRECISION)

    try:
        stations = tide_stations(ctx)
    except Exception as exc:  # network, HTTP status, cassette miss, bad payload, budget
        return StationTides(reason=f"NOAA CO-OPS station list: {_describe(exc)}")

    found = nearest_station(stations, rounded_lat, rounded_lon)
    if found is None:
        return StationTides(reason="NOAA CO-OPS published no tide-prediction stations")
    station, distance_m = found
    if distance_m > reach_m:
        return StationTides(
            distance_m=round(distance_m, 1),
            reason=(
                f"nearest tide station {station.name} ({station.id}) is "
                f"{distance_m / 1000:.0f} km away, beyond the {reach_m / 1000:.0f} km "
                f"a station is taken to govern"
            ),
        )

    try:
        payload = fetch(
            ctx.cache,
            f"{SOURCE}.predictions",
            predictions_args(station.id, day),
            day,
            _producer(
                ctx,
                lambda: _get_json(
                    NOAA_DATAGETTER_ROOT,
                    {
                        "application": APPLICATION,
                        "format": "json",
                        "begin_date": day.strftime("%Y%m%d"),
                        "end_date": (day + timedelta(days=PREDICTION_DAYS - 1)).strftime("%Y%m%d"),
                        "station": station.id,
                        "product": "predictions",
                        "interval": "hilo",
                        "datum": "MLLW",
                        "units": "metric",
                        "time_zone": "lst_ldt",
                    },
                ),
            ),
        )
    except Exception as exc:
        return StationTides(
            station=station,
            distance_m=round(distance_m, 1),
            reason=f"NOAA CO-OPS predictions for {station.id}: {_describe(exc)}",
        )

    extremes = parse_predictions(payload)
    if not extremes:
        return StationTides(
            station=station,
            distance_m=round(distance_m, 1),
            reason=(
                f"NOAA CO-OPS returned no high or low waters for "
                f"{station.name} ({station.id}) on {day.isoformat()}"
            ),
        )
    return StationTides(station=station, distance_m=round(distance_m, 1), extremes=extremes)


__all__ = [
    "APPLICATION",
    "HIGH_WATER_WINDOW_MIN",
    "NOAA_DATAGETTER_ROOT",
    "NOAA_STATIONS_ROOT",
    "PREDICTION_DAYS",
    "SOURCE",
    "STATION_REACH_M",
    "StationTides",
    "TideExtreme",
    "TideStation",
    "nearest_station",
    "parse_predictions",
    "parse_stations",
    "predictions_args",
    "station_tides",
    "stations_args",
    "tide_stations",
]
