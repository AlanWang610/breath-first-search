"""New Jersey's statewide WZDx feed (scope 7.6, 7.10).

Tier 1, no key, WZDx 4.1, **CC0 1.0 declared in the envelope** (ADR 0038), so it backs a
cassette.

Called 2026-09-23: **HTTP 200, 1,131,481 bytes, 740 features, 740 parsed**, envelope four
hours old, data source TRANSCOM.

**Published by NJIT, not by NJDOT.** The USDOT registry's issuing organisation for this
feed is the New Jersey Institute of Technology, and the payload's publisher is `NJIT`; it
is the university's Smart Work Zones programme republishing TRANSCOM's operational data.
The adapter is named for the state because that is what it answers for, and the
attribution line names NJIT because that is who publishes it.

**All 740 features are `all-lanes-open`, and that is a statement, not a gap.** Maricopa
County publishes `vehicle_impact: "unknown"` and Kentucky omits the field, which is a
publisher declining to say; New Jersey says, on every record, that no lane is closed. The
consequence for a plan is the same either way - no New Jersey work zone clears ADR 0013's
gate 1 on the impact field - but the reason is different, and `closures` reporting "740
work zones, none blocking" is true here in a way it would not be for Phoenix. Measured on
the whole live feed, not on the cassette.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed live and keyless on 2026-09-23: WZDx 4.1, CC0 1.0, 740 features.
URL = "https://smartworkzones.njit.edu/nj/wzdx"


class NjDotClosures:
    name = "wzdx.njdot"
    kind = "closures"
    tier = 1
    jurisdictions = ("tiger:state:34",)
    source = "wzdx_njdot"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = NjDotClosures()

__all__ = ["CLOSURES", "URL", "NjDotClosures"]
