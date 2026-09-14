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
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.base import AdapterResult, describe
from longrun.adapters.keys import NPS

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext
    from longrun.core.models.features import Feature

URL = "https://developer.nps.gov/api/v1/alerts"

#: Alerts per request. The API pages, and a national park has tens of alerts, not thousands.
PAGE_SIZE = 50

HTTP_TIMEOUT_S = 20.0

#: Tier 3 is brittle by design (scope 7.10). Above `MAX_EXTRACTION_CONFIDENCE` because a
#: park's own alerts page is not an LLM's reading of one, and below
#: `closures.HARD_FLAG_CONFIDENCE` because prose is prose - which is belt and braces, since
#: `MIN_HARD_FLAG_TIER` already forbids a tier-3 record from hard-flagging.
NPS_CONFIDENCE = 0.7


def nps_args(park_codes: list[str] | None, day: date | str) -> dict[str, Any]:
    """The cache key, as a named function so a hash-stability test can pin it."""
    return {"adapter": "portal.nps", "parks": sorted(park_codes or []), "day": str(day)}


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
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        from longrun.core.data.cache import fetch

        key = NPS.value()
        if key is None:
            return AdapterResult(reason=NPS.missing_reason(), source_url=URL)

        def produce() -> Any:
            import httpx

            ctx.budget.spend_api_call()
            response = httpx.get(
                URL,
                params={"limit": PAGE_SIZE},
                headers={"X-Api-Key": key},
                timeout=HTTP_TIMEOUT_S,
                follow_redirects=True,
            )
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {"data": []}

        try:
            payload = fetch(ctx.cache, f"adapter.{self.name}", nps_args(None, day), day, produce)
        except Exception as exc:  # noqa: BLE001 - a portal that is down is a reason
            return AdapterResult(reason=describe(exc), source_url=URL)

        return AdapterResult(
            features=parse_alerts(payload, source_url=URL), vintage="nps-alerts", source_url=URL
        )


TRAIL_STATUS = NpsTrailStatus()

__all__ = [
    "NPS_CONFIDENCE",
    "PAGE_SIZE",
    "TRAIL_STATUS",
    "URL",
    "NpsTrailStatus",
    "nps_args",
    "parse_alerts",
]
