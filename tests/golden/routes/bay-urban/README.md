# bay-urban

A real corridor, scored against real data: the Embarcadero north to Fort Mason, 2.0 km,
over **USGS 3DEP terrain** and **7,038 Overture building footprints**. This is the route
that answers M2's exit criterion — *"a plan sheet reports shaded fraction and WBGT per
segment from real DSM geometry"*.

It is the counterpart to `synthetic-hazards`, and the division of labour matters. The
synthetic route proves the numbers are **arithmetically right**: a wall of height H at
distance D subtends `arctan(H/D)`, and every figure in its expectation was hand-checked.
This one proves the pipeline **survives contact with real data** — 7,038 footprints of
wildly varying quality, real terrain that goes below sea level at the waterfront and climbs
115 m two blocks inland, and a real forecast. Nobody hand-verifies these numbers; the
worst-N lists and the summaries are reviewed and the rest is a change detector.

## Why late afternoon

`request.yaml` pins **17:30**. At 07:00 — `synthetic-hazards`' hour — the sun is barely up
and almost due east, and a street canyon running east–west is lit straight down its axis:
buildings are present and cast almost nothing onto the route. At 17:30 in September the sun
is low in the west and downtown blocks put real shadows across real streets, so the shade
figure has something to measure. Peak irradiance is 279 W/m² against the synthetic route's
45, which is the difference between "just after sunrise" and "an hour before it".

## What is real, and what is missing

| Layer | Source | State |
|---|---|---|
| DEM | USGS 3DEP 1/3 arc-second, tile `n38w123`, clipped | real, −1.1 to 5.7 m along the route |
| Buildings | Overture `2026-08-19.0`, via duckdb over S3 | real, 6,470 of 7,038 with a height |
| Canopy | Meta/WRI | **not fetched** — the tile index is 15 MB and this corridor is downtown |
| Ways, nodes, amenities | OSM, Geofabrik NorCal clipped to the Bay Area, `2026-09-04` | real, via `longrun load-osm` → PostGIS → `freeze-fixture` |
| Parks | PAD-US | **absent** — that loader is still M3 |

All twelve implemented scorers now measure. The five that report `unavailable` are the
ones whose milestone has not arrived: `closures`, `trail_status` and `access_hours` need
the M4 adapter registry, `hazards` needs NHD, `bailouts` needs GTFS.

## What the OSM layers changed, and what to read in them

Segments went from 9 to 51. That is scope's segment rule working rather than a tuning
change: with no way ids a route is cut into uniform ~250 m pieces, and with them it is cut
at way-change boundaries.

**Seven HARD legality flags on a waterfront route is not a bug, and it took checking.**
Two are pier gangways tagged `foot=private, bridge=yes, layer=1`, snapped 2 m and 20 m from
the line. Three more are at 1655–1726 m where the route leaves the promenade and runs onto
The Embarcadero itself (`highway=primary, foot=no, lanes=4`), its Muni busway
(`access=no`), and the Bay Street ramp (`primary_link, foot=no`, snapped 0.1 m). The route
really does go there. This is `repair` mode doing its job on a route with problems in it.

**One crossing on a 2 km San Francisco route is also right, and checking it caught a
wrong explanation.** Eighteen ways meet the route line; twelve are `service` driveways and
parking aisles, below `crossings.MIN_REPORTED_RANK`. Four are secondary or above, and
three of those — The Embarcadero as `primary` and again as `secondary`, and one of the two
Bay Street `primary_link` ramps — are ways the route *runs along*, which `own_way_ids`
excludes. What is left is the **other** Bay Street ramp, way 368095536, and it is
`primary_link` rather than the secondary road a first reading of the code suggested. It
carries no `maxspeed`, so it would classify as `unsignalized_primary_crossing_unknown_speed`
at severity 0.7 — except that `signal_positions` finds a `traffic_signals` node within 30 m,
which is correct: Bay Street at The Embarcadero is a signalized intersection. Hence
`unsignalized: 0` and no flag.

That correction came from a sabotage, not from reading. Lowering the threshold to
`tertiary` changed nothing, because no tertiary way meets this route — a vacuous sabotage
of exactly the kind M2.2 hit. Raising it to `primary` was the one that bit, and it bit
`synthetic-hazards` while leaving `bay-urban` untouched, which is what proved the surviving
crossing was primary-class all along.

**62% of route points snap to a way within 25 m**, so 38% of the route carries no tags at
all: `legality` reports 776 m of 2027 m as unknown and `surface_profile` reports 70%
unknown, which are the same fact seen twice. That figure now reaches the coverage manifest
on every run. Until this fixture existed it was recorded only below 50%, so a route like
this one said nothing at all — a cliff no reader could see.

## Fixture extent

The rasters and the buildings cover the route bounding box plus **990 m** on every side —
the corridor half-width (400 m) plus the ray-cast search radius (500 m), plus 10%. Not a
round number and not a guess: a DSM tile pads by exactly that, and a fixture that stopped
at the route would leave the margins as nodata and drag the shade confidence down for a
reason that has nothing to do with the route.

`dem.tif` is clipped on a **whole-pixel window** of the source. An unsnapped float window
rounds on read, so re-clipping with slightly different padding lands on a different pixel
grid, and the same route then samples different cells: trimming this fixture moved the
elevation gain from 32.6 m to 38.9 m for no reason but the window. Snapped, two clips at
different paddings agree exactly.

## Regenerating

`route.gpx` is interpolated between six real waypoints. The fixtures were built by the
script recorded in the M2.6 commit; `cache.sqlite` was recorded with
`longrun freeze-cassette` **before** 2026-09-12, because a forecast for a past date can
never be re-fetched. Do not regenerate the cassette — re-record only if the route or the
sampling changes, and then only against a future date.
