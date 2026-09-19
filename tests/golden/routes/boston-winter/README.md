# boston-winter — the first golden route run in the dark

The sixth golden, and scope §11's region 1: *"dense, cold, transit-rich… darkness tolerance,
`sun.cool`, stop density"*. Every plan this project had produced before it was a daylight plan.

The Dr. Paul Dudley White Path along the Charles, 2026-01-15, starting at 17:00 EST. Boston
civil twilight that day ends between 17:00 and 17:15, so the run begins in the last of the
light and finishes after dark.

## What it pins, and what none of the other five could

```
dark_m                1067.0     of 2457 m  (43%)
starts_in_daylight    true
finishes_in_daylight  false
unlit_dark_m           125.6     ways tagged lit=no, run after twilight
unknown_lit_dark_m     821.5     ways that said nothing, run after twilight
flags                  2 soft    unlit_in_darkness, severity 1.0, COMFORT
mean_confidence        0.867
```

Every one of those is a first. `bay-urban`, `kc-stateline`, `loop-bayarea`, `phoenix-heat` and
`synthetic-hazards` all report `dark_m: 0.0`, `unlit_dark_m: 0.0` and `mean_confidence: 1.0`
from `lighting` — so the `unlit_in_darkness` flag, its severity formula, the
`darkness_tolerance` branch, `UNKNOWN_LIT_CONFIDENCE = 0.4` and the *"run in darkness on ways
with no lit tag"* coverage entry had been pinned by unit tests and by nothing end to end.

**The 125.6 against 821.5 is the point of the route, not an accident of it.** Scope §8.3's flag
is *"segment in darkness with `lit=no`"* — not "without `lit=yes`" — and Boston tags 595 ways
`lit=no` against 60,466 untagged. So this route runs 947 m of unlit-or-unknown ground in the
dark and flags 126 m of it, because that is the part somebody actually surveyed. A reader who
wants the other 821 m is told it is *unknown, not unlit*, in a coverage line. Reverse that rule
and most of the rural US becomes a hard flag for having roads nobody has tagged.

## Two things the date settles

**It is a past date, and that is what makes it recordable.** The M8–M14 plan assumed a winter
golden was impossible — *"a forecast for a past date can never be re-fetched"* — which stopped
being true in M3. `forecast.open_meteo_root` reaches `archive-api.open-meteo.com`, which is how
`phoenix-heat` pins a July date recorded 57 days later. A *future* winter date is the
unrecordable one: the forecast endpoint reaches about a fortnight ahead.

**Recording it found a bug that had made the archive unreachable.** `open_meteo_root(day,
today)` was called with `ctx.clock.now().date()`, and that clock is `FrozenClock(start_at)` —
pinned to *the plan's own date* so plans are reproducible. Reference and day were therefore
always equal, the difference was always zero, and `ARCHIVE_AFTER_DAYS` had never once fired.
`phoenix-heat` did not catch it because 57 days is inside the forecast endpoint's own ~92-day
window. This route is 244 days back, got `400 Bad Request` from both sites, and would have
pinned a plan with no forecast at all. The fix is in `_open_meteo_fetch`: ask the forecast
endpoint, fall back to the archive when it refuses — which needs no clock, so scope §3.3's
no-implicit-clock rule stands.

Air quality answered for both sites, so `air.py` having no archive selector of its own turned
out not to matter: its endpoint serves the date directly.

## Fixtures: `ways` and `dem`, and the cap is why

`loop-bayarea` carries four layers of nine and explains the 9 MB it declined. This carries
**two**, and the reason is that Boston is the densest region built: a 300 m corridor — scope
§5's floor, not the 400 m default — around 2.5 km of river holds **14,158 ways**, and
`ways.gpkg` alone is 7.4 MB. At the default buffer with `nodes`, `amenities` and
`transit_stops` the route came to 10.9 MB, which is more headroom than the suite had.

What was dropped and where it is pinned instead: `nodes` → `synthetic-hazards` and `bay-urban`
(crossings); `amenities` → `bay-urban` and `loop-bayarea` (services, resupply);
`transit_stops` → `loop-bayarea` (bailouts, stop density); `buildings` → `bay-urban`
(sun_exposure over 8,480 Overture footprints). None of them is what *this* route is for: `lit`
lives on `ways`, and darkness is arithmetic.

**The suite was 48.49 MB against its 50 MB cap when this route landed** — 1.5 MB of headroom,
which is not enough for a seventh route. This entry said the next route to want space should
force a decision rather than shave itself, and name the honest options: trim `bay-urban`
(14.4 MB, nine layers), or raise the cap.

**M10 raised it, to 64 MB, with ADR 0031.** Worth reading that ADR before adding a route,
because it says two things this paragraph could not: M10 itself spends almost none of the new
headroom (waypoints cost ~2 kB across six `expected.json` files), and **the constraint that
actually binds is not disk but the 30-minute CI job**, which every one of these fixtures is
read by on every run. The Windows job was at 26m2s after M9.

## Reproducing

`route.gpx` is **not an input** — the harness runs this in generate mode, from the `from`/`to`
in `request.yaml`, and ignores it. It is kept because the corridor and the cassette were frozen
against it and re-recording needs the same geometry:

```powershell
uv run longrun build-region deploy/regions/boston.yaml
uv run python deploy/graphhopper/scripts/add_lts_tags.py `
    data/osm/boston.osm.pbf data/osm/boston-lts.osm.pbf
./deploy/graphhopper/import-lts.ps1 -Region boston -Force
./deploy/graphhopper/run.ps1        -Region boston

uv run longrun freeze-fixture tests/golden/routes/boston-winter/route.gpx `
    --out tests/golden/routes/boston-winter/fixtures --layers ways --buffer-m 300 `
    --dem "/vsicurl/https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/n43w072/USGS_13_n43w072.tif"
# ^ writes everything then does not exit; kill it after the last line (freeze.py documents why)

uv run longrun freeze-cassette tests/golden/routes/boston-winter/route.gpx `
    --date 2026-01-15 --out tests/golden/routes/boston-winter/cache.sqlite `
    --fixtures tests/golden/routes/boston-winter/fixtures `
    --with-routes --router http://localhost:8995 --graph "2026-09-13+lts1"
```

`freeze-cassette --with-routes` records the map-match and a detour and **not** the opening
route, so generate mode still misses. One online `longrun plan` against the same `--cache` and
the same `--snapshot` fills it; the snapshot is what makes the key match, because
`graph_identity` reads its `graph` pin.

## The first snapshot with a `graph` pin

`snapshot.json` carries `graph: "2026-09-13+lts1"` and `region: "boston"`. The five older
routes carry neither, so `SnapshotPins.graph_identity` falls back to `osm_extract_date` for
them and every cassette recorded before M9 replays untouched. This one keys on the extract
*and* the LTS rules, so a graph rebuilt under a changed `LTS_VERSION` misses loudly instead of
replaying answers no graph would now produce. That is the whole reason the field exists.

## Known weakness

**It is `rounds: 0`.** This route scores once; the loop is `loop-bayarea`'s subject and there
is no reason for two. So it pins nothing about arbitration, and a darkness flag that ought to
have driven a reroute would not be noticed here.
