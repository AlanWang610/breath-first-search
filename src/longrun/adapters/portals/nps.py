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

__all__ = [
    "MAX_PAGES",
    "NO_UNIT_REASON",
    "NPS_CONFIDENCE",
    "PAGE_SIZE",
    "TRAIL_STATUS",
    "URL",
    "NpsTrailStatus",
    "nps_args",
    "park_codes",
    "parse_alerts",
    "per_park",
]
