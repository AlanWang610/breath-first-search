"""New England Compass's WZDx feed for Maine, New Hampshire and Vermont (scope 7.6, 7.10).

Tier 1, no key, WZDx 4.2. **No cassette**, because the feed declares no licence - see
`azdot.py` for the same position and ADR 0038 for the rule. The adapter is live; its parse
path is not tested in CI, and that is a licence consequence rather than an oversight.

Called 2026-09-23: **HTTP 200, 483,953 bytes, 201 features, 201 parsed**, publisher
`NEC DOT`, envelope seconds old, `feed_info.license` absent. Impact is stated: 85
`some-lanes-closed`, 30 `all-lanes-open`, 10 `all-lanes-closed`, 76 falling back to the
event type.

**Three states for one fetch, which is the best ratio in the adapter set.** The USDOT
registry names the issuing organisation `NHDOT/VTAOT/MEDOT`, and the feed is the joint
product of the three. `registry._plan` fans out by adapter, so a route anywhere in northern
New England costs one request.

**What was measured about the three-state claim, and what was not.** Against state bounding
boxes on the day called, 147 features fall only inside Maine's, 27 only inside Vermont's,
and 27 in boxes that overlap New Hampshire's border with one of the other two. **None fell
unambiguously inside New Hampshire, and no road name carried an `NH-` prefix** (30 were
`VT-`, 17 were `ME-`). New Hampshire is declared anyway, because the publisher's stated
remit is all three and the alternative is a New Hampshire route reporting "no adapter"
when one exists - but a reader should know that on 2026-09-23 this feed answered for New
Hampshire with nothing, and that the project has not seen it answer with anything.

The feed's single data source carries `contact_name` and `contact_email` of
`PLACEHOLDER`, which is a further reason not to treat its metadata as a licence statement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext, AdapterResult

#: Confirmed live and keyless on 2026-09-23: WZDx 4.2, no declared licence, 201 features.
URL = "https://api.dx.ne-compass.com/wzdx-latest/"

#: Maine, New Hampshire, Vermont. See the docstring on what was observed for each.
NEW_ENGLAND = ("tiger:state:23", "tiger:state:33", "tiger:state:50")


class NecDotClosures:
    name = "wzdx.necdot"
    kind = "closures"
    tier = 1
    scope = "feed"
    jurisdictions = NEW_ENGLAND
    source = "wzdx_necdot"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        return fetch_feed(URL, self.name, day, ctx)


CLOSURES = NecDotClosures()

__all__ = ["CLOSURES", "NEW_ENGLAND", "URL", "NecDotClosures"]
