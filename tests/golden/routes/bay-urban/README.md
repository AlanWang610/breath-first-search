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
| Ways, nodes, amenities | OSM | **absent** — the loaders are M3 |

Six scorers therefore report `unavailable` on this route, and that is the point rather than
a defect: `legality`, `segment_hostility`, `crossings`, `stop_density`, `surface_profile`
and `services_along` all need OSM, `longrun freeze-fixture` is written and tested, and the
region build that would populate PostGIS is M3. The coverage manifest says so in one line
each, which is the scope §3.6 behaviour this fixture exercises hardest.

`segments_matched_to_a_way` is **0** for the same reason, so verification check 2
(`on_network`) reports `skipped`, not passed.

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
