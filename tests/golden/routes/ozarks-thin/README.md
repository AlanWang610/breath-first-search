# ozarks-thin — the route where most of the data is not there

The seventh golden, and scope §11's region 3: *"Rural, thin data (Appalachia or Ozarks):
sparse OSM sidewalk/fountain tags, PAD-US resolving to USFS/NPS, no GTFS, cell gaps, likely
no WZDx or 511 API. Tests whether the coverage manifest tells the truth."*

10.4 km west out of Eminence, Missouri along MO-106 to Alley Spring, through Ozark National
Scenic Riverways in Shannon County. 2026-09-26, a Saturday, 07:30 CDT. The Ozarks over
Appalachia for the reason `deploy/regions/ozarks.yaml` gives: Missouri is inside
`us-midwest-latest.osm.pbf`, on disk since Kansas City, so region 3 cost a clip rather than a
1.5 GB download.

**Why this corridor.** Shannon County has one town and about three roads. This line leaves
the only incorporated place in the region, crosses the Jacks Fork, and runs inside a national
park unit for most of its length — so in 10 km it collects a place, a county, a state and
three PAD-US managers, which is the largest jurisdiction set a rural route here can produce.
It is also the only golden long enough for a dry gap to mean anything: at 10,395 m it is the
longest in the suite, and the water gap is the whole of it.

## What it pins, and what none of the other six could

```
route length_m           10395.3    the longest golden; next is loop-bayarea at 7893.5
segments                      54    34 of them matched to a way by the router
surface unknown_m         5334.9    51.3% of the route has no surface tag at all
surface unpaved_m         4918.6    47.3%; every other *real* golden reports 0.0 unpaved
surface paved_m            141.8     1.4%
legality unknown_m        4524.9    43.5% is ways the access rules cannot read
hostility scored_m        5870.4    of 10395.3 - the LTS table scores 56% of this route
services water_count           0    and max_water_gap_m is 10395.294: the entire route
services food_count            3    toilet_count 3, both clustered at the two ends
transit stops_in_corridor      0    stop_density stops 0, signals 0, gates 0
crossings                      0    no crossing node of any kind in 10 km
hazards                       13    possible_water_crossing, all 13 from NHD flowlines
closures jurisdictions         5    5 answered, 0 unanswered, all tier 1, 0 closures found
check 6 no_active_closures passed   every other golden in the suite SKIPS this check
sun_exposure svf           0.966    shaded_fraction 0.092, terrain only
cell_coverage segments         0    ABSENT, naming the download rather than a key
```

**Check 6 passing is the headline.** `no_active_closures` is `skipped` on all six older
routes, because ADR 0013's rule is fail beats skip beats pass and a route with any unasked
jurisdiction has not been cleared. `kc-stateline` skips it with four of nine unasked;
`bay-urban` and `phoenix-heat` skip it with *every* jurisdiction unasked. This is the first
route whose jurisdictions were all answered by a real feed, so it is the only one that
exercises the passing branch.

**The three zeros are three different facts and the sheet keeps them apart.** `crossings: 0`
and `stop_density.stops: 0` are measurements — `nodes` was frozen, read, and holds 7 features
here, none of them a crossing or a stop. `transit.stops_in_corridor: 0` is also a measurement:
`transit_stops.gpkg` exists and is empty because there is no agency in Shannon County.
`cell_coverage` is neither — it is `NOT CHECKED`, and its reason names the errand: *"the FCC
mobile coverage download is a manual step and no region build has loaded one"*. Scope §3.6
asks for that distinction and this is the only route where all three appear at once.

**47.3% unpaved is a first.** Every other real golden reports `unpaved_fraction: 0.0`; only
hand-written `synthetic-hazards` has any, because somebody typed the tag. So the unpaved
branch and the confidence it costs had been pinned end to end by nothing. The 51.3% that is
*unknown* is the other half of the same fact and scope §12's rule in the wild — a missing
`surface` tag is unknown, not absent, which is why the scorer reports mean confidence 0.682
rather than a clean answer about a road nobody has surveyed.

## §11's prediction about this region did not hold

