# Golden routes

One directory per route under `routes/`:

```
routes/<name>/
  route.gpx         the geometry
  cache.sqlite      the recorded forecast, replayed offline
  request.yaml      the plan request: an absolute date and a pinned start time
  profile.yaml      the preference profile in force, pinned
  snapshot.json     data-snapshot pins, so the expectation is reproducible
  expected.json     the record of what the scorers used to say
  fixtures/         the layers and DEM the run reads: one file per layer
  README.md         what the route is built to exercise
```

Run through the CLI, no model in the loop, `LONGRUN_OFFLINE=1` exported so a scorer that
grows an external call fails loudly instead of depending on the network being up.

```
uv run pytest -m golden
uv run pytest -m golden --update-golden     # then read the diff before committing
```

## Why this tier exists

The unit tests pin each scorer against a corridor built for it. Nothing else pins the
*pipeline*. A change to segmentation, position weighting or arbitration alters what every
scorer says about a whole route while leaving every unit test green — that is the gap
these close, and it is why the run goes through `longrun repair` rather than calling
scorers directly.

## Two kinds of route, and the difference matters

**Synthetic** — a hand-drawn line over hand-written ways, where every LTS level, crossing
and water point is known because someone wrote the tags. These pin scorer **semantics**
and are reviewable line by line against the route's README. Layers are committed as
GeoJSON so the tags read in a diff.

**Real** — a clipped corridor frozen from PostGIS by `longrun freeze-fixture`. These pin
**integration and regression**. Nobody hand-verifies every number; you check the worst-N
lists and the summary statistics and treat the rest as a change detector. Layers are
GeoPackages, because that is what the database gave.

Start with one of each and resist adding more until scorer semantics stop churning. A
golden suite grown too early becomes a tax that discourages fixing scorers.

There are now exactly two: `synthetic-hazards` (hand-written, every figure checkable)
and `bay-urban` (real 3DEP terrain and 8,480 Overture buildings). Each caught a class of
bug the other could not — the synthetic one an azimuth convention and a nodata policy,
the real one a rasterizing loop that was a hundred times too slow and an absolute path
in a coverage reason that would have failed on CI.

## What `expected.json` holds

A summary, not a dump. A per-segment array for every scorer is a file nobody reviews and
every refactor rewrites. Stored instead: route and pacing totals; per scorer its
route-level summary, flag counts split hard from soft, mean confidence and worst-N as
`(segment_id, severity, reason_code)`; the residual flags after arbitration; all ten
verification results with their three states; the coverage manifest verbatim; and **a
content hash of the full measurement array**, so drift in something the summary does not
show still fails while the diff you read stays small.

Reason *codes*, never prose — `detail` is free to be rewritten without a golden diff.
Floats are rounded at serialization and never compared as raw f64.

## Discipline

`expected.json` is the record of what the scorers used to say. Diffs in it are **reviewed,
never blanket-regenerated**. `--update-golden` exists to write a change you have already
decided is correct, not to make a failure go away.
