# 0032 — A carried measurement finds its segment by ground, or is dropped and said so

Status: **accepted**, M11, 2026-09-22.

## Context

`segment_id(index)` is `f"s{index:05d}"` — purely positional
(`core/geo/segments.py`). `run_scorers` merges carried results **by scorer name only**, so
a carried `ScorerResult` keeps the `measurements[].segment_id` it was measured with. Both
were correct as long as nothing carried a result across a line that had moved, and until M11
nothing did: `rescore_plan` passes no router, so the geometry of a refresh is identical by
construction and ADR 0030 says as much in its own consequences.

Scope §10.3 removes that guarantee. "Click a flagged segment → choose an alternative; drag
to add a via point; draw an avoid polygon. **Each action re-runs only the affected
scorers**" is a partial re-score *across an edit*, and every edit that inserts or removes a
single route point renumbers every segment after it.

The failure is not that an id becomes invalid. It stays valid: after the edit `s00042`
still names a segment, a few hundred metres from the one the measurement was taken on. A
hostility reading of a quiet residential street is then presented as a reading of the
arterial the reroute put there, with full confidence, on the plan sheet, in the tier that
decides whether a route is safe. Nothing raises and nothing is logged.

**M10.12 met the same shape and settled it there.** GraphHopper numbers a turn instruction's
interval against the points it *sent*, and `path_to_route` renormalises and densifies those,
so reading an interval as a route index put "Arrive at destination" 8.1 km early on an
18.2 km route — as a perfectly plausible number. The fix was `cumulative_after_normalize`,
an inverse index that converts a position in one list into a distance that both lists agree
about. This is that move applied to `segment_id`.

## Decision

**A carried result is re-keyed onto the new segmentation through the ground, and anything
whose ground is gone is dropped and reported. Nothing is ever re-pointed.**

`core/geo/segments.py::map_segments(old_route, old, new_route, new)` returns a `SegmentMap`,
and `registry.carry()` applies it. Three parts, each of which is load-bearing.

**1. The span is the index, and it is not sufficient on its own.** An old segment matches a
new one when `cum_start_m` and `length_m` agree within `SAME_GROUND_M` (one metre). That is
what makes the search local and it is the roadmap's own rule. It is *not* enough, and the
counter-example is ordinary rather than exotic: replace a kilometre in the middle of a route
with a different kilometre of the same length and every distance downstream is unchanged, so
a span match alone re-points every measurement in the replaced stretch at new ground with no
sign that anything happened. So the endpoints of the candidate are compared against the
endpoints of the original, each on its own route — latitude and longitude only, never
`ele_m`, because `_score_pass` writes terrain heights onto the points it returns and two
readings of one line differ there whenever one machine had a DEM and the other did not.
`test_the_same_distances_over_different_ground_carry_nothing` pins the case directly.

**2. A segment that slid is lost, not followed.** A stretch pushed 300 m down the route by
an insertion upstream is reported lost. Following it would need a claim about *which* edit
happened, and an edit is not always a single contiguous splice — two avoid polygons in one
gesture are not. A measurement whose distance-along-route changed is a measurement that has
to be taken again, and saying so is cheaper than being wrong about it.

**3. Three kinds of loss, counted apart, on `ScorerResult.regrounded`.** A measurement
dropped because its segment is gone describes ground the edit removed. A measurement whose
id never named a segment — `ROUTE_SUMMARY_ID`, a `start@07:00` sweep row from
`start_time_optimizer`, a `meet#0` crew point — describes the *whole route*, and the whole
route is what changed; nothing is wrong with the ground it covers, the number is simply
about a different line. A flag and a waypoint are their own counts again. `None` on that
field is a fourth answer and not a fifth zero: it means the question did not arise, because
this pass measured the result itself or carried it across a line that did not move.

**`carry` drops rather than re-points, and there is no nearest-segment fallback.** "Close to
where this was taken" is not "where this was taken", and scope §3.6's whole argument is that
an absent answer beats a plausible wrong one. `core/scorers/base.py::unavailable` exists for
the same reason one level up.

**A grounding is required, not defaulted.** `run_scorers` raises `UngroundedCarry` when
results are offered to carry with no `carried_route` and `carried_segments`. This follows
`StalePrior` exactly: a partial pass that guesses produces a *wrong* number rather than a
missing one, and the wrong one is silent. `carried_segments=[]` is a real answer and not a
missing one — it says the stored results were measured against no segmentation at all, which
is what a synthetic result in a test has.

## Consequences

**No golden expectation moves, and that is checkable rather than hoped for.** `carry` is
only reached on the `name not in wanted` branch, which `only=None` never takes, so a full
pass does not call it at all. `measurements_sha256` is keyed on `segment_id`
(`tests/golden/expectation.py`), so a moved hash would have meant M11.3 renumbered something
it should not have. Six routes, **1,664 passed** against a 1,643 baseline, zero golden hashes
moved.

**`longrun refresh` is bit-identical to what it was.** `map_segments` opens with an equality
check on the two segmentations and the two point lists, and a refresh re-derives both from
the same stored route — so `moved` is False, the map is the identity, `regrounded` stays
`None`, and every carried result keeps every measurement it had.

**A waypoint is kept by distance, because that is the only key it has.** `PlanWaypoint`
carries `cum_dist_m` and no `segment_id` (ADR 0027), so one is kept when the ground at its
distance is ground this map matched. ADR 0027 closed by naming exactly this gap — "whether a
waypoint should carry a stable identity across re-scorings … that question belongs with the
editing surface" — and this is the answer for the carry case only. It is not identity: two
scorings still dedup on `(kind, position at 6dp)`, so "the user hid this one" is still
unanswerable and still belongs to the UI milestone.

**One metre is generous and deliberately so.** An edit that leaves a stretch alone leaves
its *points* alone, so an unchanged prefix agrees to float noise rather than to a metre. The
tolerance exists because `cum_dist_m` is re-accumulated from the first point on every pass
and because a line that came back from the router twice can differ in its seventh decimal
place. It sits far below `DEFAULT_MAX_SEGMENT_M = 250`, which is what stops it reaching a
neighbouring segment.

## What this does not decide

**Whether an edit should re-score more than the caller asked for.** `PRIOR_DEPENDENCIES` and
`closure()` cover the case where one scorer reads another's output; they say nothing about a
scorer whose *route summary* was dropped and which therefore has no total to report until it
is re-run. Today that reads as a missing measurement, which is honest and is not
self-healing. A rule that widened `only` to cover it would have to widen it silently or
report the widening, and ADR 0030 already argues for the second — so the shape of the answer
is known and the need for one has not yet been measured.
