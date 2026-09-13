"""Kansas DOT's WZDx feed (scope 7.6, 7.10, 11 region 4).

Tier 1, no key, CC0, and **WZDx 4.0** - the oldest of the three versions this project reads,
which is why `feed.parse_wzdx` looks for `road_names` both inside `core_details` and flat on
`properties`. 4.1 introduced that nesting; Kansas has not moved.

The URL is also the concrete form of a risk worth naming. The USDOT feed registry publishes
`ks.carsprogram.org`, which 301-redirects here. The registry `adapters/wzdx/__init__.py`
says to consult before relying on a feed is itself stale.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed live and keyless on 2026-09-10: WZDx 4.0, CC0, 64 features, publisher
#: "KDOTCastleRock". Reached by following the registry's 301 rather than trusting it.
URL = "https://kscars.kandrive.gov/carsapi_v1/api/wzdx"


class KDotClosures:
    name = "wzdx.kdot"
    kind = "closures"
    tier = 1
    jurisdictions = ("tiger:state:20",)
    source = "wzdx_kdot"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = KDotClosures()

__all__ = ["CLOSURES", "URL", "KDotClosures"]
