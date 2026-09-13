"""Hourly weather along a route (scope 7.4, 4.4).

Scope §7.4 is explicit that a single station is not enough — a 50 km route crosses
microclimates, and the marine layer that makes the first 10 km cool is not what the runner
meets at kilometre 40. So this samples several points and keeps them separate.

**This is the first module in the project that touches an external API**, and it sets the
pattern the M4 adapters will copy. Three rules it exists to demonstrate:

*Everything goes through the cache.* `core.data.cache.fetch` is the only door, keyed
`(tool, args_hash, day)`. With `LONGRUN_OFFLINE=1` a miss raises instead of fetching, which
is what makes a golden route hermetic and doubles as the cassette mechanism — there is no
second recording system.

*Cache keys are provider-scoped.* `nws.gridpoints` and `open_meteo.forecast` are separate
keys, never one `microclimate` key. Keying on the tool would bake whichever provider
happened to answer first into a key that claims to be provider-neutral, and a cassette
could then never gain the other one without invalidating what it already had.

*Coordinates are rounded before they reach the key.* An unrounded float makes the args hash
depend on the last bit of a haversine sum, so a cassette recorded on one machine misses on
another. Four decimal places is about 11 m — far finer than a 2.5 km NWS grid cell.

What is cached is the **normalized** series, with the provider recorded as a field. An NWS
cassette and an Open-Meteo cassette are then interchangeable downstream, and a plan does
not re-score differently because a fallback fired — while `RouteForecast.providers` still
records which answered, so a cassette that quietly lost its NWS recording shows up as a
reviewable diff rather than a silent change of source.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from longrun.core.data.cache import COORD_PRECISION, STATIC_DAY, CacheMiss, fetch
from longrun.core.models.context import BudgetExceeded
from longrun.core.models.coverage import CoverageEntry

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable, Sequence

    from longrun.core.models.context import ScorerContext
    from longrun.core.models.geometry import Route, Segment

#: NWS forecast grid cells are ~2.5 km. Sampling finer than two of them spends two HTTP
#: calls to ask the same cell the same question; 5 km guarantees a genuinely different
#: answer rather than an aliasing artifact.
DEFAULT_SPACING_M = 5000.0

#: Worst case 48 NWS calls (a `/points` and a gridpoint read each) against a 200-call
#: budget, leaving room for everything else a plan does.
MAX_SAMPLE_POINTS = 24


NWS_ROOT = "https://api.weather.gov"
OPEN_METEO_ROOT = "https://api.open-meteo.com/v1/forecast"

#: The same fields, for a day the forecast endpoint no longer covers.
#:
#: The forecast API reaches about a fortnight either side of today; a golden route pins an
#: **absolute** date forever (scope 11), so the day a golden was recorded on slides out of
#: that window and its cassette becomes unrecordable. `freeze-cassette`'s docstring said as
#: much - "a forecast for a past date can never be re-fetched" - and that is a property of
#: the endpoint, not of the weather. The archive holds the reanalysis for the same hours.
OPEN_METEO_ARCHIVE_ROOT = "https://archive-api.open-meteo.com/v1/archive"

#: How far back before the archive is used instead. The archive lags real time by a few
#: days, and inside that window the forecast endpoint is the one with data.
ARCHIVE_AFTER_DAYS = 10

#: Open-Meteo hourly fields, in the order they go into the cache key. Kept as a tuple
#: so the key cannot change because a set iterated differently.
OPEN_METEO_FIELDS = (
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "wind_speed_10m",
    "cloud_cover",
    "precipitation_probability",
)

#: NWS policy requires a contact string. Without one this module does not call NWS at all —
#: sending a default user agent against a policy that asks for a contact is how a project
#: gets blocked, and `conftest._clear_longrun_env` deletes it so tests never try.
USER_AGENT_ENV_VAR = "LONGRUN_NWS_USER_AGENT"

HTTP_TIMEOUT_S = 10.0

Provider = Literal["nws", "open-meteo", "none"]

#: NWS reports each field with a `uom`; everything here is converted to SI on the way in.
_UOM_SCALE: dict[str, float] = {
    "wmoUnit:degC": 1.0,
    "wmoUnit:percent": 1.0,
    "wmoUnit:m_s-1": 1.0,
    "wmoUnit:km_h-1": 1.0 / 3.6,
}

_DURATION = re.compile(r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?)?$")


class HourlyPoint(BaseModel):
    """One hour of forecast at one place. Every field optional: a provider may not say."""

    model_config = {"frozen": True}

    time: datetime
    temp_c: float | None = None
    dewpoint_c: float | None = None
    relative_humidity_pct: float | None = None
    wind_speed_ms: float | None = None
    cloud_cover_pct: float | None = None
    precip_probability_pct: float | None = None


class ForecastSite(BaseModel):
    """A place on the route where the forecast was asked for."""

    model_config = {"frozen": True}

    index: int
    route_index: int
    lat: float
    lon: float
    cum_dist_m: float


class SiteForecast(BaseModel):
    """One site's normalized series, and which provider produced it."""

    site: ForecastSite
    provider: Provider = "none"
    provider_detail: str | None = None
    hours: list[HourlyPoint] = Field(default_factory=list)
    reason: str | None = None

    def at(self, when: datetime) -> HourlyPoint | None:
        """The forecast at an instant, interpolated between bracketing hours.

        Returns None outside the horizon rather than clamping to the last hour. A run
        planned three weeks out is beyond any hourly forecast, and saying so is the
        difference between a measurement and a guess (scope 3.6).
        """
        if not self.hours:
            return None
        ordered = sorted(self.hours, key=lambda h: h.time)
        if when < ordered[0].time or when > ordered[-1].time:
            return None
        for earlier, later in zip(ordered[:-1], ordered[1:], strict=True):
            if earlier.time <= when <= later.time:
                span = (later.time - earlier.time).total_seconds()
                weight = 0.0 if span <= 0 else (when - earlier.time).total_seconds() / span
                return _blend(earlier, later, weight, when)
        return ordered[-1]


