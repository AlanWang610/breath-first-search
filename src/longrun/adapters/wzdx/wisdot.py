"""Wisconsin DOT's statewide WZDx feed (scope 7.6, 7.10).

Tier 1, no key, WZDx 4.2, **CC0 1.0 declared in the envelope** (ADR 0038), so it backs a
cassette.

Called 2026-09-23: **HTTP 200, 9,387,857 bytes, 4,180 features, 4,180 parsed**, publisher
"Work Zone Manager", envelope two minutes old. It is the richest feed in the set on
impact - 2,735 `some-lanes-closed`, 740 `flagging`, 371 `all-lanes-closed`, 243 `detour`,
50 `temporary-traffic-signal`, 40 `alternating-one-way`, and **nothing falling back to the
event type**. Wisconsin states the impact on every single record, which no other feed M14
called does.

**9.4 MB is the second-largest payload this project fetches**, and it is worth saying what
that costs and does not cost. `client.fetch_feed` pulls the whole statewide file because
none of these endpoints accepts a spatial filter, and one fetch then answers for every
county and place in the state (`registry._plan` fans out by adapter). So the cost is one
request against `MAX_ADAPTER_FETCHES = 24` and about ten megabytes in the cache, once per
day per region - not per jurisdiction. Florida's feed was refused partly on this axis and
the difference is two orders of magnitude, not a judgement call: see
`adapters/wzdx/__init__.py`.

The feed's `data_sources` carries two entries with empty organisation names, so there is
no originating agency to attribute beyond WisDOT itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed live and keyless on 2026-09-23: WZDx 4.2, CC0 1.0, 4,180 features.
URL = "https://511wi.gov/api/wzdx"


class WisDotClosures:
    name = "wzdx.wisdot"
    kind = "closures"
    tier = 1
    jurisdictions = ("tiger:state:55",)
    source = "wzdx_wisdot"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = WisDotClosures()

__all__ = ["CLOSURES", "URL", "WisDotClosures"]
