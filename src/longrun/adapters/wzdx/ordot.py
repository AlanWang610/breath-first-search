"""Oregon DOT's statewide WZDx feed (scope 7.6, 7.10).

Tier 1, keyed, WZDx 4.0, **CC0 1.0 declared in the envelope** (ADR 0038), so it backs a
cassette. The licence travels with the payload, not with whoever fetched it, so a recording
made with a key replays for everybody without one (`client.replay_or_refuse`).

Called 2026-10-28 with a key: **HTTP 200, 297,992 bytes, 171 features, 171 parsed**,
publisher "Oregon-DOT", envelope (`road_event_feed_info`) one minute old, 158 records active
at the time of the call. Impact is stated on every record: 123 `all-lanes-open` and 48
`some-lanes-closed`, nothing falling back to the event type. Without a key: **HTTP 401**,
"Access denied due to missing subscription key".

**Oregon publishes projects, not nightly closures.** The earliest record began 2022-05-27
and the latest ends 2030-11-10; 79 of the 171 run longer than ADR 0013's 180 days and so are
standing conditions, reported and never failing a route. Every record is a `MultiPoint` of
the zone's two ends, which Wisconsin publishes too and `closures.runs_along` reads as the
line between them.

**Which key this is was found by calling, not by reading.** It arrived as
`LONGRUN_ODOT_API_KEY`, and three DOTs answer to ODOT. Ohio's feed is OHGO's, Oklahoma's
answered **HTTP 403** to it, and Oregon's answered 200. So it is read as
`LONGRUN_ORDOT_API_KEY`, after the registry's own `ordot`, and the name now says which state
it opens.

The key travels in Azure API Management's `Ocp-Apim-Subscription-Key` header, so it never
reaches a URL, a cache key or an error message.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.base import AdapterResult
from longrun.adapters.keys import ORDOT
from longrun.adapters.wzdx.client import fetch_feed, replay_or_refuse

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext

#: Confirmed with a key on 2026-10-28: WZDx 4.0, CC0 1.0, 171 features.
URL = "https://api.odot.state.or.us/WZDx/v4.0/Workzones"


class OrDotClosures:
    name = "wzdx.ordot"
    kind = "closures"
    tier = 1
    scope = "feed"
    jurisdictions = ("tiger:state:41",)
    source = "wzdx_ordot"
    key = ORDOT
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        value = ORDOT.value()
        if value is None:
            return replay_or_refuse(URL, self.name, day, ctx, ORDOT)
        return fetch_feed(URL, self.name, day, ctx, headers={"Ocp-Apim-Subscription-Key": value})


CLOSURES = OrDotClosures()

__all__ = ["CLOSURES", "URL", "OrDotClosures"]
