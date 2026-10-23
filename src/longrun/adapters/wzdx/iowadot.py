"""Iowa DOT's statewide WZDx feed (scope 7.6, 7.10).

Tier 1, no key, WZDx 4.0. **No cassette**: `feed_info.license` is absent (ADR 0038).

The absence is worth one sentence more than usual here, because Iowa DOT does run an open
data portal and it does mention CC0. That is a statement about the portal's datasets, not
about this endpoint, which is served from the state's ATMS vendor host and carries no
licence of its own. ADR 0038 declines to reason from one to the other: a grant on a
different page for different files is not a grant for this payload, and guessing would put
somebody else's data in a committed fixture.

Called 2026-09-23: **HTTP 200, 1,417,338 bytes, 1,088 features, 1,088 parsed**, publisher
"Iowa DOT", envelope seconds old. Impact is stated on every record and there is no third
category: **685 `all-lanes-open` and 403 `some-lanes-closed`, nothing else**. So Iowa
reports work zones and none of them can ever clear ADR 0013's gate 1 on impact - not
because the publisher declined to say, as in Maricopa County, but because it says the road
is open.

**4.0 with the old envelope key**, like Kansas and Indiana and unlike North Dakota's 4.0.
See `nddot.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed live and keyless on 2026-09-23: WZDx 4.0, no declared licence, 1,088 features.
URL = "https://iowa-atms.cloud-q-free.com/api/rest/dataprism/wzdx/wzdxfeed"


class IowaDotClosures:
    name = "wzdx.iowadot"
    kind = "closures"
    tier = 1
    scope = "feed"
    jurisdictions = ("tiger:state:19",)
    source = "wzdx_iowadot"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = IowaDotClosures()

__all__ = ["CLOSURES", "URL", "IowaDotClosures"]