class RouteForecast(BaseModel):
    """Every site along one route, for one day."""

    sites: list[SiteForecast] = Field(default_factory=list)
    spacing_m: float = DEFAULT_SPACING_M

    @property
    def providers(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for site in self.sites:
            counts[site.provider] = counts.get(site.provider, 0) + 1
        return counts

    @property
    def answered(self) -> bool:
        return any(site.hours for site in self.sites)

    def nearest(self, cum_dist_m: float) -> SiteForecast | None:
        """The site nearest **along the route**, not as the crow flies.

        On an out-and-back, a site 300 m away across the turnaround is 8 km away along the
        line, and the weather question is about where the runner will be — not about what
        is geometrically close.
        """
        with_hours = [s for s in self.sites if s.hours]
        if not with_hours:
            return None
        return min(with_hours, key=lambda s: abs(s.site.cum_dist_m - cum_dist_m))

    def at_distance(self, cum_dist_m: float, when: datetime) -> HourlyPoint | None:
        site = self.nearest(cum_dist_m)
        return None if site is None else site.at(when)

    def gap_to_nearest_m(self, cum_dist_m: float) -> float | None:
        site = self.nearest(cum_dist_m)
        return None if site is None else abs(site.site.cum_dist_m - cum_dist_m)

    def for_segments(
        self, segments: Sequence[Segment], etas: Sequence[datetime]
    ) -> list[HourlyPoint | None]:
        """One forecast per segment, at that segment's own arrival time."""
        out: list[HourlyPoint | None] = []
        for segment in segments:
            when = etas[segment.start_idx] if segment.start_idx < len(etas) else None
            centre = segment.cum_start_m + segment.length_m / 2.0
            out.append(None if when is None else self.at_distance(centre, when))
        return out

    def coverage(self) -> list[CoverageEntry]:
        """One entry per provider that answered, plus one for the sites that did not."""
        entries: list[CoverageEntry] = []
        for provider, sources in (("nws", "nws"), ("open-meteo", "open_meteo")):
            count = self.providers.get(provider, 0)
            if count:
                entries.append(
                    CoverageEntry(
                        source=sources,
                        kind="forecast",
                        checked=True,
                        reason=f"{count} of {len(self.sites)} sites along the route",
                    )
                )
        unanswered = [s for s in self.sites if not s.hours]
        if unanswered:
            entries.append(
                CoverageEntry(
                    source="forecast",
                    kind="forecast",
                    checked=False,
                    reason=(
                        f"{len(unanswered)} of {len(self.sites)} sites had no forecast: "
                        + "; ".join(sorted({s.reason or "unknown" for s in unanswered}))
                    ),
                )
            )
        return entries


def _blend(a: HourlyPoint, b: HourlyPoint, weight: float, when: datetime) -> HourlyPoint:
    """Linear between two hours for continuous fields, nearest for a probability.

    A precipitation probability *for* the 14:00 hour is a statement about that hour, not a
    value at an instant, so interpolating it would invent a number nobody published.
    """

    def mix(x: float | None, y: float | None) -> float | None:
        if x is None:
            return y
        if y is None:
            return x
        return x + (y - x) * weight

    nearer = a if weight < 0.5 else b
    return HourlyPoint(
        time=when,
        temp_c=mix(a.temp_c, b.temp_c),
        dewpoint_c=mix(a.dewpoint_c, b.dewpoint_c),
        relative_humidity_pct=mix(a.relative_humidity_pct, b.relative_humidity_pct),
        wind_speed_ms=mix(a.wind_speed_ms, b.wind_speed_ms),
        cloud_cover_pct=mix(a.cloud_cover_pct, b.cloud_cover_pct),
        precip_probability_pct=nearer.precip_probability_pct,
    )


def sample_points(
    route: Route,
    spacing_m: float = DEFAULT_SPACING_M,
    max_points: int = MAX_SAMPLE_POINTS,
) -> list[ForecastSite]:
    """Where along the route to ask.

    The first and last points are always included whatever the spacing: a start and a
    finish are where a runner decides whether to go at all.
    """
    if not route.points:
        return []

    wanted: list[int] = [0]
    last_at = route.points[0].cum_dist_m
    for index, point in enumerate(route.points):
        if point.cum_dist_m - last_at >= spacing_m:
            wanted.append(index)
            last_at = point.cum_dist_m
    final = len(route.points) - 1
    if final not in wanted:
        wanted.append(final)

    if len(wanted) > max_points:
        step = len(wanted) / max_points
        thinned = sorted({wanted[min(int(i * step), len(wanted) - 1)] for i in range(max_points)})
        if final not in thinned:
            thinned.append(final)
        wanted = thinned

    return [
        ForecastSite(
            index=i,
            route_index=index,
            lat=round(route.points[index].lat, COORD_PRECISION),
            lon=round(route.points[index].lon, COORD_PRECISION),
            cum_dist_m=route.points[index].cum_dist_m,
        )
        for i, index in enumerate(wanted)
    ]


# --- parsing ----------------------------------------------------------------


def _duration_hours(text: str) -> int:
    match = _DURATION.match(text)
    if not match:
        return 1
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    return max(1, days * 24 + hours + (1 if minutes else 0))


def expand_intervals(field: dict[str, Any] | None) -> dict[datetime, float]:
    """One NWS gridpoint field into `{hour: value}`.

    NWS publishes values over ISO-8601 *intervals* (`2026-09-12T14:00:00+00:00/PT3H`), not
    at instants, so a three-hour block has to become three hours. The `uom` is honoured
    here rather than assumed: wind arrives in km/h from some offices and m/s from others.
    """
    if not field or not isinstance(field.get("values"), list):
        return {}
    scale = _UOM_SCALE.get(str(field.get("uom", "")), 1.0)
    out: dict[datetime, float] = {}
    for entry in field["values"]:
        raw = entry.get("value")
        if raw is None:
            continue
        stamp, _, duration = str(entry.get("validTime", "")).partition("/")
        try:
            start = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        for hour in range(_duration_hours(duration)):
            out[start + timedelta(hours=hour)] = float(raw) * scale
    return out


def parse_nws_gridpoint(payload: dict[str, Any]) -> list[HourlyPoint]:
    """An NWS raw gridpoint payload into normalized hours.

    The *raw* endpoint, not `/forecast/hourly`, because only the raw one carries
    `skyCover` — and cloud cover is what `sun_exposure` needs to scale clear-sky
    irradiance. Same call count, more fields, at the cost of expanding intervals.
    """
    properties = payload.get("properties", {})
    fields = {
        "temp_c": expand_intervals(properties.get("temperature")),
        "dewpoint_c": expand_intervals(properties.get("dewpoint")),
        "relative_humidity_pct": expand_intervals(properties.get("relativeHumidity")),
        "wind_speed_ms": expand_intervals(properties.get("windSpeed")),
        "cloud_cover_pct": expand_intervals(properties.get("skyCover")),
        "precip_probability_pct": expand_intervals(properties.get("probabilityOfPrecipitation")),
    }
    hours = sorted({stamp for series in fields.values() for stamp in series})
    return [
        HourlyPoint(time=stamp, **{name: series.get(stamp) for name, series in fields.items()})
        for stamp in hours
    ]


def parse_open_meteo(payload: dict[str, Any]) -> list[HourlyPoint]:
    """An Open-Meteo hourly payload into the same normalized hours."""
    hourly = payload.get("hourly") or {}
    stamps = hourly.get("time") or []
    columns = {
        "temp_c": hourly.get("temperature_2m") or [],
        "dewpoint_c": hourly.get("dew_point_2m") or [],
        "relative_humidity_pct": hourly.get("relative_humidity_2m") or [],
        "wind_speed_ms": hourly.get("wind_speed_10m") or [],
        "cloud_cover_pct": hourly.get("cloud_cover") or [],
        "precip_probability_pct": hourly.get("precipitation_probability") or [],
    }
    out: list[HourlyPoint] = []
    for i, stamp in enumerate(stamps):
        try:
            when = datetime.fromisoformat(str(stamp))
        except ValueError:
            continue
        values = {
            name: (float(col[i]) if i < len(col) and col[i] is not None else None)
            for name, col in columns.items()
        }
        out.append(HourlyPoint(time=when, **values))
    return out


# --- fetching ---------------------------------------------------------------


def user_agent(env: dict[str, str] | None = None) -> str | None:
    """The NWS contact string, or None when nobody set one."""
    source = os.environ if env is None else env
    return source.get(USER_AGENT_ENV_VAR, "").strip() or None


def nws_points_args(lat: float, lon: float) -> dict[str, float]:
    """Cache-key inputs for `/points`. Named so a stability test can pin the hash."""
    return {"lat": round(lat, COORD_PRECISION), "lon": round(lon, COORD_PRECISION)}


def nws_grid_args(office: str, grid_x: int, grid_y: int) -> dict[str, Any]:
    return {"office": office, "x": int(grid_x), "y": int(grid_y)}


def open_meteo_args(lat: float, lon: float, day: date) -> dict[str, Any]:
    return {
        "latitude": round(lat, COORD_PRECISION),
        "longitude": round(lon, COORD_PRECISION),
        "hourly": OPEN_METEO_FIELDS,
        "start_date": day.isoformat(),
        "end_date": day.isoformat(),
        "timezone": "UTC",
    }


def _get_json(url: str, params: dict[str, Any] | None, headers: dict[str, str]) -> Any:
    import httpx

    response = httpx.get(
        url, params=params, headers=headers, timeout=HTTP_TIMEOUT_S, follow_redirects=True
    )
    response.raise_for_status()
    return response.json()


def _producer(ctx: ScorerContext, call: Callable[[], Any]) -> Callable[[], Any]:
    """Wrap a real HTTP call so the budget is spent on it — and only on it.

    Spent inside the producer rather than around `fetch`, so a cache hit costs nothing.
    Scope 6.4 caps external calls per plan, and a cassette replay is not an external call.
    """

    def run() -> Any:
        ctx.budget.spend_api_call()
        return call()

    return run


def _nws_site(site: ForecastSite, ctx: ScorerContext, day: date) -> SiteForecast:
    """Ask NWS for one site: `/points` for the grid cell, then the raw gridpoint."""
    agent = user_agent()
    if agent is None:
        return SiteForecast(
            site=site,
            reason=f"{USER_AGENT_ENV_VAR} is not set; NWS requires a contact string",
        )

    headers = {"User-Agent": agent, "Accept": "application/geo+json"}
    grid = fetch(
        ctx.cache,
        "nws.points",
        nws_points_args(site.lat, site.lon),
        STATIC_DAY,
        _producer(
            ctx, lambda: _get_json(f"{NWS_ROOT}/points/{site.lat},{site.lon}", None, headers)
        ),
    )
    properties = (grid or {}).get("properties") or {}
    office = properties.get("gridId")
    grid_x, grid_y = properties.get("gridX"), properties.get("gridY")
    if office is None or grid_x is None or grid_y is None:
        return SiteForecast(site=site, reason="NWS returned no forecast grid for this point")

    payload = fetch(
        ctx.cache,
        "nws.gridpoints",
        nws_grid_args(office, grid_x, grid_y),
        day,
        _producer(
            ctx,
            lambda: _get_json(f"{NWS_ROOT}/gridpoints/{office}/{grid_x},{grid_y}", None, headers),
        ),
    )
    hours = parse_nws_gridpoint(payload or {})
    if not hours:
        return SiteForecast(site=site, reason="NWS gridpoint carried no usable hours")
    return SiteForecast(
        site=site, provider="nws", provider_detail=f"{office}/{grid_x},{grid_y}", hours=hours
    )


def open_meteo_root(day: date, today: date | None = None) -> str:
    """Which Open-Meteo endpoint can answer for a day.

    A named function so the choice is testable without the network, and because it is the
    kind of thing that looks like an implementation detail and is not: the two endpoints
    return the same fields for the same hours but one of them returns *nothing* outside
    its window, which reads downstream as "Open-Meteo returned no hours".
    """
    from datetime import date as _date

    reference = today or _date.today()
    return (
        OPEN_METEO_ARCHIVE_ROOT if (reference - day).days > ARCHIVE_AFTER_DAYS else OPEN_METEO_ROOT
    )


def _open_meteo_site(site: ForecastSite, ctx: ScorerContext, day: date) -> SiteForecast:
    args = open_meteo_args(site.lat, site.lon, day)
    # The endpoint is chosen from the day but is deliberately **not** in the cache key: a
    # cassette recorded from the archive has to replay for a scorer that would have asked
    # the forecast endpoint, and the answer is the same weather either way.
    root = open_meteo_root(day, ctx.clock.now().date())
    payload = fetch(
        ctx.cache,
        "open_meteo.forecast",
        args,
        day,
        _producer(
            ctx,
            lambda: _get_json(
                root,
                {**args, "hourly": ",".join(OPEN_METEO_FIELDS)},
                {"Accept": "application/json"},
            ),
        ),
    )
    hours = parse_open_meteo(payload or {})
    if not hours:
        return SiteForecast(site=site, reason="Open-Meteo returned no hours")
    return SiteForecast(site=site, provider="open-meteo", provider_detail="open-meteo", hours=hours)


def _describe(exc: Exception) -> str:
    """A short cause, fit for a coverage line in a plan sheet.

    Deliberately without the cache key: two sites missing from the same cassette are one
    fact for a reader, and including the args hash makes them two different strings that
    cannot be deduplicated. The full key is still in the `CacheMiss` a caller can catch.
    """
    if isinstance(exc, CacheMiss):
        return "not in the cassette"
    return f"{type(exc).__name__}: {exc}"


def route_forecast(
    route: Route,
    ctx: ScorerContext,
    day: date,
    *,
    spacing_m: float = DEFAULT_SPACING_M,
    max_points: int = MAX_SAMPLE_POINTS,
) -> RouteForecast:
    """Hourly forecast at several points along the route (scope 7.4).

    NWS first, Open-Meteo second, and a site that gets neither says so rather than
    vanishing. A partial forecast — eighteen sites of twenty-one — is genuinely useful, so
    one missing cassette key must not take the whole scorer down; what it must not do is
    pass silently, which is why every failure lands in `SiteForecast.reason` and from there
    in the coverage manifest.
    """
    sites = sample_points(route, spacing_m=spacing_m, max_points=max_points)
    results: list[SiteForecast] = []
    exhausted = False

    for site in sites:
        if exhausted:
            results.append(
                SiteForecast(site=site, reason="external API budget exhausted for this plan")
            )
            continue

        reasons: list[str] = []
        answered: SiteForecast | None = None
        for attempt in (_nws_site, _open_meteo_site):
            try:
                candidate = attempt(site, ctx, day)
            except BudgetExceeded:
                # Not an error in the data: a resource decision. Every remaining site is
                # marked, and the plan reports it rather than the scorer dying.
                exhausted = True
                reasons.append("external API budget exhausted for this plan")
                break
            except Exception as exc:  # network, HTTP status, cassette miss, bad payload
                reasons.append(_describe(exc))
                continue
            if candidate.hours:
                answered = candidate
                break
            reasons.append(candidate.reason or "no hours returned")

        results.append(
            answered
            if answered is not None
            else SiteForecast(site=site, reason="; ".join(reasons) or "no provider answered")
        )

    return RouteForecast(sites=results, spacing_m=spacing_m)


__all__ = [
    "COORD_PRECISION",
    "DEFAULT_SPACING_M",
    "MAX_SAMPLE_POINTS",
    "ARCHIVE_AFTER_DAYS",
    "OPEN_METEO_ARCHIVE_ROOT",
    "OPEN_METEO_ROOT",
    "NWS_ROOT",
    "STATIC_DAY",
    "USER_AGENT_ENV_VAR",
    "ForecastSite",
    "HourlyPoint",
    "RouteForecast",
    "SiteForecast",
    "OPEN_METEO_FIELDS",
    "expand_intervals",
    "nws_grid_args",
    "nws_points_args",
    "open_meteo_args",
    "open_meteo_root",
    "parse_nws_gridpoint",
    "parse_open_meteo",
    "route_forecast",
    "sample_points",
    "user_agent",
]
