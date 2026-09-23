"""Idaho Transportation Department's 511 WZDx feed (scope 7.6, 7.10).

Tier 1, no key, WZDx 4.1. **No cassette**: `feed_info.license` is absent (ADR 0038).

Called 2026-09-23: **HTTP 200, 2,059,065 bytes, 841 features, 803 parsed**, publisher
`Arcadis`, data source `ERS`, envelope seconds old. **38 records did not parse** - ends
before starts, dropped one at a time by `feed.parse_wzdx` - which is the highest
proportion in the adapter set at 4.5%, against North Carolina's 0.5% and zero for every
feed that declares CC0. Recorded rather than smoothed over: it is a fact about the
publisher, and if it grows it should be visible as a change rather than as a number nobody
wrote down.

Impact is stated on most records: 326 `all-lanes-open`, 177 `alternating-one-way`, 66
`some-lanes-closed`, 29 `all-lanes-closed`, and 205 falling back to the event type.

**Keyless, on a 511 host, which is the AZ511 lesson repeating.** `azdot.py:11` records
that reading AZ511's documentation said a key was required and calling the endpoint said
it was not. `511.idaho.gov/api/wzdx` behaves the same way. The USDOT registry happens to
agree this time - it lists Idaho as needing no key - but the registry was wrong about
Florida and Oklahoma in the other direction on the same day (see
`adapters/wzdx/__init__.py`), so the call is still what settled it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed live and keyless on 2026-09-23: WZDx 4.1, no declared licence, 841 features.
URL = "https://511.idaho.gov/api/wzdx"


class IdDotClosures:
    name = "wzdx.iddot"
    kind = "closures"
    tier = 1
    jurisdictions = ("tiger:state:16",)
    source = "wzdx_iddot"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = IdDotClosures()

__all__ = ["CLOSURES", "URL", "IdDotClosures"]
