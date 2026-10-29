"""Illinois DOT's statewide CWZ feed (scope 7.6, 7.10).

Tier 1, keyed, **CWZ 1.0**, read by the shared WZDx reader (ADR 0058), and **CC0 1.0
declared in the envelope** (ADR 0038), so it backs a cassette that replays with no key.

Called 2026-10-28 with a key: **HTTP 200, 1,713,288 bytes, 800 features, all 800 parsed**,
publisher "Illinois Department of Transportation", envelope seconds old, every record
active at the call. Without a key: **HTTP 422**, the query parameter `api_key` required.

**It carries the Illinois Tollway.** The envelope lists four data sources, and the
Tollway's Traffic Center is one of them: 103 of the 800 records, beside IDOT's own
operations events (624) and two road-work systems (73). The Tollway's own WZDx feed still
fails its certificate check and waits in `AWAITING_KEY`. Until then its roads are answered
here, which is why that entry says to adopt it with `complete = False`.

**The impact is mostly unstated.** 697 records publish `vehicle_impact: "unknown"`, and the
category falls back to the event type, `work-zone`. That is Maricopa County's position
(`feed.UNINFORMATIVE`): those work zones are reported and can clear ADR 0013's gate 1 only
if their description names a sidewalk. The rest: 97 `some-lanes-closed`, 4
`all-lanes-closed`, 2 `all-lanes-open`. 400 records run longer than 180 days, standing
conditions under gate 4, and 51 are single points.

**The key travels as a query parameter**, the only form the endpoint accepts. A refused
request still never shows it: `http.get_json` raises `FeedRefused`, which names the status,
host and path and nothing in the query (M20).
The env var is `LONGRUN_IDOT_API_KEY`, the name it arrived under. The registry's `idot` is
Iowa's keyless feed (`iowadot.py`), so the variable says "Illinois" in its service name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.base import AdapterResult
from longrun.adapters.keys import IDOT
from longrun.adapters.wzdx.client import fetch_feed, replay_or_refuse

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext

#: Confirmed with a key on 2026-10-28: CWZ 1.0, CC0 1.0, 800 features.
URL = "https://wzad-idot.illinois.gov/cwz/01.00"


class IlDotClosures:
    name = "wzdx.ildot"
    kind = "closures"
    tier = 1
    scope = "feed"
    jurisdictions = ("tiger:state:17",)
    source = "wzdx_ildot"
    key = IDOT
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        value = IDOT.value()
        if value is None:
            return replay_or_refuse(URL, self.name, day, ctx, IDOT, spec="cwz")
        return fetch_feed(URL, self.name, day, ctx, params={"api_key": value}, spec="cwz")


CLOSURES = IlDotClosures()

__all__ = ["CLOSURES", "URL", "IlDotClosures"]
