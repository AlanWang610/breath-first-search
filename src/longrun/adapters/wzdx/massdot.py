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

**Called with a key on 2026-10-28, which settles both questions this module used to
carry** (ADR 0058):

* **The header is right.** `Authorization: Bearer <key>` answered **HTTP 200**. The same key
  as `x-api-key`, or as an `api_key` query parameter, answered 401.
* **CWZ 1.0 reads.** **3,245,157 bytes, 1,363 features, all 1,363 parsed** by the shared
  WZDx reader, publisher "MassDOT Work Zone Manager", envelope seconds old, 259 active at
  the call. CWZ keeps WZDx's `core_details`, `vehicle_impact` and dates where the reader
  looks for them, so nothing needed a second parser. The impact is stated on all but 12:
  720 `some-lanes-closed`, 244 `all-lanes-open`, 237 `alternating-one-way` and 150
  `all-lanes-closed`.

**CC0 1.0 is declared in the envelope** (ADR 0038), so it backs a cassette that replays
with no key, `cwz_massdot.json`. Reported as `cwz-1.0` rather than `wzdx-1.0`, which would
name a WZDx version from 2019.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.base import AdapterResult
from longrun.adapters.keys import MASSDOT
from longrun.adapters.wzdx.client import fetch_feed, replay_or_refuse

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext

URL = "https://api.massdot-swzm.com/api/v1/cwz/work-zone-feed"


class MassDotClosures:
    name = "wzdx.massdot"
    kind = "closures"
    tier = 1
    scope = "feed"
    jurisdictions = ("tiger:state:25",)
    source = "wzdx_massdot"
    key = MASSDOT
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        value = MASSDOT.value()
        if value is None:
            return replay_or_refuse(URL, self.name, day, ctx, MASSDOT, spec="cwz")
        # A bearer header: verified with a key on 2026-10-28, when `x-api-key` and an
        # `api_key` query parameter were both refused (see the module docstring).
        return fetch_feed(
            URL, self.name, day, ctx, headers={"Authorization": f"Bearer {value}"}, spec="cwz"
        )


CLOSURES = MassDotClosures()

__all__ = ["CLOSURES", "URL", "MassDotClosures"]
