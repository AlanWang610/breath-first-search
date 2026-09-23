"""The Kentucky Transportation Cabinet's statewide WZDx feed (scope 7.6, 7.10).

Tier 1, no key, WZDx 4.1, **CC0 1.0 declared in the envelope** (ADR 0038), so it backs a
cassette. Served as a static object from Google Cloud Storage under a bucket named
`kytc-its-2020-openrecords`, which is the agency's own open-records publication path.

Called 2026-09-23: **HTTP 200, 524,864 bytes, 282 features, 280 parsed**, publisher
"Kentucky Transportation Cabinet (KYTC)", envelope 24 minutes old. The two features that
did not parse are the case `feed.parse_wzdx` drops deliberately: `Feature` rejects an end
before its start, and KYTC publishes two such records. One bad record must not cost a
route every closure in the state, so they are dropped and the other 280 are kept.

**KYTC states no vehicle impact on any of the 280.** Every one categorises as `work-zone`
by falling back to the event type, which puts Kentucky in Maricopa County's position: its
work zones are reported and can never clear ADR 0013's gate 1 on the impact field, only on
a description that names a footway. Measured, not assumed, and it is the reason this
docstring does not say the adapter "adds closure coverage" without qualification.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed live and keyless on 2026-09-23: WZDx 4.1, CC0 1.0, 282 features.
URL = (
    "https://storage.googleapis.com/kytc-its-2020-openrecords/public/feeds/WZDx/"
    "kytc_wzdx_v4.1.geojson"
)


class KytcClosures:
    name = "wzdx.kytc"
    kind = "closures"
    tier = 1
    jurisdictions = ("tiger:state:21",)
    source = "wzdx_kytc"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = KytcClosures()

__all__ = ["CLOSURES", "URL", "KytcClosures"]
