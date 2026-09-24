"""North Dakota DOT's statewide WZDx feed (scope 7.6, 7.10).

Tier 1, no key, and **CC0 1.0 declared in the feed's own envelope** - which is what ADR
0038 makes the test of whether a feed may be committed as a cassette. NDDOT passes it, so
this feed has a recorded parse path in CI rather than only a live call.

Called 2026-09-23: **HTTP 200, 307,333 bytes, 106 features, 106 parsed**, publisher
`NDDOT`, data source `NDDOT-WZDX`, envelope updated fourteen hours earlier. Impact is
stated properly - 63 `some-lanes-closed`, 37 `all-lanes-open`, 6 falling back to the event
type - so a North Dakota work zone can clear ADR 0013's gate 1, unlike any of Maricopa's.

**It declares 4.0 and uses the 4.1 envelope key**, which corrects a generalisation
`feed.py` invites. That file explains `ENVELOPE_KEYS` through Kansas: 4.1 renamed
`road_event_feed_info` to `feed_info` and "Kansas has not moved". The rename is not what
sorts publishers by version. NDDOT publishes 4.0 under `feed_info`; Indiana and Iowa
publish 4.0 under `road_event_feed_info`. Looking for both is load-bearing at every
version, not a compatibility shim for one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed live and keyless on 2026-09-23: WZDx 4.0, CC0 1.0, 106 features.
URL = "https://travelfiles.dot.nd.gov/geojson_nc/wzdx_geojson.json"


class NdDotClosures:
    name = "wzdx.nddot"
    kind = "closures"
    tier = 1
    scope = "feed"
    jurisdictions = ("tiger:state:38",)
    source = "wzdx_nddot"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = NdDotClosures()

__all__ = ["CLOSURES", "URL", "NdDotClosures"]
