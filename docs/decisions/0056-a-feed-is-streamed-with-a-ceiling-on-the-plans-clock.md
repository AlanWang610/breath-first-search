# 0056 — A feed is streamed, refused past a ceiling, and read on the plan's clock

Status: **accepted**, M18, 2026-10-23.

## Context

Every adapter read its feed with `httpx.get(...).json()`: the whole body into memory, then
parsed, under a 20-second timeout that httpx applies per phase. A feed trickling a byte
every nineteen seconds never times out. Nothing in the adapter layer ever asked the plan's
three-minute clock (scope 6.4).

The nationwide scope adds feeds by the dozen. Two problems arrive with them:

- **Size.** Florida's statewide WZDx feed is 109 MB, a tenth of a gigabyte held to answer
  whether one road is open, and a cassette that breaches ADR 0031's 64 MB cap the day it is
  recorded.
- **Silence.** ADR 0039 recorded a CWZ feed read as WZDx: it parses to nothing, and with an
  envelope present it reads as "checked, nothing here" while carrying every work zone in the
  state.

## Decision

**1. Feeds are read through `adapters/http.get_json`.**

- **Streamed:** read in 1 MiB chunks.
- **Size declared up front:** a `Content-Length` over the ceiling is refused before the
  body is read.
- **Size counted as it arrives:** bytes past `MAX_FEED_BYTES` (32 MB) are refused.
- **On the plan's clock:** `Budget.check_deadline()` is checked after every chunk.

A refusal is a reason, never a truncated answer. A feed cut off at 32 MB reads exactly like
a state with fewer work zones.

**2. A deadline is a ceiling.**

- **Its own exception:** `DeadlineExceeded` subclasses `BudgetExceeded`, so every existing
  handler still catches it.
- **Said as it is:** `describe` reports it by its own message. "API call budget exhausted"
  had sent a reader looking for a call count nowhere near its limit.
- **It ends the climb:** the registry classes a deadline as a ceiling, so the climb ends
  there (ADR 0047 §3) rather than spending the time that ran out on the next tier.

**3. A feed whose records all fail to parse is a reason.** If a payload carries records and
none read as WZDx, the answer is "the feed carried N record(s) and none read as WZDx", not an
empty answer. Every recorded payload in the repository parses in full, so nothing moved.

## Consequences

* `wzdx/client.fetch_feed` reads through `get_json`, with per-adapter `timeout_s` and
  `max_bytes`.
* No golden moved. No recorded feed is near the ceiling, and every one parses in full.
* `nps.per_park` already pages and refuses past `MAX_PAGES` (ADR 0054). M20's 511 client
  and M21's portal client read through `get_json` from the start.

**How a feed larger than the ceiling will be read.** This is designed now and built with the
first such feed, Florida, when its key is in hand:

1. **Download:** stream the feed to a temporary file under the deadline, then parse it once.
2. **Cache in tiles:** split the features into 0.5° tiles (`t{floor(lat*2)}_{floor(lon*2)}`),
   cached as `part=<tile>` under the feed's day, with an index of tile counts under
   `part=index`. `wzdx_args` leaves `part` out when it is `None` (ADR 0029), so every
   existing cassette hash is unchanged.
3. **Ask per corridor:** declare the adapter `scope = "polygon"` and read only the tiles the
   corridor meets. A tile missing from the index is empty, not a cache miss.
4. **Keep cassettes small:** `freeze-cassette` keeps only the tiles a route read.

The tiling waits for Florida, the one feed that needs it, so nothing is built for a payload
nobody can see (ADR 0035, 0039).

## What would make us revisit

A feed that is large *and* updated many times a day, where re-downloading per day is the
wrong cadence. A feed's own `update_frequency` field would be the place to start.
