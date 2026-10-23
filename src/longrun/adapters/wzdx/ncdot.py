"""North Carolina DOT's DriveNC WZDx feed (scope 7.6, 7.10).

Tier 1, no key, WZDx 4.1. **No cassette**: `feed_info.license` is absent and the DriveNC
terms page carries no redistribution grant this project could act on (read 2026-09-23).
ADR 0038 has the rule; `nysdot.py` has the case that shows why it is not over-caution.

Called 2026-09-23: **HTTP 200, 12,105,118 bytes, 6,458 features, 6,425 parsed**, publisher
`Arcadis`, data source `ATMSERS`, envelope seconds old. The 33 dropped records are ends
before starts, which `feed.parse_wzdx` discards one at a time rather than failing the
state. Impact is stated on 2,557 of them: 1,304 `all-lanes-open`, 430 `all-lanes-closed`,
414 `alternating-one-way`, 409 `some-lanes-closed`; the remaining 3,868 fall back to the
event type.

**The largest payload this project fetches, at 12 MB.** The registry lists North Carolina,
New York and Idaho under the same vendor, and all three answer keyless; they are also the
three biggest downloads in the adapter set. One fetch per region per day is the whole cost
(`registry._plan` fans out by adapter, not by jurisdiction), but it is the reason
`HTTP_TIMEOUT_S` at 20 s is a real constraint rather than a formality - this feed took
1.17 s on a good connection and Florida's, which M14 refused, is nine times larger again.

Also note the host: the registry publishes `drivenc.gov`, which 301s to `www.drivenc.gov`.
`fetch_feed` follows redirects, so this is recorded rather than worked around - the same
class of staleness `kdot.py` found in the registry's Kansas URL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed live and keyless on 2026-09-23: WZDx 4.1, no declared licence, 6,458 features.
URL = "https://drivenc.gov/api/wzdx"


class NcDotClosures:
    name = "wzdx.ncdot"
    kind = "closures"
    tier = 1
    scope = "feed"
    jurisdictions = ("tiger:state:37",)
    source = "wzdx_ncdot"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = NcDotClosures()

__all__ = ["CLOSURES", "URL", "NcDotClosures"]
