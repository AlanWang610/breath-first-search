"""MassDOT's WZDx feed (scope 7.6, 7.10, 11 region 1).

**Tier 1**, for the reason `sfbay.py` had to correct into existence: scope 7.10 assigns the
tier by *source type* — "1 WZDx, 2 511 API, 3 open-data portal, 4 LLM extraction" — and a
credential is a reason a fetch failed, not a statement about what the data is. Tier decides
the ladder, and `MIN_HARD_FLAG_TIER` is 2, so mis-tiering a feed changes which sources may
fail a route.

**Why this adapter exists at all is a finding from building region 1.** Boston's build
reported *"no closure adapter for any of 23"* jurisdictions, which read as a fact about
Massachusetts and was a fact about this project. The USDOT registry carries an **active**
MassDOT feed (`massdot__cwz`), so the dense, data-rich region had worse closure coverage
than the rural Ozarks — where MoDOT's statewide CC0 feed answers for 9 of 9 — purely
because nobody had written these forty lines.

That inversion is worth keeping in mind when reading scope §11, which predicts thin data in
the rural region and rich data in the dense one. For *closures* it came out backwards, and
the reason had nothing to do with either state.

Registered against `tiger:state:25` because MassDOT's remit is the state, and M4 measured
that one statewide fetch answers for every place inside it — which is what keeps a route
through Boston to two HTTP requests rather than twenty-three.

Confirmed 2026-09-13: the endpoint is live and answers **HTTP 401** without a key, so it
lands beside 511 SF Bay as a feed that names the key it needs rather than one that quietly
does not exist.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.base import AdapterResult
from longrun.adapters.keys import MASSDOT
from longrun.adapters.wzdx.client import fetch_feed

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext

URL = "https://api.massdot-swzm.com/api/v1/cwz/work-zone-feed"


class MassDotClosures:
    name = "wzdx.massdot"
    kind = "closures"
    tier = 1
    jurisdictions = ("tiger:state:25",)
    source = "wzdx_massdot"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        key = MASSDOT.value()
        if key is None:
            return AdapterResult(reason=MASSDOT.missing_reason(), source_url=URL)
        # Header rather than a query parameter, which is the shape the 401 body asks for.
        # Unverified against a live key — see the module docstring: this is written from the
        # unauthenticated response, and the first person with a key will find out whether
        # the header name is right. Written down here rather than implied, because an
        # adapter that fails with a *wrong* credential and one that fails with none look
        # identical on the sheet unless the reason says which.
        return fetch_feed(URL, self.name, day, ctx, headers={"Authorization": f"Bearer {key}"})


CLOSURES = MassDotClosures()

__all__ = ["CLOSURES", "URL", "MassDotClosures"]
