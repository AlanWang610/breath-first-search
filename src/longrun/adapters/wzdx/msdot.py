"""Mississippi DOT's statewide WZDx feed (scope 7.6, 7.10).

Tier 1, no key, WZDx 4.2 - the newest version the shared reader handles - and **CC0 1.0
declared in the envelope** (ADR 0038), so it backs a cassette.

Called 2026-09-23: **HTTP 200, 163,338 bytes, 160 features, 160 parsed**, publisher
"Mississippi Department of Transportation", envelope seconds old. 26 of the 160 are
`all-lanes-closed`, which is a higher proportion of fully-closed roads than any other feed
M14 called; 133 fall back to the event type and 1 is `all-lanes-open`.

Mississippi matters to the coverage argument more than its size suggests. Scope §12 says
data quality "varies regionally within the US" and §11 predicts thin data in rural
regions; the state DOT here publishes a current, CC0, 4.2 feed with stated impact, while
New York's much larger feed states no impact at all and forbids redistribution. Feed
quality does not track population, and a coverage manifest that reported only counts would
hide that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed live and keyless on 2026-09-23: WZDx 4.2, CC0 1.0, 160 features.
URL = "https://api.mdottraffic.com/prod/v3/data/wzdx"


class MsDotClosures:
    name = "wzdx.msdot"
    kind = "closures"
    tier = 1
    jurisdictions = ("tiger:state:28",)
    source = "wzdx_msdot"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = MsDotClosures()

__all__ = ["CLOSURES", "URL", "MsDotClosures"]
