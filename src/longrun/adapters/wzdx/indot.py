"""Indiana DOT's statewide WZDx feed (scope 7.6, 7.10).

Tier 1, no key, WZDx 4.0, **CC0 1.0 declared in the envelope** (ADR 0038), so it backs a
cassette.

Called 2026-09-23: **HTTP 200, 4,874,866 bytes, 1,355 features, 1,355 parsed**, publisher
`INDOTCastleRock`, envelope four minutes old. Impact is stated: 339 `some-lanes-closed`,
212 `detour`, 24 `all-lanes-closed`, 12 `alternating-one-way`, and 768 falling back to the
event type. The 24 fully-closed records are what ADR 0013's gate 1 can act on.

**Same vendor as Kansas, and that is the reason to name it.** `kdot.py` records a
publisher of `KDOTCastleRock`; this one is `INDOTCastleRock`, and Minnesota's feed - which
M14 could not reach, see `adapters/wzdx/__init__.py` - is on the same `carsprogram.org`
platform. One vendor behind three state feeds means a schema change there arrives in three
states at once, which is worth knowing before it does rather than after.

**It is 4.0 with the old envelope key**, matching Kansas and unlike North Dakota's 4.0.
See `nddot.py`: the envelope key does not follow the version.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed live and keyless on 2026-09-23: WZDx 4.0, CC0 1.0, 1,355 features.
URL = "https://in.carsprogram.org/carsapi_v1/api/wzdx"


class InDotClosures:
    name = "wzdx.indot"
    kind = "closures"
    tier = 1
    jurisdictions = ("tiger:state:18",)
    source = "wzdx_indot"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = InDotClosures()

__all__ = ["CLOSURES", "URL", "InDotClosures"]
