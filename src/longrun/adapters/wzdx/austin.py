"""The City of Austin's WZDx feed (scope 7.6, 7.10).

Tier 1, no key, WZDx 4.2, **CC0 1.0 declared in the envelope** (ADR 0038), so it backs a
cassette.

Called 2026-09-23: **HTTP 200, 8,175,587 bytes, 4,056 features, 4,056 parsed**, publisher
"City of Austin", envelope twenty minutes old, bounded to lon -97.90..-97.61 / lat
30.14..30.48 - the city, not the state. Impact is stated on every record: 3,597
`some-lanes-closed` and 459 `all-lanes-closed`, nothing falling back to the event type.
The two data sources are the city's own AMANDA right-of-way and excavation permit systems,
which is why the density is municipal: these are street cuts, not highway projects.

**The first adapter registered against a `tiger:place`, and the reason is Texas.** TxDOT
publishes a statewide 4.2 feed and it answers **HTTP 401 Unauthorized** without a key
(called 2026-09-23), so there is no keyless statewide source to prefer. Austin therefore
gets tier-1 closures and the rest of Texas gets none, which is the inversion `massdot.py`
records for Boston arriving somewhere new: coverage follows who publishes, not who is
big or who is central.

`registry.matches` tests `Jurisdiction.ids` by set membership, so this answers for the
place and never for Travis County or for Texas - which is right. Austin's permits stop at
the city line even where the city does not.

**And it will match nothing until TIGER places for Texas are loaded**, which is a finding
about the install rather than about the adapter and is the one thing M14 measured that
disappointed. `tiger.boundaries` on the machine this was written on holds all 56 states and
all 3,235 counties but places for only five - California, Missouri, Kansas, Arizona and
Massachusetts, the states the five committed regions needed. So `longrun build-region` over
an Austin polygon resolves Travis County and Texas, neither of which this adapter claims,
and reports *"no closure adapter for any of 2"* with a correct, registered, live Austin
adapter sitting right there. `longrun load-tiger` for Texas is the fix, and the general
lesson is that a place-level adapter has a data prerequisite a statewide one does not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed live and keyless on 2026-09-23: WZDx 4.2, CC0 1.0, 4,056 features. The
#: Socrata download URL 302s to a signed file URL, which `fetch_feed` follows.
URL = "https://data.austintexas.gov/download/d9mm-cjw9"

#: Austin city, Texas. The place, not Travis County: the feed is the city's permit system.
AUSTIN = "tiger:place:4805000"


class AustinClosures:
    name = "wzdx.austin"
    kind = "closures"
    tier = 1
    scope = "feed"
    jurisdictions = (AUSTIN,)
    source = "wzdx_austin"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = AustinClosures()

__all__ = ["AUSTIN", "CLOSURES", "URL", "AustinClosures"]
