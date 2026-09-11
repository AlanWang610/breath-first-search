"""NWS watches, warnings and advisories along a route (scope 7.6).

`hazards` needs two things the OSM and NHD layers cannot answer: whether a river a route
fords is in flood on the day, and whether the day carries a red-flag fire warning. Both are
NWS alert products, and both are properties of a *date and a place* rather than of the
terrain — which is why they belong here, behind the cache, and not in a layer.

Structurally this is `forecast.py`'s smaller sibling and deliberately so: same
`ForecastSite` sampling, same `fetch(cache, tool, args, day, producer)` door, same
`LONGRUN_NWS_USER_AGENT` requirement, same refusal to invent a contact string. A scorer
therefore replays alerts from the same cassette that carries its forecast.

**Alerts are near-term by nature, and the module says so rather than pretending.** NWS
publishes active and recently expired alerts, not an archive: a plan for a date three weeks
out will correctly get nothing, and that is *not* the same claim as "no flood warning is in
force". `RouteAlerts.horizon_days` carries how far ahead the query could see, and
`hazards` turns a query beyond it into an `unknown`, never into an all-clear.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from longrun.core.data.cache import COORD_PRECISION, CacheMiss, fetch
from longrun.core.data.forecast import (
    HTTP_TIMEOUT_S,
    NWS_ROOT,
    USER_AGENT_ENV_VAR,
    ForecastSite,
    sample_points,
    user_agent,
)
from longrun.core.models.context import BudgetExceeded

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext
    from longrun.core.models.geometry import Route

#: Alert sites are much sparser than forecast sites. An NWS alert is issued over a county
#: or a fire-weather zone, so sampling every 5 km asks the same zone the same question ten
#: times and spends ten API calls doing it.
DEFAULT_ALERT_SPACING_M = 30_000.0

#: At most this many sites, however long the route. A 100 km route is four.
MAX_ALERT_SITES = 6

#: How far ahead NWS alerts are meaningful. Warnings run hours to a few days; a plan for a
#: date beyond this gets `unknown`, which is the honest answer and not an all-clear.
ALERT_HORIZON_DAYS = 7

#: The `event` values scope 7.6 names, matched case-insensitively as substrings because NWS
#: publishes "Flood Warning", "Flash Flood Watch" and "Red Flag Warning" as distinct events
#: and the family is what matters.
WANTED_EVENTS: tuple[str, ...] = ("flood", "red flag", "fire weather")

#: Severity ordering as NWS publishes it, worst first.
SEVERITY_ORDER: tuple[str, ...] = ("Extreme", "Severe", "Moderate", "Minor", "Unknown")


@dataclass(frozen=True)
class Alert:
    """One NWS alert, reduced to what a hazard flag needs."""

    event: str
    severity: str
    headline: str
    onset: str | None
    expires: str | None

    @property
    def family(self) -> str:
        """Which of scope 7.6's two families this is, for the reason code."""
        lowered = self.event.lower()
        return "flood" if "flood" in lowered else "fire_weather"


@dataclass
class SiteAlerts:
    """What one sampled point got back."""

    site: ForecastSite
    alerts: list[Alert] = field(default_factory=list)
    reason: str | None = None

    @property
    def answered(self) -> bool:
        return self.reason is None


@dataclass
class RouteAlerts:
    """Every site's answer, plus how far ahead the question could be asked."""

    sites: list[SiteAlerts] = field(default_factory=list)
    horizon_days: int = ALERT_HORIZON_DAYS
    beyond_horizon: bool = False

    @property
    def answered(self) -> bool:
        return any(site.answered for site in self.sites)

    @property
    def all_alerts(self) -> list[Alert]:
        """Deduplicated by event and headline: adjacent sites share a warning zone."""
        seen: dict[tuple[str, str], Alert] = {}
        for site in self.sites:
            for alert in site.alerts:
                seen.setdefault((alert.event, alert.headline), alert)
        return sorted(seen.values(), key=lambda a: _severity_rank(a.severity))

    @property
    def reasons(self) -> list[str]:
        return sorted({s.reason for s in self.sites if s.reason is not None})


def _severity_rank(severity: str) -> int:
    try:
        return SEVERITY_ORDER.index(severity)
    except ValueError:
        return len(SEVERITY_ORDER)


