"""Fetching a 511 platform's or a state DOT's own feed through the cache (M20).

The rules `wzdx/client.py` set, kept for a source that is not WZDx:

*Everything goes through the cache*, under a provider-scoped tool (`adapter.state511.ga`),
so `LONGRUN_OFFLINE=1` makes a miss an error and a recording replays.

*The budget is spent inside the producer*, so a replay costs nothing.

*The cache holds the raw payload.* A 511 platform's event list is parsed outside it, so a
parser that learns a new field does not invalidate what was recorded.

*Read through `adapters.http.get_json`* (ADR 0056): streamed, refused past its ceiling rather
than truncated, and on the plan's clock.

**The key is never in the cache key.** An IBI platform takes its credential as the `key`
query parameter. A cache key carrying it would make a recording depend on whose key fetched
it, and put a secret in a committed fixture's name. And a refused request is described by
`http.FeedRefused`, which names the host and path - the reason a refusal produces reaches the
plan sheet, and httpx's own message is the whole URL, key and all.

**A missing key replays before it refuses** (M17): peek under the key `fetch` would have
written, so a recording made by somebody with the key replays for everybody without it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.base import AdapterResult, describe
from longrun.adapters.http import HTTP_TIMEOUT_S, MAX_FEED_BYTES, FeedRefused

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext
    from longrun.adapters.keys import ApiKey

#: Every IBI Group 511 platform serves its event list at this path: 511GA, 511 Alaska,
#: NVRoads and CTroads among them, each documented at `<site>/help/endpoint/event`.
EVENTS_PATH = "/api/v2/get/event"

#: The statuses an IBI platform answers a bad key with. Checked 2026-10-28 with no key at
#: all: GA, AK, NV, CT, ID, AZ, LA and PA each answered **400** with the body
#: `<Error><Message>Invalid Key</Message></Error>` - XML, although `format=json` was asked.
KEY_REFUSED_STATUSES = frozenset({400, 401, 403})


def events_args(adapter: str, day: date | str) -> dict[str, Any]:
    """The cache key's arguments, as a named function so a hash-stability test can pin it.

    No coordinates and no key: the fetch is the platform's whole event list, which no IBI
    platform filters by area, and a recording must not depend on whose key fetched it.
    """
    return {"adapter": adapter, "day": str(day)}


def cached_json(
    url: str,
    adapter: str,
    args: dict[str, Any],
    day: date,
    ctx: AdapterContext,
    *,
    params: dict[str, Any] | None = None,
    timeout_s: float = HTTP_TIMEOUT_S,
    max_bytes: int = MAX_FEED_BYTES,
) -> Any:
    """One JSON document, cached under `adapter.<adapter>` and `args`. Raises; the adapter
    turns the exception into a reason.

    `params` goes on the request and never into `args` - that is where a credential rides.
    """
    from longrun.adapters.http import get_json
    from longrun.core.data.cache import fetch

    def produce() -> Any:
        ctx.budget.spend_api_call()
        payload = get_json(url, ctx, params=params, timeout_s=timeout_s, max_bytes=max_bytes)
        # `cache.fetch` refuses a producer returning None; a JSON `null` is an empty answer.
        return payload if payload is not None else []

    return fetch(ctx.cache, f"adapter.{adapter}", args, day, produce)


def fetch_events(
    base_url: str,
    adapter: str,
    day: date,
    ctx: AdapterContext,
    key: ApiKey,
    *,
    timeout_s: float = HTTP_TIMEOUT_S,
    max_bytes: int = MAX_FEED_BYTES,
) -> list[Any] | AdapterResult:
    """An IBI platform's event list - fetched, or replayed when no key is set - or the
    reason it could not be had. Never raises."""
    from longrun.core.data.cache import peek

    url = base_url.rstrip("/") + EVENTS_PATH
    args = events_args(adapter, day)
    value = key.value()
    try:
        if value is None:
            payload = peek(ctx.cache, f"adapter.{adapter}", args, day)
            if payload is None:
                return AdapterResult(reason=key.missing_reason(), source_url=url, key_missing=True)
        else:
            payload = cached_json(
                url,
                adapter,
                args,
                day,
                ctx,
                params={"key": value, "format": "json"},
                timeout_s=timeout_s,
                max_bytes=max_bytes,
            )
    except FeedRefused as exc:
        if exc.status in KEY_REFUSED_STATUSES:
            return AdapterResult(
                reason=f"{exc.where} refused {key.env_var} (HTTP {exc.status})", source_url=url
            )
        return AdapterResult(reason=describe(exc), source_url=url)
    except Exception as exc:  # noqa: BLE001 - a platform that is down is a reason
        return AdapterResult(reason=describe(exc), source_url=url)
    if not isinstance(payload, list):
        return AdapterResult(reason="the platform did not return an event list", source_url=url)
    return payload


__all__ = [
    "EVENTS_PATH",
    "KEY_REFUSED_STATUSES",
    "cached_json",
    "events_args",
    "fetch_events",
]
