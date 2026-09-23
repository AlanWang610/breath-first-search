"""Louisiana DOTD's statewide WZDx feed (scope 7.6, 7.10).

Tier 1, no key, WZDx 4.1, **CC0 1.0 declared in the envelope** (ADR 0038). Same publisher
and the same permit-feed shape as Delaware's - `deldot.py` carries the long version of why
a feed that empties out every few hours is still worth having.

Called 2026-09-23: **HTTP 200, 13,009 bytes, 2 features, 2 parsed**, both west of Baton
Rouge, both ending within two hours of the call. Two is the whole statewide feed.

**Two is not zero, and this adapter exists to keep the difference.** Without it Louisiana
reports "no closure adapter"; with it, a Louisiana plan reports that the state's feed was
read and says what it said. That is ADR 0013's coverage argument applied to the smallest
feed in the set, and the smallest is the better test of it - nobody is tempted to call a
4,000-feature feed not worth wiring up.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed live and keyless on 2026-09-23: WZDx 4.1, CC0 1.0, 2 features.
URL = "https://wzdx.e-dot.com/la_dot_d_feed_wzdx_v4.1.geojson"


class LaDotdClosures:
    name = "wzdx.ladotd"
    kind = "closures"
    tier = 1
    jurisdictions = ("tiger:state:22",)
    source = "wzdx_ladotd"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = LaDotdClosures()

__all__ = ["CLOSURES", "URL", "LaDotdClosures"]