§11 says region 3 has *"likely no WZDx or 511 API"*. It has WZDx, and it covers everything.
**Five of five jurisdictions answer at tier 1** — Shannon County, Eminence and all three
PAD-US managers — from **one** fetch of MoDOT's statewide CC0 feed, which returned 693 work
zones. `registry._plan` groups by adapter rather than by jurisdiction, so a state feed answers
for every body inside the state and each records `covered_by`.

Worth being precise about rather than celebrating. What makes the park units answer is
`from_padus_row` putting `tiger:state:29` in their `within` tuple, which happens because this
region touches exactly one state; `kc-stateline` touches two, `only_state` is `None` there,
and its `padus:CITY` falls to the tier-4 seam unqualified. So the coverage is real — MoDOT's
feed does describe roads inside the park — but it is coverage of *Missouri roads*, obtained by
asking the state on the park's behalf, and says nothing about a trail. The `trail_status`
lines for the same three jurisdictions are the honest counterpart: `padus:NPS` is matched by
the tier-3 NPS alerts adapter and reports `LONGRUN_NPS_API_KEY is not set` by name, while
`padus:OTHF` and `padus:NGO:29` fall to the tier-4 seam and say it is not wired up. Three
jurisdictions, three different reasons, none of them "no closures here".

What §11 got right: no GTFS (0 stops), sparse tagging (51.3% unknown surface, zero drinking
water in 10 km), and PAD-US resolving to NPS. `USFS` is in the region — the build found 8
units — but not in this corridor, so this route pins NPS and not USFS. Cell gaps are the one
prediction nothing here can test: there is no cell-coverage layer for any region, so the
answer is an absent source rather than a measured gap.

## The date, the offset, and the two windows

Recorded 2026-09-22 for **2026-09-26**. The cassette is permanent: a WZDx feed serves current
conditions and has no archive, and a forecast cannot be re-issued for a past date.

The binding constraint is not the weather. Queried on the recording day, Open-Meteo's
**weather** endpoint forecast hourly to 2026-10-07 and its **air-quality** endpoint only to
2026-09-28 — sixteen days against seven. A date inside the first and outside the second
records a cassette that half-scores, which is what the first `kc-stateline` attempt did.
2026-09-26 is four days out with two days of margin at the end that binds, and both endpoints
were asked for that exact date and returned a full 24 hours before anything was written.

A **Saturday** deliberately: day of week is the only input `transit` and `bailouts` have, so a
weekday zero would leave a reader unable to tell an absent agency from a weekday-only service.
**UTC−5, stated not guessed** — Shannon County is Central Time and observes daylight saving,
and the 2026 switch is 2026-11-01. ADR 0008 settled that the offset is looked up, and
`lighting` records `utc_offset_source: stated`. Sunrise at Eminence is about 07:01, so 07:30
runs entirely in daylight and `dark_m` is 0.0 on purpose: darkness is `boston-winter`'s
subject.

## Fixtures: all of them, and nothing dropped

`boston-winter` carries two layers of nine and `loop-bayarea` four, because a dense corridor
is expensive. This one carries **everything** — `ways`, `nodes`, `amenities`, `parks`,
`railways`, `flowlines`, `transit_stops`, `boundaries` and a clipped 3DEP DEM — for 4.68 MB,
of which 1.45 MB is the DEM and 1.73 MB the cassette. A 400 m corridor around 10.4 km of the
Ozarks holds **341 ways**; the same buffer around 2.5 km of the Charles holds 14,158.

`railways.gpkg` and `transit_stops.gpkg` are empty files and it matters that they exist: each
says the layer is loaded for this region and this corridor has none of it. `freeze-fixture`
refuses to write a file for a layer with *no table*, because "an empty GeoPackage in a fixture
is indistinguishable from a corridor that genuinely contains nothing" — so here the empty file
is the positive statement, and deleting it would turn a measured none back into an unchecked
layer.

## Reproducing

`route.gpx` is **not an input** — this route runs in generate mode from the `from`/`to` in
`request.yaml`. It is kept because the corridor and the cassette were frozen against it, and
it was drawn by `GraphHopperRouter.route`, the same call `longrun plan` makes.

