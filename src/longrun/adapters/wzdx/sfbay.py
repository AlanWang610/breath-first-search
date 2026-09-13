"""511 SF Bay's WZDx feed (scope 7.6, 7.10).

**Tier 1**, because scope 7.10 defines the tier by *source type* - "1 WZDx, 2 511 API, 3
open-data portal, 4 LLM extraction" - and this is a WZDx feed served through the same parser
as Missouri's. It shipped at tier 2, which conflated "needs a key" with "is a worse kind of
source". A credential is a reason a fetch *failed*; it says nothing about what the data is.

That mistake had teeth. Tier decides the ladder, and `MIN_HARD_FLAG_TIER` is 2 - so a
mis-tiered feed changes which sources may fail a route, not merely how the sheet reads.

What is true is that reaching it needs a key, so the Bay Area reported no closures until one
was supplied. Confirmed working on 2026-09-11: **954 work zones**, WZDx 4.1.

`bayarea.yaml` already records the same fact about transit: *"511.org needs a key and the
direct agency URLs are not all stable"*, so BART is the only feed loaded. The coverage
manifest is where that distinction has to survive, there and here.

Registered against the nine Bay Area counties rather than California, because MTC's remit
is the region and Caltrans' is the state. Claiming `tiger:state:06` would have this adapter
answer for San Diego.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.base import AdapterResult
from longrun.adapters.keys import SF_BAY_511
from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext

URL = "https://api.511.org/traffic/wzdx"

#: The nine counties MTC covers: San Francisco, San Mateo, Santa Clara, Alameda, Contra
#: Costa, Solano, Napa, Sonoma, Marin.
BAY_AREA_COUNTIES = (
    "tiger:county:06075",
    "tiger:county:06081",
    "tiger:county:06085",
    "tiger:county:06001",
    "tiger:county:06013",
    "tiger:county:06095",
    "tiger:county:06055",
    "tiger:county:06097",
    "tiger:county:06041",
)


class SfBay511Closures:
    name = "wzdx.sfbay"
    kind = "closures"
    tier = 1
    jurisdictions = BAY_AREA_COUNTIES
    source = "wzdx_sfbay"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        key = SF_BAY_511.value()
        if key is None:
            return AdapterResult(reason=SF_BAY_511.missing_reason(), source_url=URL)
        return fetch_feed(URL, self.name, day, ctx, params={"api_key": key})


CLOSURES = SfBay511Closures()

__all__ = ["BAY_AREA_COUNTIES", "CLOSURES", "URL", "SfBay511Closures"]
