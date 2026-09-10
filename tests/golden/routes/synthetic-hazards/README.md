# synthetic-hazards

A hand-written route over hand-written ways. No OSM data, no real place: every LTS level,
crossing, gate and water point here is known **by construction**, because the tags were
written to produce it. That is what a synthetic golden is for — `expected.json` can be
checked line by line against this file, which a real route's expectation never can be.

The route is deliberately *bad*. It runs on a motorway, crosses a fast road unsignalized,
and spends 800 m on gravel. Repair mode exists for routes with problems, and a golden
whose every check passes would exercise none of the reporting that is the actual product.

## Geometry

101 points at latitude 37.7955, from longitude −122.4000 eastward in steps of 0.00045°.
That is 39.54 m per step and **3954.0 m** in total. Points are indexed 0-100 below.

## Ways along the route

| way | points | length | tags | what it is there for |
|---|---|---|---|---|
| 101 | 0-20 | 790.8 m | `highway=footway, surface=asphalt, lit=yes` | benign opening |
| 102 | 20-30 | 395.4 m | `highway=residential, maxspeed=25 mph, sidewalk=both` | low LTS |
| 103 | 30-40 | 395.4 m | `highway=primary, maxspeed=80, lanes=6, sidewalk=no` | **LTS 4** |
| 104 | 40-60 | 790.8 m | `highway=track, surface=gravel` | **sustained unpaved** |
| 105 | 60-70 | 395.4 m | `highway=motorway, foot=no` | **illegal on foot** |
| 106 | 70-100 | 1186.2 m | `highway=footway, surface=asphalt` | benign close |

## Ways the route crosses

Placed at *half*-steps, so no route point sits on an intersection: a point equidistant
from its own way and a crossing way would make way assignment a coin flip, and the golden
would be non-deterministic for a reason that has nothing to do with any scorer.

| way | at point | tags | expected |
|---|---|---|---|
| 201 | 24.5 | `highway=primary, maxspeed=60` | **hard** — primary, 60 kph > the 40 kph floor |
| 202 | 44.5 | `highway=secondary, maxspeed=30` | **soft** — below primary rank |
| 203 | 88.5 | `highway=primary, maxspeed=60` | **no flag** — a signal node sits on it |

Way 203 is the negative control. It is a crossing that *counts* (`crossings: 3`) and does
not *flag*, so a bug that stopped detecting it entirely would change the count while
leaving the flags identical — which is why the route-level summary is pinned and not only
the flags.

## Nodes

`stop_density` counts signals and gates, and nothing else — a node tagged `crossing` is
neither, so a cluster of those would measure zero and prove nothing.

- one `traffic_signals` at point 88.5, which is what signalizes crossing 203
- one `gate` at point 40
- five `traffic_signals` at points 72, 74, 76, 78, 80 — 316 m apart end to end, so a
  1 km window over them holds 6 stops against the default tolerance of 3.0/km

## Amenities and parks

Water at points 6 and 89, a toilet at 15, a cafe at 33; all within 30 m of the line, well
inside the 200 m service buffer. The long water gap is between the two water points:
3519.4 − 237.2 = **3281.8 m**, which is what `max_water_gap_m` reports.

One park polygon covers points 2-18.

## Elevation and the surface model

`dem.tif` is a plane: elevation rises linearly with longitude from **5 m** at the west end
to **65 m** at the east, on a 0.0002° grid (≈22 m). Uniform 1.5 % grade, no spikes, so
verification check 4 passes and the pacing figure is a clean function of distance and
climb. Regenerate with the same linear ramp if it is ever lost; do not substitute real
terrain, which would make the pacing number unreviewable.

It extends **±0.011° of longitude and ±0.010° of latitude beyond the route**, which is not
slack: a DSM tile pads the route by the corridor half-width (400 m) plus the ray-cast
search radius (500 m), and a raster that stopped at the route would leave those margins as
nodata and drag the shade confidence down for a reason that has nothing to do with the
route.

`canopy.tif` is 12 m of tree over points 40–60 — the gravel track — and zero elsewhere.

`buildings.geojson` is 25 m tall throughout: a **canyon** lining both sides of points 0–20
at 20 m from the line, then a **single row on the south side only** over points 22–30, so
one stretch is half-open rather than enclosed. One footprint near point 32 carries **no
height at all**, because Overture records frequently lack the attribute and the coverage
manifest has to be able to say so.

Two properties this fixture is built to demonstrate, both visible in `expected.json`:

- The canyon does **not** shade the route at 07:00. The sun is barely up and almost due
  east, the street runs east–west, and a canyon parallel to the sun's azimuth is lit down
  its axis. Shade appears where geometry puts it, not where buildings are.
- `heat_stress` reports WBGT around **18 °C** at 15–16 °C and high humidity, well under the
  26 °C soft threshold — so the flag path stays untested here on purpose. A hot-region
  golden is what would exercise it (scope §11 test region 2).

## The forecast cassette

`cache.sqlite` is a recorded Open-Meteo forecast for 2026-09-12 at the route's two sample
sites, written by `longrun freeze-cassette` — the same `route_forecast` a scorer calls, so
it holds exactly the keys a scorer will ask for. It is permanent: a forecast for a past
date can never be re-fetched, which is also why `request.yaml` pins an absolute date.

`request.yaml` pins `utc_offset_hours: -7`. Longitude alone gives −8, and 12 September is
Pacific Daylight Time; an hour of error is fifteen degrees of solar azimuth.

## What this route does not cover

Air quality, resupply schedules, closures, trail status, access hours, hazards and bailouts
all report `unavailable` here, because their scorers do not exist yet. They appear in the
coverage manifest with a reason, which is the scope 3.6 behaviour under test — not an
omission from this fixture.

Way matching is geometric (repair mode has no router), so verification check 2
(`on_network`) reports `skipped`. It is not a pass.
