# 0020 — Tuning costs recorded routes; it never re-routes

Status: accepted (2026-09-13)

## Context

§7.1 says the six priority parameters are fit "by grid or Bayesian search maximizing
agreement" against pairwise route preferences. It does not say what scoring a candidate
vector against a pair *means*, and there are two readings.

The literal one: for each vector, ask the router for a route and see whether it matches the
one the runner chose. That is what "fit the parameters" sounds like, and it is what the
router would actually do in production.

The tractable one: take the two routes that already exist in the pair, compute what each
would cost under the vector, and see whether the preferred one costs less.

The difference is not small. A grid of five levels over six parameters is 15,625 vectors.
Measured on this machine against 24 pairs, the second reading takes **0.91 seconds**. The
first would be 15,625 round trips to GraphHopper per pair — hours, and a fit nobody would
run twice.

## Decision

**A vector is scored by costing routes that already exist.**

```
cost(route) = sum over way classes of  metres / priority(class, vector)
```

which is the arithmetic GraphHopper runs inside its own search, so the ordering this
produces is the ordering the router would produce between those two lines. `agreement` is
the weighted fraction of pairs whose preferred route the vector costs lower.

A pair therefore stores a `RouteSummary` — metres per way class — and not a route. That has
a second benefit worth stating: a labelled set built this way is self-contained YAML. It
needs no fixtures, no layer store and no router to replay, and it survives the OSM vintage
underneath it changing, which a stored route full of way ids would not.

## Consequences

**The fit cannot discover a route the router never proposed.** This is the real cost and it
should not be glossed. If the best line between two points is one that no vector in the grid
would have drawn, no amount of fitting will find it, because it is not in any pair.

That is acceptable because **the pairs are the preference**. A preference pair records a
choice a person made between two routes they were shown. A route nobody chose between is not
evidence about what they want; it is a hypothesis about what they might have wanted. Fitting
against hypotheses is how an optimiser ends up confidently wrong.

**What would change this.** If the labelled set ever grows large enough that the grid is the
bottleneck rather than the routing — or if a fitted vector is found to consistently produce
routes that beat every option in the pairs it was fitted on — the right answer is a small
validation pass that *does* re-route, on the fitted vector only, and compares what comes back
against the chosen routes. That is one routing call per pair rather than 15,625, and it is
the check this decision defers rather than forbids.

**Way classes are the router's vocabulary, not the scorer's.** `lts.py` reads `sidewalk=*`
and posted speeds and reaches a better LTS than GraphHopper can; the router sees
`road_class`, `surface` and the `lts` encoded value. `WayClass` deliberately carries only
what the router can match on, because fitting a parameter against an attribute the router
cannot read produces a vector that does nothing and reports that it helped.

## Amendment, 2026-09-29 (M23): a routed line is summarised from the router's own details

The decision is unchanged: a pair stores two `RouteSummary`s, and a vector is scored by the
priority it implies over routes that exist. What changed is where a summary of a line the
**router** drew comes from.

Until M23 the only summariser was `tune --edit`'s: match a GPX against a ways layer, read
OSM tags, classify. That is right for a line a person drew, and it is unavailable wherever
there is no ways layer, which is every built region outside a golden's fixtures. So no pair
could be made on ground a person actually runs.

GraphHopper already carries `lts`, `surface` and `road_class` on every edge it serves, and
answers them as path `details` when asked (ADR 0001 measured `details=["lts"]`). So
`Router.route_summaries` asks for all three and `graphhopper.path_way_classes` reads them
off the path, per edge, on the router's raw points. This is also the more faithful reading
for a pair about router lines: the summary is in exactly the vocabulary the custom model's
terms match on, rather than an OSM reading of it that has to be kept in step by hand.

Keeping it in step by hand had already failed once. The fitter counted `compacted`,
`fine_gravel` and `grass` as unpaved and the router's `unpaved` term did not, so a vector
fitted to prefer Marin's compacted fire roads would have been costed on ways its own term
never touched. `custom_model` now holds one list of GraphHopper `Surface` names - the three
added were read from the 11.0 jar, and a contract test has the server accept them - plus the
jar's own alias table (`earth` is `DIRT` on import), and `preferences` derives its OSM
spellings from those. The path and collector classes are one tuple each, used both to emit
the terms and to count ways.

Asking for details is a different question from not asking, so it has its own cache key;
`route_args` adds `details` only when present, and every cassette recorded before this keeps
its key.
