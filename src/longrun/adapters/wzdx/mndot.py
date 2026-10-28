"""Minnesota DOT's statewide WZDx feed (scope 7.6, 7.10).

Tier 1, no key, WZDx 4.0, **CC0 1.0 declared in the envelope** (ADR 0038), so it backs a
cassette.

**Refused by M14, adopted by M19, on the same rule.** On 2026-09-23 it raised `ReadTimeout`
at 60 seconds, twice, six minutes apart. On 2026-10-28 it answered twice, the second time
in under four seconds: **HTTP 200, 811,015 bytes, 404 features, all 404 parsed**, publisher
"MnDOTCastleRock", envelope minutes old, 352 records active at the call.

It is a live programme, not a frozen file like Hawaii's (see `__init__.py`). Its 404 records
carry 160 distinct windows, and 227 of them were updated in October 2026. Impact is stated
on 115:
- 72 `all-lanes-closed`, 40 `some-lanes-closed`, 2 `flagging`, 1 `temporary-traffic-signal`;
- 189 say `unknown` and fall back to the event type `work-zone`;
- 100 are detours with no impact field at all.

137 records run longer than ADR 0013's 180 days. 50 of the closed ones are events inside
it, so this feed can fail a route.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed live and keyless on 2026-10-28: WZDx 4.0, CC0 1.0, 404 features.
URL = "https://mn.carsprogram.org/carsapi_v1/api/wzdx"


class MnDotClosures:
    name = "wzdx.mndot"
    kind = "closures"
    tier = 1
    scope = "feed"
    jurisdictions = ("tiger:state:27",)
    source = "wzdx_mndot"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = MnDotClosures()

__all__ = ["CLOSURES", "URL", "MnDotClosures"]
