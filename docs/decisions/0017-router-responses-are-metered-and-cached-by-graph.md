# 0017 — Router responses are metered always, and cached by graph identity

Status: accepted (2026-09-12, written 2026-09-14)

**Written two days after it was accepted, and that gap is part of the record.** M5's plan
listed this ADR among the five it owed, `tests/golden/routes/loop-bayarea/README.md` cited it
by number, and the file was never created. Every other ADR from M5 was written with the code
that proved it; this one was not, and the dangling citation sat in a golden route's README
until a count of the directory came out one short. The decision below is the one
`core/routing/cached.py` has implemented since M5.4b — nothing here is new, only written down.

## Context

Before M5.4, `grep Budget core/routing/` returned nothing. No router call had ever been
charged to a budget, so scope 6.4's 200-call cap counted forecasts and adapter fetches and
not the calls scope 8.1 step 6 makes most of — up to five rounds of detours. And no router
answer had ever been recorded, so no golden could exercise the loop, whose whole subject is
what comes back from an alternative.

The first draft fused these into one decision. They are two, and only one needs a cache.

## Decision

**Metering is unconditional.** Every call the inner router makes is charged with
`budget.spend_api_call()`, inside the producer passed to `cache.fetch` — so a live call costs
one and a replay from the cassette costs nothing. That placement is the whole mechanism: a
charge outside the producer would bill a recorded answer as if it had been fetched.

**Replay keys on the graph, not the day.** `CachedRouter` wraps any engine exposing `paths`
and `map_match`, and records under `graphhopper.route` and `graphhopper.map_match` with
`STATIC_DAY` in the date column. A route is not a forecast, so the day is meaningless to it;
what a route *does* depend on is the graph it was drawn on, and scope 7 rebuilds that weekly.
So the graph identity — `SnapshotPins.osm_extract_date` — goes into the args, and two graphs
never share a key.

This is a **new exception** to the `(tool, args, date)` rule, not an extension of ADR 0007.
0007 rests on a COG's bytes being content-addressed and immutable; a route is neither.

Three rules, each of which fails silently if broken:

1. **Cache the raw path dicts**, exactly as the server sent them, with `path_to_route` left as
   the single decoder. `SqliteCache.put` stores `json.dumps(value, default=str)`, so handing it
   a pydantic `Route` would store the model's repr and return a `str` on the next hit.
2. **Never store a degraded empty.** `alternatives` turns `RouterUnavailable` and
   `NoRouteError` into `[]`, which is indistinguishable from "this span has no detour" and
   perfectly cacheable — so one outage during a recording session would pin a false "no
   alternatives here" into a cassette for good. Only the inner router's *answers* are
   recorded; a producer that raises stores nothing.
3. **Re-raise `CacheMiss` before any broad handler.** `CacheMiss` is a `LookupError`, and both
   `map_match` and `pipeline.matched_way_ids` had bare `except Exception` blocks. Without the
   re-raise, an offline miss becomes a silent fall-back to geometric snapping — a wrong answer
   that looks exactly like a right one, in the place the cassette guarantee exists to protect.

## Consequences

**A golden can run the whole loop with the router pointed at a closed port.** `loop-bayarea`
passes `--router http://127.0.0.1:9`, so an unrecorded request fails loudly as `CacheMiss`
rather than quietly reaching a GraphHopper somebody happens to have running. Sabotaged in
M5.12: bypass the cache and the route exits 3 instead of passing.

**Everything in the key must be stable across platforms, and that was learned the hard way.**
The first CI run on M5 failed `loop-bayarea` on `ubuntu-latest` with a cache miss that never
happened on Windows (M5.13). A detour's avoid-polygon is a GEOS buffer of 87 trigonometric
vertices, transformed back to WGS84 by PROJ at 17 significant digits, and it travels inside
`custom_model` — which is in the key. A last-bit disagreement between the two platforms'
builds of GEOS or PROJ gave one request two keys. Coordinates had been rounded for `points`
and not for the polygon beside them. Both are now rounded: at the source in `detour_area`, so
two platforms send the same request, and again in the args builder, so a polygon a user drew
is covered too.

**What is deliberately *not* in the key.** `instructions` is not among the fields a route is
keyed on, because nothing requests them. The day something does — `cue_sheet` is the obvious
candidate — the key has to learn the flag first, or every cassette recorded without
instructions will be served to a request that wants them, and the result will be a cue sheet
with no turns in it. `tools/runnability.py` says so where the flag would be flipped.

**What would change this.** A second routing engine keeps its own dotted tool names, so no
key collides. A graph rebuilt without a new `osm_extract_date` would replay stale routes
against new data — the identity is only as good as the pin, and a region build that forgot
to bump it would be the failure mode.
