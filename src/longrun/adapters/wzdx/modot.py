"""Missouri DOT's WZDx feed (scope 7.6, 7.10, 11 region 4).

Tier 1, no key, **CC0 1.0** - and the licence is not a footnote. ADR 0006 established that
a source whose data cannot be redistributed cannot be committed as a cassette, and therefore
cannot back a golden route. CC0 can, which is half of why Kansas City is scope 11's
state-line region rather than Portland-Vancouver: Oregon's WZDx feed needs a key and
Missouri's and Kansas's do not.

Declared against the *state*, so one fetch answers for Jackson County, Kansas City and
every other place inside Missouri. See `registry._plan`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed live and keyless on 2026-09-10: WZDx 4.1, CC0 1.0, 40 features, several of them
#: inside Kansas City.
URL = "https://traveler.modot.org/timconfig/feed/desktop/mo_wzdx.json"


class MoDotClosures:
    name = "wzdx.modot"
    kind = "closures"
    tier = 1
    jurisdictions = ("tiger:state:29",)
    source = "wzdx_modot"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = MoDotClosures()

__all__ = ["CLOSURES", "URL", "MoDotClosures"]
