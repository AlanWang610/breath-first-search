"""Reading a feed over HTTP: streamed, with a ceiling, and on the plan's clock (M18, ADR 0056).

Every adapter read its feed with `httpx.get(...).json()`: the whole body into memory, then
parsed, under a 20-second timeout that httpx applies per *phase* - a feed trickling a byte
every nineteen seconds never times out at all. Two things follow, and both get worse as
M19-M21 add feeds by the dozen:

* **A big feed is a big plan.** Florida's statewide WZDx feed is 109 MB. Read whole, it is a
  plan that holds a tenth of a gigabyte to answer whether one road in Gainesville is open,
  and a cassette that breaches ADR 0031's cap the day somebody records it.
* **A slow feed is a slow plan.** Scope 6.4 gives a plan three minutes, and nothing in the
  adapter layer ever asked the clock.

So a feed is streamed: `Content-Length` refused up front when it says too much, the bytes
counted as they arrive and refused past `MAX_FEED_BYTES`, and the plan's deadline checked
after every chunk - so a feed that would outlast the plan ends as a ceiling (ADR 0047 §3)
rather than taking the plan with it. A refusal is a reason, never a truncated answer: a
feed cut off at 32 MB reads exactly like a state with fewer work zones.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from longrun.adapters.base import AdapterContext

#: Long enough for a state DOT's whole feed on a slow morning, short enough that a hung
#: endpoint does not eat the plan's three-minute budget (scope 6.4).
HTTP_TIMEOUT_S = 20.0

#: The largest feed read whole. Every feed adopted by M18 is under 12 MB; Florida's 109 MB is
#: the reason for the number, and ADR 0056 records how a feed that size would be read.
MAX_FEED_BYTES = 32 * 1024 * 1024

#: How much arrives between two looks at the plan's clock.
CHUNK_BYTES = 1024 * 1024


class FeedTooLarge(RuntimeError):
    """A feed larger than its ceiling, refused rather than truncated."""


class FeedRefused(RuntimeError):
    """A feed that answered with an error status (M20).

    Raised in place of httpx's `HTTPStatusError`, whose message is the whole request URL -
    query string included - and so carried `api_key=...` into every coverage reason a keyed
    feed's refusal produced, and from there onto the plan sheet. This one names the host and
    path and nothing a caller put in `params`.
    """

    def __init__(self, status: int, where: str) -> None:
        super().__init__(f"HTTP {status} from {where}")
        self.status = status
        self.where = where


def get_json(
    url: str,
    ctx: AdapterContext,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = HTTP_TIMEOUT_S,
    max_bytes: int = MAX_FEED_BYTES,
) -> Any:
    """GET a JSON document, streamed. Raises `FeedTooLarge`, `FeedRefused`, `BudgetExceeded`
    (the plan's deadline) or httpx's transport errors; never returns a partial document.

    Spends nothing - the caller's producer spends the API call, as it always has, so a
    cassette replay costs nothing.
    """
    import httpx

    with httpx.stream(
        "GET", url, params=params, headers=headers, timeout=timeout_s, follow_redirects=True
    ) as response:
        if response.is_error:
            raise FeedRefused(
                response.status_code, f"{response.request.url.host}{response.request.url.path}"
            )
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise FeedTooLarge(f"the feed declares {int(declared):,} bytes, over {max_bytes:,}")
        body = bytearray()
        for chunk in response.iter_bytes(CHUNK_BYTES):
            body += chunk
            if len(body) > max_bytes:
                raise FeedTooLarge(f"the feed ran past {max_bytes:,} bytes")
            ctx.budget.check_deadline()
    return json.loads(bytes(body))


__all__ = [
    "CHUNK_BYTES",
    "HTTP_TIMEOUT_S",
    "MAX_FEED_BYTES",
    "FeedRefused",
    "FeedTooLarge",
    "get_json",
]
