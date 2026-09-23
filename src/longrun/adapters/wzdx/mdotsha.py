"""Maryland DOT State Highway Administration's WZDx feed (scope 7.6, 7.10).

Tier 1, no key, WZDx 4.1, **CC0 1.0 declared in the envelope** (ADR 0038), so it backs a
cassette.

Called 2026-09-23: **HTTP 200, 56,006 bytes, 50 features, 50 parsed**, publisher "Maryland
DOT SHA", envelope seconds old. Impact is stated: 46 `some-lanes-closed`, 3
`all-lanes-closed`, 1 `all-lanes-open`.

**Served by RITIS, and that is the fragility worth naming.** The URL is
`filter.ritis.org`, the University of Maryland's regional data clearing house, not a
`maryland.gov` host - so the feed reaches a reader through a third party the publisher
does not operate. This is the `kdot.py` risk in a different shape: there the registry's URL
had moved, here the URL is a redistributor's. Both are reasons the adapter records the date
it was called rather than the date it was written.

Named `mdotsha` rather than `mdot`, because `modot` is Missouri and two adapters three
letters apart, one of them a typo away from the other, is the kind of mistake
`base.valid_jurisdiction_id` cannot catch: both would load, and the wrong one would answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed live and keyless on 2026-09-23: WZDx 4.1, CC0 1.0, 50 features.
URL = "https://filter.ritis.org/wzdx_v4.1/mdot.geojson"


class MdotShaClosures:
    name = "wzdx.mdotsha"
    kind = "closures"
    tier = 1
    jurisdictions = ("tiger:state:24",)
    source = "wzdx_mdotsha"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = MdotShaClosures()

__all__ = ["CLOSURES", "URL", "MdotShaClosures"]