def alert_args(lat: float, lon: float, day: date) -> dict[str, Any]:
    """Cache-key inputs, rounded so two nearby sites share a key.

    A named function with a stability test, for the reason `forecast.py` gives: the
    cassette is produced through this same path, so a rounding change that nobody noticed
    would invalidate every recorded key at once.
    """
    return {
        "point": f"{round(lat, COORD_PRECISION)},{round(lon, COORD_PRECISION)}",
        "start": f"{day.isoformat()}T00:00:00Z",
        "end": f"{day.isoformat()}T23:59:59Z",
    }


def parse_alerts(payload: Any) -> list[Alert]:
    """The alerts we care about, out of a GeoJSON FeatureCollection."""
    out: list[Alert] = []
    for feature in (payload or {}).get("features") or []:
        properties = feature.get("properties") or {}
        event = str(properties.get("event") or "").strip()
        if not any(word in event.lower() for word in WANTED_EVENTS):
            continue
        out.append(
            Alert(
                event=event,
                severity=str(properties.get("severity") or "Unknown"),
                headline=str(properties.get("headline") or event),
                onset=properties.get("onset"),
                expires=properties.get("expires"),
            )
        )
    return out


def _get_json(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
    import httpx

    response = httpx.get(
        url, params=params, headers=headers, timeout=HTTP_TIMEOUT_S, follow_redirects=True
    )
    response.raise_for_status()
    return response.json()


def _describe(exc: Exception) -> str:
    """A failure reason with nothing machine-specific in it.

    `CacheMiss` names its args hash, which makes two identical failures read as two
    different ones in a coverage manifest and puts a hash into a golden expectation.
    """
    if isinstance(exc, CacheMiss):
        return "not in the cassette"
    if isinstance(exc, BudgetExceeded):
        return "API call budget exhausted"
    return f"{type(exc).__name__}"


def _site_alerts(site: ForecastSite, ctx: ScorerContext, day: date) -> SiteAlerts:
    agent = user_agent()
    if agent is None:
        return SiteAlerts(
            site=site,
            reason=f"{USER_AGENT_ENV_VAR} is not set; NWS requires a contact string",
        )

    headers = {"User-Agent": agent, "Accept": "application/geo+json"}
    args = alert_args(site.lat, site.lon, day)

    def produce() -> Any:
        ctx.budget.spend_api_call()
        return _get_json(f"{NWS_ROOT}/alerts", args, headers)

    try:
        payload = fetch(ctx.cache, "nws.alerts", args, day, produce)
    except Exception as exc:  # noqa: BLE001 - one site's failure is not the route's
        return SiteAlerts(site=site, reason=_describe(exc))
    return SiteAlerts(site=site, alerts=parse_alerts(payload))


def route_alerts(
    route: Route,
    ctx: ScorerContext,
    day: date,
    *,
    spacing_m: float = DEFAULT_ALERT_SPACING_M,
    max_points: int = MAX_ALERT_SITES,
    today: date | None = None,
) -> RouteAlerts:
    """Flood and fire-weather alerts along a route, for one day (scope 7.6).

    Returns `beyond_horizon` rather than an empty list when the plan date is further out
    than NWS publishes for. The distinction is the whole point: "nothing is in force" and
    "nobody can know yet" are different claims, and only the first is an all-clear.
    """
    reference = today or ctx.clock.now().date()
    result = RouteAlerts(beyond_horizon=(day - reference) > timedelta(days=ALERT_HORIZON_DAYS))
    if result.beyond_horizon:
        return result

    for site in sample_points(route, spacing_m=spacing_m, max_points=max_points):
        result.sites.append(_site_alerts(site, ctx, day))
    return result


def covers_day(alert: Alert, day: date) -> bool | None:
    """Whether an alert is in force on a day, or None when its window cannot be read.

    None rather than True: an alert with an unparseable window is an alert we cannot place,
    and placing it anyway would put a flood warning on a route it may not touch.
    """
    onset, expires = _parse_time(alert.onset), _parse_time(alert.expires)
    if onset is None and expires is None:
        return None
    if onset is not None and onset.date() > day:
        return False
    return not (expires is not None and expires.date() < day)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


__all__ = [
    "ALERT_HORIZON_DAYS",
    "DEFAULT_ALERT_SPACING_M",
    "MAX_ALERT_SITES",
    "SEVERITY_ORDER",
    "WANTED_EVENTS",
    "Alert",
    "RouteAlerts",
    "SiteAlerts",
    "alert_args",
    "covers_day",
    "parse_alerts",
    "route_alerts",
]
