"""AZ511's statewide WZDx feed (scope 7.6, 7.10).

Tier 2, key-gated, and deliberately registered against **Arizona** while
`wzdx.maricopa` holds the county. The registry asks the best tier first and stops there, so
a Phoenix route gets MCDOT's keyless feed at tier 1 and never reaches this one; a Tucson
route has nothing at tier 1 and falls here, where it is told which key is missing.

That layering is the tiered design working rather than an accident: scope 7.10's ladder
exists so a jurisdiction with a better source does not pay for a worse one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.base import AdapterResult
from longrun.adapters.keys import AZ_511
from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext

URL = "https://az511.com/api/wzdx"

#: Ten calls per sixty seconds, per the developer documentation. Well inside one fetch per
#: plan, and recorded because a future adapter asking per-county would not be.
RATE_LIMIT_PER_MINUTE = 10


class Az511Closures:
    name = "state511.az511"
    kind = "closures"
    tier = 2
    jurisdictions = ("tiger:state:04",)
    source = "state511_az511"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        key = AZ_511.value()
        if key is None:
            return AdapterResult(reason=AZ_511.missing_reason(), source_url=URL)
        return fetch_feed(URL, self.name, day, ctx, params={"key": key})


CLOSURES = Az511Closures()

__all__ = ["CLOSURES", "RATE_LIMIT_PER_MINUTE", "URL", "Az511Closures"]
