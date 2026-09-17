# 0025 — LTS is computed once, by the function the scorer calls, and stored in PostGIS

Status: **accepted**, M9, 2026-09-16.

## Context

Scope §7.1 states that an LTS 1–4 score "is computed offline per way in PostGIS and written as
an encoded value at import". ADR 0001 proved the *second* half end to end and left the first
explicitly undone — its reproduce section reads `# 2. synthetic LTS tag (placeholder scoring
-- replace with the PostGIS lookup)`, and `add_lts_tags.py::lts_for_way` carried a body
labelled `PLACEHOLDER` for eight milestones.

So the project had **two** LTS implementations. `core/routing/lts.py::lts_from_tags` — Furth
adapted for pedestrians, confidence-aware, reason-carrying — is what every scorer and every
plan sheet used. A crude tag table in a deploy script is what the router steered on. Nobody
had compared them.

Measured on the Ozarks when this ADR was written: **they disagreed about 469 of 1,600 ways**,
29% of the region.

| highway | placeholder | `lts_from_tags` | ways |
|---|---|---|---|
| `service` | 1 | 2 | 385 |
| `bridleway` | 1 | 2 | 69 |
| `tertiary` | 4 | 3 | 7 |
| `primary` | 3 | 4 | 6 |

`sidewalk=separate` was the sharpest: the placeholder read it as a sidewalk and *decremented*
the level; `has_sidewalk` returns `False` for it, because the sidewalk is mapped as its own way
and this way is the roadway, so the scorer *increments*. On a fast arterial that is the
difference between LTS 2 and LTS 4, in opposite directions.

The consequence is not that one was wrong. It is that **the LTS a plan reported was never the
LTS its route had been drawn to avoid**, and §8.4 then arbitrated between routes on that basis.

## Decision

**One implementation. `osm_lts.way_lts` is computed by `lts_from_tags`, in Python, and stored
in PostGIS.**

`regions/lts.py` streams `osm.ways`, calls the same function the scorers call, and writes
`(way_id, lts, confidence, reasons, aadt, aadt_source, lts_version)`.
`deploy/graphhopper/scripts/add_lts_tags.py` looks each way up by OSM id.

### Why not express Furth in SQL

Because agreement *is* the deliverable. A SQL reimplementation would be a second
implementation of the exact thing whose disagreement this ADR exists to end, and it would
drift the first time either side learned a new tag — silently, because nothing compares them.
The scope's phrase "computed in PostGIS" is honoured by where the table lives, which is what
the importer needs; it is not a claim about which language evaluates the rules. 1.79 M ways
score in 27 s, so nothing is bought by moving the arithmetic to the server either.

### Why its own schema

`PostGISLayerStore.vintage()` selects `WHERE layer_schema = %s ORDER BY (region = '') ASC,
loaded_at DESC LIMIT 1` — **schema only, no table and no source**. A vintage row for the LTS
computation written under `osm` becomes the answer every plan reports for `ways`, `nodes`
*and* `amenities`, by being the newest row in that schema. Three layers made wrong by one row
in the wrong place, with nothing to notice.

Verified by sabotage: writing the row under `osm` makes `vintage("ways")` return
`2026-09-13+lts1`. `tests/contract/test_way_lts_table.py` fails on it.

### Why a way with no `highway` tag gets no row

`core/data/osm.py::WAY_KEYS` loads `highway|railway|footway|cycleway`, so `osm.ways` holds
railways. `lts_from_tags` answers for *any* dict: an absent `highway` falls into its
`unknown_highway_class` branch and returns a well-formed level 2 at reduced confidence.
Writing that row would tag **4,392 railway ways** across the five regions at the same stress as
a residential street, and the importer could not tell them from real answers. The placeholder
returned `None` here, and that behaviour is kept.

### Why scoring is global and only the count is per region

`load_extract` loads the whole `.pbf`, which is a **bbox** clip, and `add_lts_tags.py` rewrites
that same bbox. Scoping the scoring to the region polygon — the obvious economy — would leave
every way between the polygon and the bbox edge with no row, reaching GraphHopper as the
encoded value's 0, which reads as "unknown" rather than "missing" to every custom model.
Measured: the five regions' polygons contain 1,567,925 ways and the extracts contain
1,792,674. **224,749 ways** would have been silently unscored.

### Why it runs after step 2, not inside step 1

§13 step 1 is "fetch the extract; compute the offline LTS score per way; build the graph", and
step 2 is what loads OSM into PostGIS. A PostGIS computation therefore depends on a step that
comes after it. `STEPS` gains `way_lts` between `layers` and `terrain`; this is an erratum to
§13's ordering, not a deviation from its intent.

### AADT

Optional and unchanged from ADR 0012: an `osm.way_aadt` left-join when the table exists
(nothing writes it), then OSM's own `aadt` tag, then nothing. The middle rung is not
decoration — `core/scorers/hostility.py` already reads `aadt_of(tags)`, so a table that
ignored the tag would disagree with the scorer on precisely the ways that carry a volume.
`aadt_source` records which answered.

**The forward hazard, stated so it is not discovered later:** the day something writes
`osm.way_aadt`, the scorer must read it too. The scorer reaches AADT only through the ways
frame, and there is no `aadt` layer in `DEFAULT_LAYER_TABLES`. Until that is wired, a conflated
table would reintroduce exactly the divergence this ADR closes, on the subset of ways where
the answer matters most.

## Consequences

- **`LTS_VERSION`, and a composite vintage `<extract>+lts<version>`.** The extract date pins
  the input and says nothing about the rules applied to it. A graph is built once and served
  for a week (§7), so "the router steered on rules this process no longer holds" is a reachable
  state, and nothing else could detect it. `SnapshotPins.graph_identity` returns the composite
  when present and falls back to the extract date, which is what keeps every cassette recorded
  before M9 replaying unchanged.
- **No fallback scoring path in the importer.** An unreachable database stops the build. A
  fallback is how the placeholder would come back: a plausible `.pbf`, a clean import, working
  routes, and wrong answers.
- **A hit-rate floor.** "No rows came back" and "the right rows came back" are indistinguishable
  once a graph is built. `add_lts_tags.py` counts roads found against roads in the extract and
  refuses below 95%. Sabotaged by offsetting every way id by one: 34.81%, refused, exit 1, no
  file written.
- ADR 0001's consequence still holds: **LTS scoring and LTS routing remain separate
  deliverables.** `core/` computes levels in pure Python and never reads this table; a Java
  problem still cannot block the core library. What changed is that the two now agree.
