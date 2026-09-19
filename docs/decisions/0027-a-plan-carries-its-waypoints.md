# 0027 — A plan carries its waypoints, on a list beside the measurements

Status: **accepted**, M10, 2026-09-18.

## Context

Scope §9's first listed output is a GPX "with waypoints for water, toilets, bailouts, hazards
and gates". `core/geo/gpx.py::gpx_write` has accepted a `waypoints` argument since M1 and
**no caller has ever passed one** — the only call site in `src/` is the `gpx_write` MCP tool,
which passes none, and no CLI command wrote a GPX at all until this milestone.

The reason is upstream of the writer. The scorers that find these places throw the positions
away:

* `services.py` holds a fountain's latitude and longitude, calls `frame.locate`, and keeps a
  scalar distance;
* `bailouts.py::_nearest_served_stop` finds the winning transit stop and returns a `float`;
* `hazards.py` computes the exact point where the route crosses a railway and keeps a count,
  with the position surviving only as prose inside `Flag.detail`.

`SegmentMeasurement.values` is `dict[str, float | int | str | bool | None]`, frozen, and
scalars-only **by design** — `start_time_optimizer.py` says so in as many words: *"`values`
holds scalars by design — that is what keeps the plan sheet renderable and the golden content
hash meaningful."*

## Decision

A waypoint is **not a measurement**. `PlanWaypoint` lives in `core/models/waypoint.py` and
hangs off `ScorerResult` as a **fourth list beside `coverage`**, with the merged, deduplicated
result stored on `Plan.waypoints`.

`WaypointKind` moves from `core/geo/gpx.py` to the models layer, which is free — it had no
consumer outside that file — and necessary, because the models layer may not import the geo
layer. `gpx.py` re-exports it, so `gpx_write`'s signature does not move.

## Consequences

**The golden content hash does not move.** `expectation.measurements_hash` walks
`result.measurements` and `measurement.values`; a sibling list is invisible to it. Six
scorers began recording positions and **`measurements_sha256` was byte-identical on all six
routes** — checked with `git diff -- tests/golden/routes | grep -c measurements_sha256`,
which returned 0. Coordinates inside `values` would have rewritten all six. A test asserts
the property directly rather than leaving it to be rediscovered.

**A waypoint can carry things a measurement cannot**: a real `datetime` and a nested
`LatLon`. `crew_points` stored its ETA as an `"%H:%M"` string *only* because of the scalars
rule, and both course formats need a timestamp. `_eta_at` now returns the `datetime` and the
measurement formats at its own call site, so `values["runner_eta"]` is unchanged.

**The digest learns counts, never positions.** A lat/lon in `expected.json` fails on a libm
difference inside `Transformer.transform` — the exact class
`test_the_expectation_is_machine_independent` exists to catch. Counts per kind per scorer
still catch a scorer going quiet, one emitting twice, or a dedup rule changing.
`max_offset_m` is the one geometric value pinned, because it is what moves if someone
confuses a waypoint's true position for its position on the course.

**Two positioning rules, enforced by module boundaries rather than by convention.** See
ADR 0028 for the FIT half; the rule itself is that `core/export/course.py::at_distance` is
the only way to ask "where on the track is this", `fit.py` and `tcx.py` call it and never read
`waypoint.position`, and the GPX reads `position` and never calls `at_distance`. A bailout
6 km off the line would otherwise put a course point 6 km off the track. Both directions are
tested, because either alone permits the bug.

**Two judgements about what *not* to emit**, both of which make the output usable:

* **Bailouts emit only where the nearest exit is a served transit stop.** The nearest
  drivable road for a city segment is the unnamed street the runner is standing on, forty
  metres away; on `loop-bayarea` that would be 126 markers of no information. Measured after
  the rule: `bay-urban` produces **1**. Where there is no exit at all, nothing is emitted —
  the `no_bailout_in_reach` flag already says so, and a marker at a place you cannot leave
  from would be a lie.
* **`marker` gets no producer.** A kilometre marker is a rendering choice, not a
  measurement, and a hundred of them would swamp `plan.json`. The writers synthesise them.

**The word "waypoint" now means two things in this codebase.** Six modules
(`cli/plan.py`, `regions/routers.py`, `agent/loop.py`, `core/routing/base.py`,
`core/plan/metrics.py`) use it for the *router input points* a route is drawn through, which
`PlanRequest` calls `start`, `end` and `via`. The field docstring says so, because otherwise
the next reader wires `PlanRequest.via` into the GPX.

**What this does not settle:** whether a waypoint should carry a stable identity across
re-scorings. Today it does not, and `merge_waypoints` dedups on `(kind, position at 6dp)`,
which is enough for a GPX and not enough for "the user hid this one". That question belongs
with M13's editing surface.