```powershell
docker compose -f deploy/docker-compose.yml up -d
uv run longrun build-region deploy/regions/ozarks.yaml
./deploy/graphhopper/run.ps1 -Region ozarks          # port 8997

uv run longrun freeze-fixture tests/golden/routes/ozarks-thin/route.gpx `
    --out tests/golden/routes/ozarks-thin/fixtures `
    --dem "/vsicurl/https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/n38w092/USGS_13_n38w092.tif"
# ^ writes everything then does not exit; kill it after the last line (freeze.py documents why)

uv run longrun freeze-cassette tests/golden/routes/ozarks-thin/route.gpx `
    --date 2026-09-26 --out tests/golden/routes/ozarks-thin/cache.sqlite `
    --fixtures tests/golden/routes/ozarks-thin/fixtures `
    --with-adapters --with-routes --router http://localhost:8997 --graph "2026-09-13+lts1"

# `--with-routes` records the map-match and one detour and NOT the opening route, so
# generate mode still misses. One online plan against the same cache and snapshot fills it:
uv run longrun plan --from "37.1508,-91.3576" --to "37.1497,-91.4436" `
    --date 2026-09-26 --start 07:30 --rounds 0 --utc-offset -5 `
    --fixtures tests/golden/routes/ozarks-thin/fixtures `
    --profile  tests/golden/routes/ozarks-thin/profile.yaml `
    --snapshot tests/golden/routes/ozarks-thin/snapshot.json `
    --cache    tests/golden/routes/ozarks-thin/cache.sqlite `
    --router http://localhost:8997 --out $env:TEMP/ozarks-online
```

`snapshot.json` carries `graph: "2026-09-13+lts1"` and `region: "ozarks"`, so the route cache
key is the extract *and* the LTS rules; `--graph` on the freeze and `graph_identity` on the
replay must agree or the cassette misses loudly.

Recorded with **no keys in the environment**, deliberately. `uv run` does not load `.env` and
`tests/conftest._clear_longrun_env` deletes every `LONGRUN_*` variable before each test, so a
cassette recorded with `LONGRUN_NPS_API_KEY` set would hold an NPS response the golden can
never replay — and the route would pin a different answer on a developer's machine from the
one CI sees.

## Known weakness

**The `transit_stops` vintage is wrong and is left that way.** `snapshot.json` pins
`transit_stops: "gtfs-mbta"` for a corridor with no transit agency.
`PostGISLayerStore.vintage()` takes the most recent `meta.layer_vintage` row for the schema
and has no idea which region is asking; no GTFS feed has ever been loaded for the Ozarks
because none exists, so it returned Boston's. Nothing here reads a stop that came from Boston
— the layer froze zero features — but the pin claims a feed this corridor does not have,
which is the precise error this route exists to catch. Recorded in `snapshot.json`'s `extra`
rather than hand-corrected: correcting it by hand would hide the finding, and fixing it
properly means giving the store a region, which is more than a golden should change.

**Elevation is terrain-only.** `sun_exposure` reports `shade measured from dem; buildings and
canopy unavailable` at a mean sky-view factor of 0.966, over a route under closed
oak-hickory canopy for most of its length. `ozarks.yaml` predicted this region would be the
first real test of M2's height arithmetic over forest; it is not, because the canopy raster
has never been fetched for any region. 0.966 is honest about a bare-earth DEM and dishonest
about the shade a runner gets, and `canopy: NOT CHECKED` is all that stands between the two.

**It is `rounds: 0` and pins nothing about arbitration**, and nothing here exercises tier-4
extraction with a model — two jurisdictions reach the seam and report `NullExtractor`'s
honest reason, the same answer `kc-stateline` records. The cassette does hold one detour, and
its number is worth knowing: the only alternative around the 4.2–5.2 km span is **26.87 km**
against a 10.4 km route. In a county with one road a reroute is not a small correction, and a
loop golden here would be testing that rather than arbitration. `loop-bayarea` stays the loop
golden.
