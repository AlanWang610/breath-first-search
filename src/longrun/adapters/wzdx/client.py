"""Fetching a WZDx feed through the cache (scope 4.4, 7.10).

One function, shared by every WZDx adapter, so that the rules `forecast.py` set as "the
pattern the M4 adapters will copy" are obeyed in one place rather than three:

*Everything goes through the cache.* `core.data.cache.fetch` is the only door, and
`LONGRUN_OFFLINE=1` turns a miss into an error - which is what makes a golden route
hermetic and doubles as the cassette mechanism. There is no second recording system.

*Keys are provider-scoped.* `adapter.wzdx.modot` and `adapter.wzdx.kdot` are separate keys,
never one `wzdx` key. Keying on the kind would bake whichever publisher answered first into
a key claiming to be publisher-neutral.

*The budget is spent inside the producer*, so a cassette replay costs nothing.

**The cache holds the raw payload, not parsed features.** `forecast.py` caches raw NWS JSON
and parses outside it, and the reason applies here with more force: WZDx has three live spec
versions and the parser will change. Caching parsed output would invalidate every recorded
cassette the first time `_detail` learned a new field, and a WZDx feed for a past date can
no more be re-fetched than a forecast can.

**The whole feed is fetched, not a bbox query.** These are statewide files of a few hundred
kilobytes and none of them accepts a spatial filter; the corridor is applied client-side.
That is also what makes one fetch answer for every county and place in a state, which is the
registry's entire budget story.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.base import AdapterResult, describe
from longrun.adapters.wzdx.feed import feed_publisher, feed_version, parse_wzdx

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext
    from longrun.adapters.keys import ApiKey
    from longrun.core.models.features import FeatureKind

#: Long enough for a state DOT's whole feed on a slow morning, short enough that a hung
#: endpoint does not eat the plan's three-minute budget (scope 6.4).
HTTP_TIMEOUT_S = 20.0


def wzdx_args(adapter: str, day: date | str) -> dict[str, Any]:
    """The cache key's arguments, as a named function so a hash-stability test can pin it.

    No coordinates: the fetch is the whole statewide feed, so a key carrying a bbox would
    make two routes in the same state miss each other's cassette for no reason.
    """
    return {"adapter": adapter, "day": str(day)}


def fetch_feed(
    url: str,
    adapter: str,
    day: date,
    ctx: AdapterContext,
    *,
    kind: FeatureKind = "closures",
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> AdapterResult:
    """One WZDx feed, cached, parsed, and never raised out of."""
    from longrun.core.data.cache import fetch

    def produce() -> Any:
        import httpx

        ctx.budget.spend_api_call()
        # `headers` carries a credential for the feeds that need one in a header rather
        # than a query parameter. Deliberately kept out of `wzdx_args`, which is the cache
        # key: a recorded cassette must not depend on whose key fetched it, and a key in a
        # cache key would also put a secret in a committed fixture's filename.
        response = httpx.get(
            url,
            params=params,
            headers=headers,
            timeout=HTTP_TIMEOUT_S,
            follow_redirects=True,
        )
        response.raise_for_status()
        payload = response.json()
        # `cache.fetch` raises on a producer returning None, deliberately: "no data here"
        # has to be a structured empty payload so the absence stays reportable.
        return payload if isinstance(payload, dict) else {"type": "FeatureCollection"}

    try:
        payload = fetch(ctx.cache, f"adapter.{adapter}", wzdx_args(adapter, day), day, produce)
    except Exception as exc:  # noqa: BLE001 - a feed that is down is a reason, not a crash
        return AdapterResult(reason=describe(exc), source_url=url)
    return _result(payload, url, kind)


def replay_or_refuse(
    url: str,
    adapter: str,
    day: date,
    ctx: AdapterContext,
    key: ApiKey,
    *,
    kind: FeatureKind = "closures",
) -> AdapterResult:
    """A keyed feed whose key is not set: what was recorded, or the key's own reason (M17).

    Peeks under the same cache key `fetch_feed` writes, so a cassette recorded by somebody
    with the key replays for everybody without it - which is every CI run. A miss is
    `key_missing`, and the registry treats that as a rung nobody tried: no slot spent, and
    the next tier asked.
    """
    from longrun.core.data.cache import peek

    try:
        payload = peek(ctx.cache, f"adapter.{adapter}", wzdx_args(adapter, day), day)
    except Exception as exc:  # noqa: BLE001 - an unreadable cassette is a reason
        return AdapterResult(reason=describe(exc), source_url=url)
    if payload is None:
        return AdapterResult(reason=key.missing_reason(), source_url=url, key_missing=True)
    return _result(payload, url, kind)


def _result(payload: Any, url: str, kind: FeatureKind) -> AdapterResult:
    """A payload, fetched or replayed, as one adapter's answer."""
    version = feed_version(payload)
    features = parse_wzdx(payload, kind=kind, source_url=url)
    # "No envelope AND no features" is a feed that did not answer - a 200 carrying an error
    # page, most likely. An envelope-less feed that parsed 480 work zones answered fine, and
    # calling it unavailable would discard a whole state's closures over a metadata key.
    return AdapterResult(
        features=features,
        vintage=f"wzdx-{version}" if version else "wzdx-unversioned",
        source_url=url,
        reason=None
        if (features or version or feed_publisher(payload))
        else "the feed returned no WZDx envelope and no features",
    )


__all__ = ["HTTP_TIMEOUT_S", "fetch_feed", "replay_or_refuse", "wzdx_args"]
