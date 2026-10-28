# 0053 — A speed survey merges at region build, and reaches the router and the scorer through one column

Status: **accepted**, M18, 2026-10-23. Amends ADR 0025.

## Context

Scope 7.10 makes `speed_survey` an adapter kind, and nothing had ever consumed one. The
nationwide-adapter scope said: merge surveys at region build into `speed_survey.way_speed`,
and have `way_lts` take `max(posted, 85th percentile)`.

Read against the code, that would have reopened what ADR 0025 closed. `segment_hostility`
never reads `osm_lts.way_lts`: it scores every way from the frozen `ways` tags at plan time,
through `lts_from_tags`. A survey written only into the table would have moved the router's
level on exactly the ways someone measured, and left the scorer's where it was. ADR 0025
recorded the same hazard for traffic volume: "the day something writes `osm.way_aadt`, the
scorer must read it too".

## Decision

**1. Merged at build, never at plan time.** A new region-build step, `speed_surveys`, runs
between `layers` and `way_lts`:

- It asks the adapters that claim the region's jurisdictions for surveys.
- It matches each survey to the ways it measured.
- It writes `speed_survey.way_speed`, in its own schema, so no `ways` vintage moves.
- A change clears `way_lts`'s record, so the same build rescores.

LTS is computed once (ADR 0025), so a survey read at plan time would move the scorer's
level and not the graph's.

**2. The speed travels as one column, everywhere a way's tags are read.**

- `survey_speed_85_kph`, with its source, is joined onto the plan-time `ways` layer by
  `PostGISLayerStore`. So a plan reads it, and every fixture `freeze-cassette` writes
  carries it.
- The build's `way_lts` pass merges the same table into the tags under the same name.
- `lts_for_way(tags)` is the one reader. `hostility`, the build's rows and `preferences.way_class` all
  use it, and a test holds the build's row to it.

Conflated volume rides the same mechanism as `aadt_conflated`, read before OSM's tag. That
closes ADR 0025's volume hazard too, though nothing writes the table yet (ADR 0012).

**3. The speed read is the higher of the survey and the posted limit.** A 40 km/h street
where traffic does 60 is a 60 km/h street to someone beside it. A survey under the limit is
no reason to believe the sign less. A survey is a measured speed, so it also lifts the
missing-speed confidence penalty.

**4. Matching is geometry and a little name, in a metric frame.**

- Within 15 m for at least half the shorter line.
- Within 20 degrees, undirected.
- Names agreeing after suffix normalisation, where both have one. That is all that tells an
  arterial from its frontage road 12 m away.
- Only tier 3 or better, at confidence 0.8 or more, is merged.
- Where two surveys claim one way, the better tier wins, then the higher confidence, then
  the newer survey, then the higher speed.

**5. No `LTS_VERSION` bump; `+ss<survey>` on the graph identity instead.** A way with no
survey scores exactly as before, so the rules did not change; the input did.
`composite_vintage` appends a content hash of the merge when one exists. A region with no
survey keeps its identity, and an identical rebuild keeps its hash.

## Consequences

* **No golden moved.** No fixture carries a survey.
* **`hostility` reports `speed_85_kph` and `speed_source` only where a survey was merged.**
  That is why no golden moved.
* **The registry's 40-jurisdiction and 16-fetch ceilings are now per instance.** They belong
  to a plan. A region build passes `None`, because Bay Area's 187 jurisdictions are a
  region's ordinary size.
* **The step reports `blocked` until an adapter claims a survey.** No adapter does before
  M21.
* **The database halves are `network` contract tests.** They skip without a PostGIS, and
  skip until a region has a survey merged.

## What would make us revisit

A survey published as points, at count stations, rather than as lines. The bearing test needs
a line, and a point dropped on a crossroads would be merged into whichever street was nearer.
If M21 finds such data worth having, the station's direction field is the place to start.
