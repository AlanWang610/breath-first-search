"""The National Park Service's road events, a WZDx feed of park roads (scope 7.6, 7.10).

Tier 1, keyed with the same NPS key `portal.nps` uses, WZDx 4.1, **CC0 1.0 declared in the
envelope** (ADR 0038), so it backs a cassette that replays with no key.

Called 2026-10-28 with the key, asking for every event type at once: **HTTP 200, 515,425
bytes, 114 features, all 114 parsed**, publisher "National Park Service". 59 are work zones
and 55 are incidents. The registry's URL asks for `type=workzone` alone, but an incident
here is a road shut by rockfall or snow, which is a closure to someone running it. All 114
were active at the call. Without a key: **HTTP 403**.

**These are park roads and trailheads, closed outright.** 108 of the 114 are
`all-lanes-closed`, for example "Death Canyon Road and Trailhead are closed to all use",
plus 4 `some-lanes-closed` and 2 `alternating-one-way`. They come from 13 parks:
- Mount Rainier, 52;
- Golden Gate National Recreation Area, 27;
- Delaware Water Gap, 8;
- Grand Teton, 6;
- the Great Smoky Mountains, 5;
- the Natchez Trace, 4;
- the Ozark National Scenic Riverways and five others, 1 or 2 each.

81 of them clear ADR 0013's gates 1 and 4, so on a route that runs along one they can
fail it, which is what this feed is for.

**The envelope's `update_date` is 2024-01-16, and the records are current.** M14 refused
Hawaii and Utah because their envelopes were stale *and* every record had expired. Here the
newest event began 2026-10-26, 91 began in 2026, and the parks' own data sources were
updated as recently as 2026-04-02. The envelope is unmaintained; the feed is not.

Registered against `padus:NPS`, like `portal.nps`: one national request answers for every
park a route crosses, and the corridor is applied client-side as for every WZDx feed. A
state DOT's feed also answers inside a park where the park lies in its state, so a route
through the Ozarks is asked of MoDOT and of this. That is two networks, park roads and
state roads, not two answers to one question.

The key travels in the `X-Api-Key` header, as for `portal.nps`. The records carry their id
as `properties.Id` rather than WZDx's feature-level `id`, so `Feature.ref` is empty.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.base import AdapterResult
from longrun.adapters.keys import NPS
from longrun.adapters.wzdx.client import fetch_feed, replay_or_refuse

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext

#: Confirmed with the NPS key on 2026-10-28: WZDx 4.1, CC0 1.0, 114 features.
URL = "https://developer.nps.gov/api/v1/roadevents"


class NpsRoadClosures:
    name = "wzdx.nps"
    kind = "closures"
    tier = 1
    scope = "feed"
    jurisdictions = ("padus:NPS",)
    source = "wzdx_nps"
    key = NPS
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        value = NPS.value()
        if value is None:
            return replay_or_refuse(URL, self.name, day, ctx, NPS)
        return fetch_feed(URL, self.name, day, ctx, headers={"X-Api-Key": value})


CLOSURES = NpsRoadClosures()

__all__ = ["CLOSURES", "URL", "NpsRoadClosures"]
