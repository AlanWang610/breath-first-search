# loop-bayarea — the scope §8.1 loop, end to end, with no model in it

The fifth golden, and the first that is not a single scoring pass. The other four run
`longrun repair` — steps 3 to 5, once. This one runs `longrun plan` through the loop:
route, score, arbitrate, reroute, re-score, verify, up to five rounds.

Ferry Building to the de Young, 2026-09-15. ADR 0001's own measured pair.

## What it pins

```
rounds: 1
locked: 5925.8–5934.3 m, "round 1: rerouted"
status: complete
trade_offs: []
budget: 0 external API calls
```

One round: the loop found a flagged segment, asked the router for a detour around it,
scored the candidate, found it **strictly better**, adopted it and locked the span so the
next round would not reopen a choice already made. The adopted route is 7,893 m against the
original 7,695 m — the loop bought 198 m of distance for a segment it did not like, which
is exactly the trade scope §8.1 step 6 exists to make.

## Two things it does deliberately

**It runs with no model.** ADR 0015: every LLM call site has a deterministic path, so the
whole of §8.1 executes with `NO_MODEL` and the only difference a key would make is a terser
trade-off line. That is what makes the loop golden-testable at all, and it is what
`harness.py` has promised in its first sentence since M1.7.

**Its router is pointed at a closed port.** Every route, detour and map-match is recorded in
`cache.sqlite` (ADR 0017), and the harness passes `--router http://127.0.0.1:9` so an
*unrecorded* request fails loudly rather than quietly succeeding against whatever
GraphHopper a developer happens to have running. Sabotaged to check: bypass the cache and
this route exits 3 instead of passing.

## The elevation gap is the point, not a defect

```
Elevation missing at 223 of 270 points; those stretches were paced as flat.
```

This route deliberately leaves the corridor `bay-urban` froze its DEM for. It is the first
route in this project not cut to fit the raster frozen for it, and that is how M5.5 found
that **every point outside a DEM's coverage had been reported as an elevation of −999999
since M1** — `read_window` returns array and transform and throws the nodata value away,
and the read is `boundless=True, fill_value=src.nodata`. The observed sheet claimed
1,000,012 m of descent over 8 km.

Every other golden sits inside its own raster, which is why four milestones of green never
touched it. This one keeps the caveat honest.

## Fixtures: four layers, not nine

`ways`, `amenities`, `transit_stops` and `dem`, copied from `bay-urban`. The first three
are what make `bailouts` and `services_along` *answer* rather than abstain — and a route
with no flags gives the loop nothing to do, which would pin zero rounds and detect no
regression in them. `buildings`, `parks`, `railways`, `flowlines` and `nodes` would be
another 9 MB of fixture buying coverage `bay-urban` already pins.

## Known weakness

**It has no same-tier conflict, so it never parks.** `trade_offs` is empty and the
`needs_input` path is exercised by `tests/unit/test_loop.py` and `tests/unit/test_jobs.py`
rather than here. Moving the route until it produced one would make the golden a test of
one week's traffic — the same judgement `kc-stateline` made about closures, and for the
same reason.
