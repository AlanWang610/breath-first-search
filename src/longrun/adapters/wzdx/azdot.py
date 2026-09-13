"""Arizona DOT's statewide WZDx feed (scope 7.6, 7.10).

Tier 1, **no key**, WZDx 4.1, publisher `AZDOT`. Confirmed live on 2026-09-11 with a clean
environment: 4.6 MB, **3,423 work zones**, of which 38 are near Tucson and 2,012 near
Phoenix.

**This was shipped as a key-gated tier-2 adapter and that was wrong twice over.** The AZ511
developer documentation says *"A registered account is needed before you can sign up for a
Developer API key"* and *"The query string parameter `key` is required for most API calls"*,
and I took that to cover every endpoint. It does not cover this one: `/api/wzdx` answers
without any credential at all. Reading the documentation is not the same as calling the
endpoint, and only the second is evidence.

**It also states impact, which Maricopa County's feed does not.** MCDOT publishes
`vehicle_impact: "unknown"` on every one of its ~2,626 features; AZDOT states it properly,
and 121 of its 3,423 are `all-lanes-closed`.

Measured rather than assumed, because the first version of this note overclaimed. Asking
Phoenix of MCDOT alone yields **11 work zones that clear ADR 0013's gate 1** - not zero -
because `blocks_pedestrians` falls back to the description when the impact field says
nothing, and a handful of MCDOT descriptions do name a sidewalk closure. Asking both feeds
yields **143**. So the claim is thirteen times more gate-1 evidence, not the difference
between some and none.

The two feeds draw on disjoint `data_sources` (AZDOT's are ERS, Tucson and RADS; MCDOT's is
its own), so they are complementary rather than redundant, and the registry consults both.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed keyless 2026-09-11. The bare host, not `www` - `www.az511.com/api/wzdx` is a
#: different vhost and the developer pages under it are not a reliable guide (see below).
URL = "https://az511.com/api/wzdx"

#: Where the API is documented. `/developers` - which this adapter shipped pointing at -
#: 302s to `/notfound`; the real page is one level deeper. Recorded because the wrong URL is
#: what a reader follows when an adapter says it needs a key.
DOCS = "https://www.az511.com/developers/doc"


class AzDotClosures:
    name = "wzdx.azdot"
    kind = "closures"
    tier = 1
    jurisdictions = ("tiger:state:04",)
    source = "wzdx_azdot"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = AzDotClosures()

__all__ = ["CLOSURES", "DOCS", "URL", "AzDotClosures"]
