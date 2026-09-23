"""New York State DOT's 511NY WZDx feed (scope 7.6, 7.10).

Tier 1, no key, WZDx 4.1. **No cassette, and this one is not a gap in the evidence - it is
the evidence.** 511NY's terms page, read 2026-09-23, says:

    Redistribution or republication of any part of 511NY or its content is prohibited,
    including by such methods as framing, other similar methods or by any other means,
    without the prior express written consent of NYSDOT.

That is an explicit prohibition, not an absent grant, and ADR 0006's rule applies at its
strongest: a committed cassette would be redistribution, so this feed can never back a
golden route or a CI-tested parse path. The adapter reads the feed at plan time, which is
use rather than republication; nothing it reads is committed.

This is the finding that made ADR 0038 worth writing. `azdot.py` records "AZ511 terms of
use" as a reason not to commit a cassette, which reads like caution; New York shows it is
not caution. Every feed M14 adopted with no `feed_info.license` sits somewhere on the range
between AZ511's silence and this, and the project cannot tell which without reading each
publisher's terms - so the licence in the payload is the only thing it will act on.

Called 2026-09-23: **HTTP 200, 7,841,257 bytes, 6,330 features, 6,330 parsed**, publisher
`Arcadis`, data source TRANSCOM, envelope seconds old, `feed_info.license` absent. **All
6,330 categorise as `work-zone`**: the impact field says nothing on any record, so New York
is in Maricopa County's position at fifty times the size - reported, never gate-1 on
impact alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed live and keyless on 2026-09-23: WZDx 4.1, no declared licence, 6,330 features.
URL = "https://511ny.org/api/wzdx"

#: Where the prohibition quoted in the module docstring is published.
TERMS = "https://511ny.org/terms"


class NysDotClosures:
    name = "wzdx.nysdot"
    kind = "closures"
    tier = 1
    jurisdictions = ("tiger:state:36",)
    source = "wzdx_nysdot"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = NysDotClosures()

__all__ = ["CLOSURES", "TERMS", "URL", "NysDotClosures"]
