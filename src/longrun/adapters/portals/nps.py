"""National Park Service alerts, for `trail_status` (scope 7.6, 7.10).

Scope 7.6 names this source by name - *"Managing agency resolved from PAD-US -> NPS Alerts
API"* - and it is the one adapter in M4 keyed on a **PAD-US agency** rather than a census
boundary. `padus:NPS` resolves for every national park in the country, which is what makes
one adapter cover Yosemite and Acadia alike; `FEDERAL_AGENCY_CODES` exists so that id is not
fragmented by state.

Tier 3 rather than 2, and the distinction is real. A WZDx feed is a structured description
of a work zone with a geometry and a window. An NPS alert is a **title and a paragraph of
prose** with a park code and no geometry at all - `category` is one of a handful of values
and everything else a reader needs is in the body. That is why ADR 0013 keeps
`trail_status` soft: sorting "muddy in places" from "bridge out" is the act scope 3.2
forbids a scorer.

**An alert with no geometry is about the whole park**, and the route is in the park - that
is why it resolved as a jurisdiction. `trail_status._place` treats an empty geometry as
being on the route rather than discarding it, which is the only reading that does not throw
away every NPS alert there is.

**Which park** (M18, ADR 0054). `padus:NPS` names the agency, not the park, and until M18
this adapter asked for no park at all - the first fifty alerts in the country, every one of
them then read as being about any NPS route. The parks asked are the units whose boundary
boxes the corridor meets (`nps_units`), as the API's codes, **one request per park**: the
API reads only the first code of a list, so `parkCode=yose,ozar` answers for Yosemite alone
and `kica,ozar` for nobody (checked 2026-10-23).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from longrun.adapters.base import AdapterResult, describe
from longrun.adapters.keys import NPS

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable
    from datetime import date

    from longrun.adapters.base import AdapterContext
    from longrun.core.models.features import Feature

URL = "https://developer.nps.gov/api/v1/alerts"

#: Alerts per request. The API pages, and a national park has tens of alerts, not thousands.
PAGE_SIZE = 50

#: Pages read for one park before the answer is refused rather than truncated. Five pages
#: is 250 records, an order of magnitude more than any park has posted.
MAX_PAGES = 5

HTTP_TIMEOUT_S = 20.0

#: Tier 3 is brittle by design (scope 7.10). Above `MAX_EXTRACTION_CONFIDENCE` because a
#: park's own alerts page is not an LLM's reading of one, and below
#: `closures.HARD_FLAG_CONFIDENCE` because prose is prose - which is belt and braces, since
#: `MIN_HARD_FLAG_TIER` already forbids a tier-3 record from hard-flagging.
NPS_CONFIDENCE = 0.7

#: PAD-US put NPS land in the corridor and no unit's boundary box meets it. A sentence and
#: not an empty answer: nobody was asked, so nothing can be said to have been found.
NO_UNIT_REASON = "the corridor meets no NPS unit's boundary; there is no park to ask"


def nps_args(
    park_codes: list[str] | None, day: date | str, adapter: str = "portal.nps"
) -> dict[str, Any]:
    """The cache key, as a named function so a hash-stability test can pin it."""
    return {"adapter": adapter, "parks": sorted(park_codes or []), "day": str(day)}


def park_codes(polygon: Any) -> list[str]:
    """The API's park codes for the NPS units a corridor's box meets (M18, ADR 0054)."""
    from longrun.adapters.portals.nps_units import api_codes, units_meeting

    try:
        west, south, east, north = (float(v) for v in polygon.bounds)
    except Exception:  # noqa: BLE001 - no corridor, no parks to ask
        return []
    return api_codes(units_meeting((west, south, east, north)))


def per_park(
    adapter: str,
    url: str,
    codes: list[str],
    day: date | str,
    ctx: AdapterContext,
    *,
    params: dict[str, Any] | None = None,
) -> list[Any] | AdapterResult:
    """One payload per park code - fetched, or replayed when no key is set - or the reason
    none could be had. Cached per park, so two routes meeting one park share its answer.

    Paged rather than truncated: a park with more than `MAX_PAGES` pages is a reason,
    because an answer missing its last page reads exactly like one that had no more.
    """
    from longrun.core.data.cache import fetch, peek

    tool = f"adapter.{adapter}"
    key = NPS.value()
    payloads: list[Any] = []
    for code in codes:
        args = nps_args([code], day, adapter)
        if key is None:
            # Peek before refusing (M17): NPS data is US public domain, so a recorded payload
            # may be committed, and a cassette CI could never replay is no cassette at all.
            recorded = peek(ctx.cache, tool, args, day)
            if recorded is None:
                return AdapterResult(reason=NPS.missing_reason(), source_url=url, key_missing=True)
            payloads.append(recorded)
            continue
        try:
            payloads.append(
                fetch(ctx.cache, tool, args, day, _pages(url, code, key, ctx, params or {}))
            )
        except Exception as exc:  # noqa: BLE001 - a portal that is down is a reason
            return AdapterResult(reason=f"{code}: {describe(exc)}", source_url=url)
    return payloads


def _pages(
    url: str, code: str, key: str, ctx: AdapterContext, params: dict[str, Any]
) -> Callable[[], Any]:
    """The producer for one park: every page of its records, or an error."""

    def produce() -> Any:
        import httpx

        records: list[Any] = []
        total: int | None = None
        for _ in range(MAX_PAGES):
            ctx.budget.spend_api_call()
            response = httpx.get(
                url,
                params={"parkCode": code, "limit": PAGE_SIZE, "start": len(records), **params},
                headers={"X-Api-Key": key},
                timeout=HTTP_TIMEOUT_S,
                follow_redirects=True,
            )
            response.raise_for_status()
            page = response.json()
            data = page.get("data") if isinstance(page, dict) else None
            if not isinstance(data, list):
                break
            records.extend(data)
            total = int(page.get("total") or 0)
            if not data or len(records) >= total:
                break
        if total is not None and len(records) < total:
            raise RuntimeError(f"{code} has {total} records, more than {MAX_PAGES} pages")
        return {"data": records, "total": len(records)}

    return produce


def parse_alerts(payload: Any, *, source_url: str | None = None) -> list[Feature]:
    """NPS alert records as `Feature`s. Never raises.

    No start or end: the API publishes `lastIndexedDate` and nothing bounding when an alert
    applies. Both left `None`, which `Feature.active_at` reads as unbounded - a standing
    alert with no stated end is one nobody can say has lifted, and inventing a window would
    be inventing the fact that it expires.
    """
    from longrun.core.models.features import Feature

    if not isinstance(payload, dict):
        return []
    records = payload.get("data")
    if not isinstance(records, list):
        return []

    out: list[Feature] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        title = str(record.get("title") or "").strip()
        body = str(record.get("description") or "").strip()
        if not title and not body:
            continue
        category = str(record.get("category") or "Information").strip().lower()
        try:
            out.append(
                Feature(
                    kind="trail_status",
                    category=category,
                    geometry={},
                    tier=3,
                    confidence=NPS_CONFIDENCE,
                    jurisdiction="padus:NPS",
                    source_url=str(record.get("url") or "") or source_url,
                    detail=" - ".join(p for p in (title, body) if p) or None,
                    ref=str(record.get("id") or "") or None,
                )
            )
        except ValueError:  # pragma: no cover - defensive
            continue
    return out


class NpsTrailStatus:
    name = "portal.nps"
    kind = "trail_status"
    tier = 3
    jurisdictions = ("padus:NPS",)
    source = "nps"
    key = NPS
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        codes = park_codes(polygon)
        if not codes:
            if NPS.value() is None:
                return AdapterResult(reason=NPS.missing_reason(), source_url=URL, key_missing=True)
            return AdapterResult(reason=NO_UNIT_REASON, source_url=URL)
        found = per_park(self.name, URL, codes, day, ctx)
        if isinstance(found, AdapterResult):
            return found
        features = [f for payload in found for f in parse_alerts(payload, source_url=URL)]
        return AdapterResult(features=features, vintage="nps-alerts", source_url=URL)


TRAIL_STATUS = NpsTrailStatus()


# --- access_hours: a park's own hours (M18) ----------------------------------------------

PARKS_URL = "https://developer.nps.gov/api/v1/parks"

_DAYS = (
    ("monday", "Mo"),
    ("tuesday", "Tu"),
    ("wednesday", "We"),
    ("thursday", "Th"),
    ("friday", "Fr"),
    ("saturday", "Sa"),
    ("sunday", "Su"),
)
_CLOCK = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*([AP])\.?M\.?$", re.IGNORECASE)
_SUN = {"sunrise": "sunrise", "sunset": "sunset", "dawn": "dawn", "dusk": "dusk"}


def _nps_time(text: str) -> str | None:
    """`6:00AM` -> `06:00`, `Sunset` -> `sunset`; `None` when it is neither."""
    word = text.strip().lower()
    if word in _SUN:
        return _SUN[word]
    match = _CLOCK.match(text.strip())
    if match is None:
        return None
    hour, minute = int(match.group(1)) % 12, int(match.group(2) or 0)
    if match.group(3).upper() == "P":
        hour += 12
    return f"{hour:02d}:{minute:02d}"


def nps_day_hours(value: Any) -> str:
    """One day of an NPS `standardHours` as an `opening_hours` time selector.

    The forms the API carries (counted over ten parks on 2026-10-23): `All Day`, `Closed`,
    `Sunrise to Sunset`, `10:00AM - 5:00PM`, `6:00AM - Sunset`. Anything else is `unknown`,
    which `_hours` reads as not understood rather than closed - an unreadable day is not a
    locked gate.
    """
    text = str(value or "").strip()
    lowered = text.lower()
    if lowered == "all day":
        return "00:00-24:00"
    if lowered == "closed":
        return "off"
    parts = re.split(r"\s+(?:-|to|–)\s+|\s*-\s*", text, maxsplit=1)
    if len(parts) == 2:
        start, end = _nps_time(parts[0]), _nps_time(parts[1])
        if start and end:
            return f"{start}-{end}"
    return "unknown"


def nps_hours_rule(hours: Any) -> str:
    """A week of NPS hours as one `opening_hours` rule, a day per `;`-part.

    A day per part rather than grouped (`Mo-Fr ...`) because each part then selects exactly
    one weekday, so no part can replace another (OSM's rule order) and the rule reads back
    the way the API published it. An empty week is a closure - that is how the API writes
    a dated exception that shuts the park.
    """
    if not isinstance(hours, dict) or not hours:
        return "off"
    return "; ".join(f"{short} {nps_day_hours(hours.get(day))}" for day, short in _DAYS)


def _is_the_park(entry_name: Any, full_name: str) -> bool:
    """Whether an `operatingHours` entry is the park's own rather than a facility's.

    A visitor centre's hours are not the park's gate hours, and GOGA posts only facilities
    (parking lots, day-use areas, Muir Woods). Containment either way, because North
    Cascades posts its hours as "North Cascades National Park Service Complex".
    """
    one = re.sub(r"[^a-z0-9 ]", "", str(entry_name or "").lower()).strip()
    other = re.sub(r"[^a-z0-9 ]", "", full_name.lower()).strip()
    return bool(one and other) and (one in other or other in one)


def parse_park_hours(payload: Any, *, source_url: str | None = None) -> list[Feature]:
    """A park's own hours as `access_hours` rules: one standing, one per dated exception.

    Park-wide (`{}` geometry), so `access_hours` reads them where the route enters NPS land
    (M18.7). A dated exception's days are local dates, converted to the instants its
    validity is stored in through the park's own zone (ADR 0046) - and skipped when no zone
    can be found for the park, since a window on an unknown clock is no window.
    """
    from longrun.core.models.features import Feature

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []
    out: list[Feature] = []
    for park in payload["data"]:
        if not isinstance(park, dict):
            continue
        full_name = str(park.get("fullName") or "").strip()
        zone = _park_zone(park)
        url = str(park.get("url") or "") or source_url
        for entry in park.get("operatingHours") or []:
            if not isinstance(entry, dict) or not _is_the_park(entry.get("name"), full_name):
                continue
            base: dict[str, Any] = {
                "kind": "access_hours",
                "category": "park hours",
                "geometry": {},
                "tier": 3,
                "confidence": NPS_CONFIDENCE,
                "jurisdiction": "padus:NPS",
                "name": full_name,
                "source_url": url,
            }
            out.append(
                Feature(
                    **base,
                    hours=nps_hours_rule(entry.get("standardHours")),
                    detail="standard hours",
                )
            )
            for exception in entry.get("exceptions") or []:
                window = _local_window(exception, zone) if isinstance(exception, dict) else None
                if window is None:
                    continue
                out.append(
                    Feature(
                        **base,
                        hours=nps_hours_rule(exception.get("exceptionHours")),
                        start=window[0],
                        end=window[1],
                        detail=str(exception.get("name") or "exception"),
                    )
                )
    return out


def _park_zone(park: dict[str, Any]) -> str | None:
    try:
        import tzfpy

        return tzfpy.get_tz(float(park["longitude"]), float(park["latitude"])) or None
    except Exception:  # noqa: BLE001 - no coordinate, or no zone covers it
        return None


def _local_window(exception: dict[str, Any], zone: str | None) -> tuple[Any, Any] | None:
    from datetime import date as calendar
    from datetime import datetime, time

    from longrun.adapters.base import local_to_utc

    if zone is None:
        return None
    try:
        first = calendar.fromisoformat(str(exception["startDate"])[:10])
        last = calendar.fromisoformat(str(exception["endDate"])[:10])
    except (KeyError, ValueError):
        return None
    if last < first:
        return None
    return (
        local_to_utc(datetime.combine(first, time()), zone),
        local_to_utc(datetime.combine(last, time(23, 59, 59)), zone),
    )


class NpsAccessHours:
    """A park's own hours, for `access_hours` (M18).

    Hours change by season, not by the hour, so the answer is cached per ISO week (keyed by
    its Monday) rather than per day - a week of plans in one park asks once.
    """

    name = "portal.nps_hours"
    kind = "access_hours"
    tier = 3
    jurisdictions = ("padus:NPS",)
    source = "nps"
    key = NPS
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        from datetime import timedelta

        codes = park_codes(polygon)
        if not codes:
            if NPS.value() is None:
                return AdapterResult(
                    reason=NPS.missing_reason(), source_url=PARKS_URL, key_missing=True
                )
            return AdapterResult(reason=NO_UNIT_REASON, source_url=PARKS_URL)
        monday = day - timedelta(days=day.weekday())
        found = per_park(
            self.name, PARKS_URL, codes, monday, ctx, params={"fields": "operatingHours"}
        )
        if isinstance(found, AdapterResult):
            return found
        features = [f for payload in found for f in parse_park_hours(payload, source_url=PARKS_URL)]
        return AdapterResult(features=features, vintage="nps-park-hours", source_url=PARKS_URL)


ACCESS_HOURS = NpsAccessHours()

__all__ = [
    "ACCESS_HOURS",
    "MAX_PAGES",
    "NO_UNIT_REASON",
    "NPS_CONFIDENCE",
    "PAGE_SIZE",
    "PARKS_URL",
    "TRAIL_STATUS",
    "URL",
    "NpsAccessHours",
    "NpsTrailStatus",
    "nps_args",
    "nps_day_hours",
    "nps_hours_rule",
    "park_codes",
    "parse_alerts",
    "parse_park_hours",
    "per_park",
]
