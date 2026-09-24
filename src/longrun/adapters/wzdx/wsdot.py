"""Washington State DOT's WZDx feed (scope 7.6, 7.10).

Tier 1, no key, WZDx 4.2. **No cassette**: `feed_info.license` is absent (ADR 0038), and
the data-catalog page the agency publishes could not be read - `wsdot.wa.gov` answered
HTTP 404 for it on 2026-09-23. An unreachable terms page is not a grant either.

Called 2026-09-23: **HTTP 200, 1,226,248 bytes, 590 features, 590 parsed**, publisher
"Washington State DOT IT", envelope four hours old. **All 590 categorise as `work-zone`**:
the impact field says nothing on any record, so Washington joins Kentucky, New York and
Maricopa County in the set of publishers whose work zones are reported and cannot clear
ADR 0013's gate 1 on impact alone.

Both of the feed's `data_sources` are named `Unknown`, which is the publisher declining to
say where its records come from. Nothing in the adapter depends on it; noted because a
`data_sources` entry is where the CC0 feeds name their originating agency, and reading
this one as a redistributor's feed rather than the agency's own would be a guess.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed live and keyless on 2026-09-23: WZDx 4.2, no declared licence, 590 features.
URL = "https://wzdx.wsdot.wa.gov/api/v4/WorkZoneFeed"


class WsDotClosures:
    name = "wzdx.wsdot"
    kind = "closures"
    tier = 1
    scope = "feed"
    jurisdictions = ("tiger:state:53",)
    source = "wzdx_wsdot"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = WsDotClosures()

__all__ = ["CLOSURES", "URL", "WsDotClosures"]
