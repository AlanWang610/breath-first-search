"""Air quality along a route (scope 7.4, 14).

Scope §7.4 names AirNow (EPA) and PurpleAir. Both need API keys, `.env.example` ships both
blank, and a scorer that can only ever report "unavailable" is not a scorer. **ADR 0006**
records the substitution: Open-Meteo's air-quality endpoint needs no key, publishes US AQI
and PM2.5 on the same grid as its weather forecast, and reaches the same cache through the
same door.

The deviation is real and is not free. AirNow is the authoritative US source and reports
what monitors actually measured; Open-Meteo publishes a model (CAMS), and a model over a
street canyon is not a measurement in it. So the coverage entry says which one answered,
and `air_quality` reports the model's own resolution rather than implying a roadside
reading.

Scope §7.4's signature is `air_quality(gpx, date)` — by date, not by ETA, unlike every
other environment tool. That is the scope's inconsistency with its own §3 "time-aware
everything" principle, and this module resolves it the other way: the series is hourly and
is read at the ETA, because a route that finishes at dusk meets a different AQI from one
that finishes at noon.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from longrun.core.data.cache import COORD_PRECISION, fetch
from longrun.core.data.forecast import (
    DEFAULT_SPACING_M,
    MAX_SAMPLE_POINTS,
    ForecastSite,
    sample_points,
)
from longrun.core.models.context import BudgetExceeded
from longrun.core.models.coverage import CoverageEntry

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext
    from longrun.core.models.geometry import Route

OPEN_METEO_AIR_ROOT = "https://air-quality-api.open-meteo.com/v1/air-quality"

#: Hourly fields, as a tuple so the cache key cannot change because a set iterated
#: differently.
AIR_FIELDS = ("pm2_5", "pm10", "us_aqi")

HTTP_TIMEOUT_S = 10.0

#: Air quality varies over kilometres, not metres, and the model grid is coarser than the
#: weather one. Sampling as densely as the forecast would spend calls for identical answers.
DEFAULT_AIR_SPACING_M = DEFAULT_SPACING_M * 2


class AirHour(BaseModel):
    """One hour of air quality at one place."""

    model_config = {"frozen": True}

    time: datetime
    us_aqi: float | None = None
    pm2_5: float | None = None
    pm10: float | None = None


class AirSite(BaseModel):
    site: ForecastSite
    hours: list[AirHour] = Field(default_factory=list)
    reason: str | None = None

    def at(self, when: datetime) -> AirHour | None:
        """Nearest hour, not interpolated.

        An AQI is a banded index over an hour, and averaging two bands produces a number
        that is in neither. The underlying PM2.5 could be interpolated; reporting them from
        the same hour keeps the pair consistent.
        """
        if not self.hours:
            return None
        return min(self.hours, key=lambda h: abs((h.time - when).total_seconds()))


class RouteAirQuality(BaseModel):
    sites: list[AirSite] = Field(default_factory=list)

    @property
    def answered(self) -> bool:
        return any(site.hours for site in self.sites)

    def at_distance(self, cum_dist_m: float, when: datetime) -> AirHour | None:
        with_hours = [s for s in self.sites if s.hours]
        if not with_hours:
            return None
        nearest = min(with_hours, key=lambda s: abs(s.site.cum_dist_m - cum_dist_m))
        return nearest.at(when)

    def coverage(self) -> list[CoverageEntry]:
        answered = sum(1 for s in self.sites if s.hours)
        if answered:
            return [
                CoverageEntry(
                    source="open_meteo",
                    kind="air_quality",
                    checked=True,
                    reason=(
                        f"{answered} of {len(self.sites)} sites, modelled (CAMS) rather "
                        "than measured; AirNow needs a key this install does not have"
                    ),
                )
            ]
        reasons = sorted({s.reason or "unknown" for s in self.sites})
        return [
            CoverageEntry(
                source="open_meteo",
                kind="air_quality",
                checked=False,
                reason="; ".join(reasons) or "no site answered",
            )
        ]


def air_args(lat: float, lon: float, day: date) -> dict[str, Any]:
    """Cache-key inputs, rounded before hashing like the weather client's."""
    return {
        "latitude": round(lat, COORD_PRECISION),
        "longitude": round(lon, COORD_PRECISION),
        "hourly": AIR_FIELDS,
        "start_date": day.isoformat(),
        "end_date": day.isoformat(),
        "timezone": "UTC",
    }


def parse_air(payload: dict[str, Any]) -> list[AirHour]:
    hourly = payload.get("hourly") or {}
    stamps = hourly.get("time") or []
    columns = {name: hourly.get(name) or [] for name in AIR_FIELDS}
    out: list[AirHour] = []
    for i, stamp in enumerate(stamps):
        try:
            when = datetime.fromisoformat(str(stamp))
        except ValueError:
            continue
        values = {
            name: (float(col[i]) if i < len(col) and col[i] is not None else None)
            for name, col in columns.items()
        }
        out.append(AirHour(time=when, **values))
    return out


def _get(args: dict[str, Any]) -> Any:
    import httpx

    response = httpx.get(
        OPEN_METEO_AIR_ROOT,
        params={**args, "hourly": ",".join(AIR_FIELDS)},
        headers={"Accept": "application/json"},
        timeout=HTTP_TIMEOUT_S,
    )
    response.raise_for_status()
    return response.json()


def route_air_quality(
    route: Route,
    ctx: ScorerContext,
    day: date,
    *,
    spacing_m: float = DEFAULT_AIR_SPACING_M,
    max_points: int = MAX_SAMPLE_POINTS,
) -> RouteAirQuality:
    """Hourly AQI and PM2.5 at points along the route, through the cache."""
    results: list[AirSite] = []
    exhausted = False

    for site in sample_points(route, spacing_m=spacing_m, max_points=max_points):
        if exhausted:
            results.append(AirSite(site=site, reason="external API budget exhausted"))
            continue

        args = air_args(site.lat, site.lon, day)

        def producer(args: dict[str, Any] = args) -> Any:
            ctx.budget.spend_api_call()
            return _get(args)

        try:
            payload = fetch(ctx.cache, "open_meteo.air_quality", args, day, producer)
        except BudgetExceeded:
            exhausted = True
            results.append(AirSite(site=site, reason="external API budget exhausted"))
            continue
        except Exception as exc:
            from longrun.core.data.cache import CacheMiss

            reason = (
                "not in the cassette"
                if isinstance(exc, CacheMiss)
                else f"{type(exc).__name__}: {exc}"
            )
            results.append(AirSite(site=site, reason=reason))
            continue

        hours = parse_air(payload or {})
        results.append(
            AirSite(site=site, hours=hours, reason=None if hours else "no hours returned")
        )

    return RouteAirQuality(sites=results)


__all__ = [
    "AIR_FIELDS",
    "DEFAULT_AIR_SPACING_M",
    "OPEN_METEO_AIR_ROOT",
    "AirHour",
    "AirSite",
    "RouteAirQuality",
    "air_args",
    "parse_air",
    "route_air_quality",
]
