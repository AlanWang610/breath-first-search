"""Maricopa County's WZDx feed (scope 7.6, 7.10, 11 region 2).

Tier 1, no key, WZDx 4.2 - the newest of the three. Registered against the **county**
rather than the state, because this is MCDOT's feed and not Arizona's: AZ511 publishes a
statewide one and it needs a key nobody here has.

This is the feed every one of `closures`' five gates was calibrated against. It carries 115
work zones, most of them lane closures on highways no pedestrian is on, and one running
2024-10-22 to 2028-06-16 - which is what `STANDING_CONDITION_DAYS` exists to catch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed live and keyless on 2026-09-10: WZDx 4.2, publisher "MCDOT", 115 features.
URL = "https://wzdxapi.aztech.org/construction"

#: Maricopa County, which contains Phoenix. `Jurisdiction.within` is what lets this answer
#: for the city without the adapter having to name every place in the county.
MARICOPA_COUNTY = "tiger:county:04013"


class MaricopaClosures:
    name = "wzdx.maricopa"
    kind = "closures"
    tier = 1
    jurisdictions = (MARICOPA_COUNTY,)
    source = "wzdx_maricopa"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = MaricopaClosures()

__all__ = ["CLOSURES", "MARICOPA_COUNTY", "URL", "MaricopaClosures"]
