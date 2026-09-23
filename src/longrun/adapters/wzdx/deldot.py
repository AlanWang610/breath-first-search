"""Delaware DOT's statewide WZDx feed (scope 7.6, 7.10).

Tier 1, no key, WZDx 4.1, **CC0 1.0 declared in the envelope** (ADR 0038), so it backs a
cassette.

Called 2026-09-23: **HTTP 200, 37,474 bytes, 12 features, 12 parsed** - and every one of
them ended within two hours of the call, the latest at 06:20 UTC against a fetch at 04:21.
This is a *permit* feed: it publishes the work happening now, not a programme of scheduled
projects, so a Delaware plan for next week will read it and correctly find nothing.

That is worth writing down because it is the benign version of the failure `massdot.py:28`
names. A feed whose whole contents expire within hours looks like a state with no work
zones on every day but today, and the difference between "nothing is happening" and
"nothing was published" is exactly what the coverage manifest exists to keep apart. Here
it is the former: the envelope's `update_date` was four minutes old when this was called.
Hawaii's and Utah's feeds are the other case, and M14 refused both - see
`adapters/wzdx/__init__.py`.

**Published by HaulHub Technologies, not by DelDOT directly.** The feed's `data_sources`
names "Delaware Department of Transportation" as the originating agency and HaulHub as the
publisher, so the CC0 dedication is the vendor's, on the agency's data. Recorded because
the attribution line names the agency and the licence comes from the vendor, and a reader
who checks one will not find the other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed live and keyless on 2026-09-23: WZDx 4.1, CC0 1.0, 12 features.
URL = "https://wzdx.e-dot.com/del_dot_feed_wzdx_v4.1.geojson"


class DelDotClosures:
    name = "wzdx.deldot"
    kind = "closures"
    tier = 1
    jurisdictions = ("tiger:state:10",)
    source = "wzdx_deldot"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = DelDotClosures()

__all__ = ["CLOSURES", "URL", "DelDotClosures"]
