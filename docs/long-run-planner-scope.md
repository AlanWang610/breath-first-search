# Long-Run Planner Agent — Scope

Status: draft v0.4

## 1. Purpose

An agent that turns a natural-language request ("run from SF Caltrain to San Jose Diridon in one day, avoid El Camino") or a user-supplied GPX into a verified route plus a plan sheet, using a real router and geospatial tools. Target use: point-to-point and loop runs of 20–100 km where traffic stress, heat, water gaps, and bailouts decide success.

Geographic scope: **anywhere in the United States.** Every data source in this document is chosen because it has national coverage or a national standard behind it; state- or city-specific sources are adapters behind a common interface, not the design. The SF Bay Area is the first test region, nothing more.

## 2. Non-goals

- Real-time navigation or live re-routing during the run
- Multi-day trips with lodging
- Training plans; nutrition beyond resupply logistics
- Social features, sharing, or any cross-user data aggregation
- Watch-side integration (on-device apps); export files only
- Pushing routes to Strava (its API does not support route creation)
- Coverage outside the US (the architecture is portable; data adapters are US-only)
- Pre-built tiles for the whole country; regions are stood up on demand from national sources (§13)
- Multi-user auth, hosted regions

## 3. Design principles

1. **The LLM does not draw routes.** It translates intent into constraints, waypoints, and costing weights. The router draws the line.
2. **Scorers measure; preferences assign cost.** No scorer decides whether sun, hills, or dirt are good or bad. Each returns a measurement; the preference profile (§6.3) maps it to cost with a user-supplied sign and weight. Only safety floors are preference-independent.
3. **Time-aware everything.** Shade, daylight, store hours, and transit are evaluated at the projected arrival time at each point, not at the start time.
4. **Worst-segment reporting.** Every scorer returns the worst N segments with reasons. No single aggregate score.
5. **Position-dependent weighting.** Hostile conditions late in the run are penalized more than early (§8.2).
6. **Report coverage honestly.** Every plan states which data sources were checked and which were not.
7. **Local-first for personal data.** Run history is processed locally; only derived curves are retained.
8. **Router behind an interface.** `core/` exposes `route`, `alternatives`, `map_match`; the costing model is an opaque object the router adapter owns, so the router can be swapped without touching scorers or the agent.
9. **Tool and CLI before UI.** Any capability exists as a tool and a CLI path before it gets a UI control. The web UI is a client, never the only way to do something.

## 4. Architecture

```
User ─► agent/ (thin orchestrator: explicit planning loop, LLM at fixed points)
              │  MCP
              ▼
        tools/ (MCP server; Pydantic schemas; stateless)
              │
              ▼
        core/  (deterministic geospatial library; zero LLM dependency; all tests here)
              │
     ┌────────┼──────────┬─────────────┬──────────────┐
     ▼        ▼          ▼             ▼              ▼
 GraphHopper PostGIS   Raster store   External APIs   Adapters
 (router,    (OSM,     (DEM, DSM,     (NWS, AirNow,   (WZDx, 511,
  custom     AADT,     canopy, SVF    GTFS, FCC,      portals, LLM
  models)    places)   tile cache)    NOAA tides)     extraction)
```

### 4.1 Layers
- `core/`: Python (geopandas, shapely, rasterio, pyproj, gpxpy, fitdecode, pvlib, numba for the ray-cast). Every scorer is a pure function `(gpx, context) → SegmentMeasurements`. Golden-route regression tests run here with no model in the loop.
- `tools/`: MCP server wrapping `core`. The same tools serve the CLI, a chat client, the orchestrator, and the web UI without rewrapping.
- `agent/`: the §8 loop as explicit code. The LLM is called at fixed points with structured outputs (Pydantic schemas, or Pydantic AI as the call wrapper): parse intent, choose among same-tier alternatives, write trade-off explanations, draft tier-4 extractions, propose preference updates. It is not handed the whole tool bag to sequence freely.

### 4.2 Agent framework
No graph-workflow framework. Control flow is known in advance, so a state-graph library adds abstraction over a loop that fits in ~150 lines. Human-in-the-loop pauses (same-tier trade-offs, preference confirmation) are handled by persisting the scratchpad and returning a `needs_input` status; the job resumes from the scratchpad. Revisit only if the loop becomes a genuinely LLM-decided branching graph or durable execution across many concurrent users becomes a requirement.

### 4.3 Router
GraphHopper, chosen over Valhalla. Custom models make tag-based penalties and the offline LTS encoded value (§7.1) query-time configuration; custom areas cover avoid-polygons and per-polygon priority; map matching and elevation are built in. Valhalla's pedestrian costing has no speed/lane/AADT knobs, and extending it means Lua tag-transform tricks or C++ changes. Valhalla's advantages — tiled graph for continental scale, native GTFS multimodal routing, richer `trace_attributes` on map match — are reachable through the router interface if they become necessary.

### 4.4 Infrastructure
GraphHopper in Docker per region; PostGIS for vectors (Overture ingested straight from GeoParquet on S3 via DuckDB spatial with a bbox filter); rasters as COGs with windowed reads; region build as a Typer CLI of idempotent steps with a manifest; plans run as async jobs with progress events; external API results cached by `(tool, args hash, date)` in SQLite or Redis.

Route state (current GPX, per-segment measurements, ETAs, locks, active preference profile) lives in a scratchpad the orchestrator reads and writes between tool calls. The scratchpad plus manifest is the stored plan.

## 5. Data stores

All base layers have national coverage. A region is a bounding polygon; the build pulls the intersecting slice of each national source.

| Store | Contents | Source (national) | Refresh |
|---|---|---|---|
| GraphHopper graph | Routable graph with encoded values incl. offline LTS score; custom-model pedestrian and car profiles | OSM US extract by state (Geofabrik) | Weekly |
| PostGIS | OSM ways + tags | osm2pgsql | Weekly |
| PostGIS | AADT, functional class, posted speed where populated | FHWA HPMS (all 50 states + DC, ArcGIS feature services) | Annual |
| PostGIS | Building footprints and heights | Overture Buildings | Quarterly |
| PostGIS | Places: water, toilets, food, urgent care | OSM amenities + Overture Places | Weekly |
| PostGIS | Park and protected-area boundaries, managing agency | USGS PAD-US | Annual |
| PostGIS | Hydrography (flowlines for ford detection) | USGS NHD | Annual |
| PostGIS | Jurisdiction boundaries (state, county, place) | Census TIGER | Annual |
| PostGIS | Transit stops and schedules | GTFS via Mobility Database / Transitland | Weekly |
| Raster | DEM | USGS 3DEP (10 m nationwide; 1 m LiDAR where available) | Annual |
| Raster | Canopy height | Meta/WRI 1 m global canopy height | Annual |
| Raster | Sky view factor tiles | Derived from DSM = DEM + canopy + building heights; computed on demand per route corridor (300–500 m buffer), DSM downsampled to 2 m, cached as COG tiles keyed by tile ID. Region-wide 1 m SVF is 10⁹–10¹⁰ pixels for a metro region and mostly never used; the direct-sun ray-cast is on demand anyway and shares the corridor extraction | On first use per tile |
| Cache | Weather, AQ, closures, extracted records (keyed by date) | Runtime | Per plan |
| User | Preference profile (§6.3), place notes, derived pacing curves, accepted-road set, habits | User statements and uploads | On update |

Optional layer, off by default: Strava global heatmap tiles via user-supplied cookies. Not in the Strava API; ToS risk sits with the user. The hostility scorer must run without it.

## 6. Inputs

### 6.1 Request
- Entry mode: **generate** (start/end, optional via points) or **repair** (user GPX via `import_route`; the loop starts at scoring with the user's line as initial state — expected to be the common mode for experienced users)
- Start and end points (address, transit stop, or coordinates); loop flag
- Date and intended start time, or a start-time window
- Target distance or acceptable range
- Constraints (§6.4)
- Per-run overrides of any preference in §6.3 (do not persist unless confirmed)

### 6.2 Run history (optional, high value)
Accepted formats, in order of preference:
1. FIT files (Garmin native; Strava bulk export includes originals)
2. Strava bulk export zip (`activities.csv` + original files)
3. Garmin Connect export
4. Strava OAuth (streams endpoint) — convenience path only; bulk export preferred for licensing reasons

Derived, then raw files discarded:
- Grade-adjusted pace curve (median `velocity_smooth` per 2% grade bin, stopped time excluded)
- Fatigue drift: pace and HR/pace ratio vs. distance on the longest efforts
- Surface and descent sensitivity
- Accepted-road set: map-matched OSM ways run ≥2 times (prior, not constraint)
- Habits: typical start times, walk breaks on climbs

Caveats surfaced to the user: race efforts excluded or flagged; extrapolation unreliable if no history run ≥50% of target distance; first/last 500 m of each activity stripped before map-matching.

Without history, the pacing model uses a population grade-adjusted curve (Minetti) with a default fatigue drift.

### 6.3 Preference profile (persistent, per user)

Scorers never attach a sign to a measurement; this profile does. Stored as versioned YAML/JSON (e.g. `~/.longrun/profile.yaml`; a table if hosted). Every entry carries `value`, `weight`, `provenance` (`stated` | `inferred` | `default`), and `updated`.

| Key | Meaning | Default |
|---|---|---|
| `sun` | `{cool, hot, pivot_c}`: weight in −1..+1 (seeks shade ↔ seeks sun) applied when forecast temperature at ETA is below/above `pivot_c`. Not split by time of day: "sun in the morning" means "sun when it's cool", and temperature at ETA is already available from pacing + `microclimate`. Seasonal and heat-acclimation cases (`hot: +0.3`) fit the same three numbers | `{cool: 0, hot: 0, pivot_c: 22}` |
| `water_gap_max_min`, `toilet_gap_max_min` | Longest acceptable gap at projected pace | 90 / 150 |
| `carry_capacity_ml` | Water carried; converts gaps to refill needs | 500 |
| `traffic_tolerance` | Highest LTS that is *not* a soft flag | 2 |
| `surface` | paved / dirt / mixed, with weight | mixed, low weight |
| `grade` | Max sustained climb and descent %, hills-seeking vs. avoiding | 10 / 10, neutral |
| `detour_tolerance_pct` | Extra distance accepted to clear a soft flag | 10 |
| `stops_tolerance` | Signalized stops per km before soft flag | 3 |
| `darkness_tolerance` | Accept unlit segments in darkness | false |
| `scenery_vs_directness` | −1..+1 | 0 |
| `pacing` | Derived curves from §6.2 | population default |

Rules:
- **Sun is not assumed bad.** With both `sun` weights at 0 the shade fraction is reported but not scored. Heat stress (WBGT) is scored regardless, because it is physiology, not preference.
- **Per-run overrides don't persist** unless the user confirms ("keep that"). Conversational statements ("I don't mind sun") are written by the LLM as a proposed field/value and confirmed before saving. `stated` always overrides `inferred`; `inferred` entries come only from post-run feedback and carry low weight.
- **Safety floors are outside the profile and not lowerable:** WBGT hard threshold, legality, hard crossings, LTS 4 as a hard flag. A user can raise a floor, not lower it.
- **Elicitation:** no up-front questionnaire. A preference question is permitted only when two candidate alternatives differ mainly on one profile axis and that axis is still `default`; the question is then concrete (which option, what differs, at what mile) and the answer is stored as `stated`. Cap 3 questions per plan. Two exceptions asked at first use because they affect nearly every plan: `traffic_tolerance` and `carry_capacity_ml`. If history is uploaded, `surface` and `grade` are inferred and not asked. Questionnaire answers to situations the user hasn't faced would be stored as `stated` with the same authority as real ones; in-context elicitation keeps `stated` meaningful.

### 6.4 Constraints (hard, distinct from preferences)
- **Time:** arrive-by (e.g. last train), earliest start (e.g. gate opens), total time budget. Evaluated against the pacing model; violations are hard flags.
- **Via points** and **must-avoid ways/areas** by name or polygon.
- **Locked segments:** ranges the loop may not reroute (set by `lock_segment`, or automatically when the user chooses between alternatives). **Pinned waypoints** become via points.
- **Budget per plan:** target end-to-end latency ≤ ~3 min; hard caps on external API calls and imagery tiles; ray-cast resolution degrades before the time budget is exceeded. Budgets are recorded in the manifest.
- **Data snapshot pinning:** the manifest records OSM extract date, HPMS vintage, DEM resolution, canopy version, GTFS feed versions, and adapter versions, so a plan is reproducible and differences between plans are attributable.
- **Graceful no-route:** the router adapter returns the nearest-connected failure (which via-point pair could not be joined) rather than a bare error; pedestrian graphs disconnect at bridges and interchanges more often than expected.

## 7. Tools

Signatures are illustrative. All route-consuming tools accept a GPX and return per-segment measurements plus a `worst` list.

### 7.1 Routing and geometry
| Tool | Description | Source |
|---|---|---|
| `route(waypoints, profile, avoid_polygons, custom_model)` | Pedestrian route with a custom model (flexible/LM mode, so the model is a query-time parameter); returns GPX + per-segment OSM tags and encoded values | GraphHopper |
| `alternatives(gpx, segment_idx, k)` | k variants for one segment via custom areas / priority overrides, re-joined to the route | GraphHopper |
| `elevation_profile(gpx)` | Gain/loss, grade histogram, longest sustained climbs and descents | DEM |
| `map_match(track)` | Snap a raw track to OSM ways (history import, verification) | GraphHopper map-matching module (Valhalla `trace_attributes` is the fallback if per-edge attributes prove too thin) |

Custom model:
- An LTS 1–4 score (Furth methodology: speed × lanes × separation, AADT as modifier where present) is computed offline per way in PostGIS and written as an encoded value at import.
- `priority` is a function of ~6 parameters: LTS 2/3/4 multipliers, unpaved multiplier, path/footway bonus, missing-sidewalk-on-collector multiplier. Surface and hills terms are set from the preference profile at query time.
- Hard exclude `access=private`, `foot=no`, `highway=motorway`, railway ROW.
- Position weight w(d) is applied in the scoring loop, not the router (§8.2).
- **Tuning:** the ~6 parameters are fit against pairwise route preferences — the user's manual GPX edits (rejected vs. chosen segments) and the accepted-road set from history — by grid or Bayesian search maximizing agreement, with a detour-ratio regularizer so the optimizer can't solve the problem by routing through every park. Acceptance metrics: fraction of length at LTS ≥3, count of LTS 4 segments, detour ratio vs. shortest legal route, published for a set of reference routes.

### 7.2 Runnability
| Tool | Description | Source |
|---|---|---|
| `segment_hostility(gpx)` | LTS 1–4 per segment plus severity; optional heatmap term | OSM tags; HPMS AADT, functional class, and `SPEED_LIMIT` where populated; posted speed from OSM/Overture; locally extracted speed surveys (CA E&TS is one adapter); optional heatmap; place notes as prior |
| `crossings(gpx)` | Crossings of roads above a class threshold; signalized vs. unsignalized; count per km | OSM |
| `stop_density(gpx)` | Signalized stops and gates per km | OSM |
| `surface_profile(gpx)` | Fraction by surface type; flags for sustained shoulder running on crowned roads | OSM; imagery spot-check |
| `cue_sheet(gpx)` | Turn list, turn count, ambiguity flags | GraphHopper instructions |

### 7.3 Time and pacing
| Tool | Description | Source |
|---|---|---|
| `pacing_model(gpx, profile)` | ETA at every point from grade-adjusted pace × fatigue drift; stop allowances added | User curves or population default |

Every time-dependent tool below takes the ETA vector from this tool.

### 7.4 Environment
| Tool | Description | Source |
|---|---|---|
| `sun_exposure(gpx, etas)` | Measurement only: per-km direct + diffuse irradiance integrated at arrival times; direct via DSM ray-cast (1 m where LiDAR exists), diffuse via sky view factor; cloud-scaled. Output J/m² and shaded fraction. Optional vertical-cylinder irradiance / MRT via SOLWEIG. Sign and weight come from `sun` in the profile | pvlib (Ineichen–Perez), DSM/SVF corridor tiles, NWS/Open-Meteo cloud cover |
| `heat_stress(gpx, etas)` | WBGT or UTCI per segment from sun exposure + temp + humidity + wind | Above + forecast |
| `microclimate(gpx, etas)` | Hourly forecast at multiple points along the route, not a single station | NWS API (api.weather.gov) primary; Open-Meteo fallback |
| `air_quality(gpx, date)` | AQI forecast and current PM2.5 along the corridor | AirNow (EPA), PurpleAir |
| `lighting(gpx, etas)` | Daylight status at each ETA; `lit=*` coverage for segments in darkness | Solar calc, OSM |

### 7.5 Resupply
| Tool | Description | Source |
|---|---|---|
| `services_along(gpx, buffer_m, types)` | Drinking water, toilets, convenience stores, cafes, parks with fountains, urgent care | OSM amenities, Overture Places, PAD-US park polygons; local park-facility datasets as optional adapters |
| `resupply_schedule(gpx, etas)` | Which services are open at arrival; max dry gap and toilet gap in minutes; suggested carry | Above + opening hours |

### 7.6 Access, closures, legality
| Tool | Description | Source |
|---|---|---|
| `closures(gpx, date)` | Overlaps with active/planned street and lane closures; returns coverage manifest | Tiered adapters (§7.10): (1) WZDx feeds; (2) state 511 APIs; (3) city/county open-data portals; (4) LLM extraction from agency pages and PDFs. Tiers 3–4 are brittle by design; every plan reports which tiers returned data for each jurisdiction crossed |
| `trail_status(gpx)` | Park alerts, seasonal closures | Managing agency resolved from PAD-US → NPS Alerts API, USFS/BLM alert pages, state park systems, regional districts (LLM extraction) |
| `access_hours(gpx, etas)` | Park gate and dawn-to-dusk violations at ETA; tide conflicts for beach segments | PAD-US agency → agency pages (extraction); NOAA CO-OPS tides |
| `legality(gpx)` | Pedestrian-prohibited segments (freeways, bridges, private, railway) | OSM tags; hard fail in verify |
| `hazards(gpx)` | At-grade rail crossings, tunnels, bridges with no walkway or high wind exposure, water crossings (NHD flowlines crossing a way with no `bridge` tag), cattle guards, seasonal snow above an elevation threshold; NWS flood and red-flag warnings by date | OSM, USGS NHD, NWS alerts |

### 7.7 Logistics
| Tool | Description | Source |
|---|---|---|
| `transit_at(point, time)` | Departures at start/end/bailout points | GTFS for every agency intersecting the region; GTFS-RT where published |
| `bailouts(gpx, etas)` | Transit stops and rideshare-plausible pickup points within reach of each segment, with plausibility flags | GTFS, OSM |
| `crew_points(gpx, etas)` | For supported runs: road-accessible meet points with parking, drive time from the previous point, runner ETA | OSM, GraphHopper car profile |
| `cell_coverage(gpx)` | Carrier coverage gaps | FCC BDC |
| `start_time_optimizer(gpx, date, window)` | Sweep start times across the window; re-run time-dependent scorers; return a table of heat, sun, daylight, service-hours, and transit outcomes | cached scorers |

### 7.8 Editing and plan management
| Tool | Description |
|---|---|
| `import_route(gpx)` | Load a user route as initial state (repair mode) |
| `lock_segment(gpx, range)` / `pin_waypoint(point)` | Exclude a range from rerouting; add a via point. User-chosen alternatives auto-lock |
| `route_diff(gpx_a, gpx_b)` | Where two routes differ, with per-difference score deltas; used for trade-off presentation and iteration review |
| `distance_markers(gpx, interval)` | Waypoints every N km/mi with ETA |
| `refresh_plan(plan, date)` | Re-run only date-sensitive scorers (closures, weather, AQ, trail status) against a stored plan; everything else from cache |
| `place_notes(way_or_area, note)` | Persistent user notes on ways/areas ("overgrown in summer"); stored as `stated`, fed to hostility as a prior. Separate from the preference profile |
| `export(plan, format)` | GPX 1.1; FIT course file with course points (turns, water, markers) for Garmin turn-by-turn; TCX |

### 7.9 Verification and I/O
| Tool | Description |
|---|---|
| `gpx_read(file)` | Parse GPX (tracks, routes, waypoints) |
| `gpx_write(route, waypoints)` | Emit GPX 1.1 with waypoints for water, toilets, bailouts, hazards, markers |
| `gpx_verify(gpx)` | Checklist below; pass/fail per check with offending segments |
| `imagery_tile(lat, lon, zoom)` | Aerial tile for vision spot-check of a flagged segment; hard cap ~10 calls per plan |
| `render(gpx, layers)` | Static map image per segment for human review |

`gpx_verify` checks:
1. Valid GPX 1.1 XML
2. Every trackpoint within 15 m of a routable way with `foot != no`
3. No inter-point gap > 100 m
4. Elevation consistent with DEM (no interpolation artifacts, no spikes)
5. No `access=private`, `highway=motorway`, or railway ROW segments
6. No overlap with active closures (from cached `closures` result)
7. Total distance within tolerance of target
8. No unsignalized crossing of `highway=trunk|primary` with `maxspeed>40`
9. No hard time-constraint violation at projected pace
10. Locked segments unchanged from the locked geometry

### 7.10 Adapter registry
Jurisdiction-specific sources are plugin modules declaring `jurisdiction` (Census GEOID or PAD-US unit ID), `kind` (closures | trail_status | access_hours | speed_survey), `tier` (1 WZDx, 2 511 API, 3 open-data portal, 4 LLM extraction), and `fetch(polygon, date) → Features` in a common schema (geometry, start/end, category, confidence, source URL). Registered via Python entry points. Discovery: spatial-join the route buffer against TIGER boundaries and PAD-US → jurisdiction IDs → registry lookup. Missing adapter → generic tier-4 search-and-extract with confidence ≤0.5 and a manifest entry marked unverified. Successful tier-4 extractions can be promoted to a drafted adapter for human review; this is how the adapter set grows without hand-writing thousands of scrapers.

## 8. Planning loop

### 8.1 Steps
1. Parse request → structured constraints (start, end, date, start time or window, distance, avoids, via points, time constraints, locks) and per-run preference overrides.
2. Ingest history if provided → pacing profile, accepted-road prior. Load preference profile and place notes.
3. `route` with pedestrian custom model, avoids, and via points — or `import_route` in repair mode (skip to 4).
4. `pacing_model` → ETAs.
5. Run scorers: `segment_hostility`, `crossings`, `sun_exposure`, `heat_stress`, `resupply_schedule`, `closures`, `trail_status`, `access_hours`, `legality`, `hazards`, `bailouts`, `lighting`.
6. Collect worst segments across scorers, excluding locked ranges. For each, `alternatives` → re-score. Same-tier conflicts are surfaced to the user (persist scratchpad, `needs_input`); the chosen alternative auto-locks. Iterate up to 5 rounds or until no scorer flags above threshold.
7. If a start-time window was given, `start_time_optimizer` before the final round.
8. Optional `imagery_tile` spot-checks on remaining ambiguous segments.
9. `gpx_verify`. On failure, return to step 6 with the failing segment.
10. Emit outputs (§9).

### 8.2 Position weight
w(d) = 1.0 for the first 40% of distance, rising linearly to 2.0 at 100%. Piecewise linear, not stepped: stepped weights create boundaries the optimizer games (a hostile segment shoved to just before the step). Applied to hostility; applied to heat stress with a lower ceiling of 1.5 since ETA already carries time of day and double-counting is the risk. Not applied to legality. Lives in the scoring loop, not the router (the router can't know distance-along-route before the route exists): route with static weights, score with w(d), reroute flagged late segments with local avoid polygons or priority overrides. Converges in 2–3 rounds.

### 8.3 Thresholds
Each scorer returns per-segment severity in [0, 1] plus hard/soft. Initial values (soft / hard):

| Scorer | Soft flag | Hard flag |
|---|---|---|
| Hostility | LTS 3 (profile `traffic_tolerance` + 1) | LTS 4 |
| Crossings | unsignalized crossing of secondary | unsignalized crossing of primary/trunk with speed >40 |
| Dry gap | >90 min (profile) | >150 min; both scale down with WBGT |
| Heat | WBGT >26 °C | WBGT >30 °C |
| Grade | sustained >10% descent >1 km (profile) | — |
| Daylight | segment in darkness with `lit=no` (profile) | — |
| Time constraint | — | any violation |
| Legality / hazards | — | any |

Soft thresholds marked "(profile)" are read from §6.3; hard thresholds are fixed safety floors.

### 8.4 Arbitration
Lexicographic by tier: safety (legality, hard hostility, hard crossings, hazards, time constraints) → physiological (dry gap, heat) → comfort (surface, stops, shade, turns, scenery). Within a tier, weighted sum of severity × w(d) × profile weight. A shadier alternative never wins against a hard-hostility alternative; a shadier alternative with LTS 2→3 is a same-tier trade. Same-tier conflicts between alternatives are not resolved silently: both are presented with a one-line trade-off ("A adds 0.8 km and a signalized crossing; B is fully shaded but has 600 m without sidewalk at mile 41"), which is the LLM's job in this loop. Residual flags are always listed.

## 9. Outputs

- **GPX** with track and waypoints (water, toilets, food, bailouts, hazards, gates, distance markers); **FIT course** and TCX via `export`.
- **Plan sheet** as markdown and as a self-contained HTML file (embedded MapLibre map, elevation profile, flagged segments with IDs, alternatives as dashed lines, no server required). Contents:
  - Distance, gain/loss, projected duration and finish time
  - Elevation profile and grade summary
  - Worst-N segments per scorer with reasons and what was tried
  - Water and toilet gaps in minutes; suggested carry
  - Sun exposure and heat stress by hour (exposure reported neutrally; scored per profile); daylight status
  - Start-time table if a window was given
  - Same-tier trade-offs left to the user, each with a one-line comparison
  - Bailout and crew points with times
  - Cue sheet
  - Coverage manifest: sources queried, sources unavailable, extraction confidence for scraped records
  - Explicit warnings (pacing extrapolation, missing sidewalk data, unchecked jurisdictions)
  - Manifest: data snapshot versions, tool calls, budgets used
- **Stored plan** (scratchpad + manifest) so `refresh_plan` and `route_diff` can run later.

## 10. Interfaces

Three surfaces on the same `tools/` layer.

### 10.1 CLI
`longrun plan request.yaml`, `longrun repair route.gpx`, `longrun build-region poly.geojson`, `longrun refresh plan.json`, `longrun export plan.json --fit`. This is the test harness; every golden test runs through it.

### 10.2 MCP in a chat client
Chat is the right surface for elicitation, same-tier trade-off questions, and preference updates. The agent references segment IDs that match the plan sheet; the user replies in natural language ("use B for segment 14", "avoid Foothill Expressway", "I don't mind sun when it's cool") and those become locks, constraints, or confirmed profile entries.

### 10.3 Web UI
Chat is poor at spatial review and the HTML plan sheet is read-only; the web UI is the editable version, kept small:

- **Map view** (MapLibre): route, flagged segments colored by tier with hover reasons, alternatives as dashed lines with score deltas, water/toilet/bailout/crew markers, sun-exposure and hostility as toggleable overlays.
- **Direct manipulation:** click a flagged segment → choose an alternative (auto-locks); drag to add a via point; select a range → lock/unlock; draw an avoid polygon. Each action re-runs only the affected scorers.
- **Timeline strip** under the map: elevation, ETA, temperature/WBGT, shade fraction, service gaps, daylight — aligned by distance; scrubbing highlights the map position.
- **Start-time slider** driving `start_time_optimizer` results.
- **Preference panel:** the §6.3 profile with provenance shown; edits are `stated`.
- **Chat pane** wired to the same agent, so elicitation and explanations happen next to the map rather than instead of it.
- **Plan list:** stored plans, `refresh_plan`, `route_diff` between versions, export buttons.
- **Region status:** which layers loaded, resolution, adapter coverage.

Stack: FastAPI over the async job runner (same jobs the CLI and MCP server use), React + MapLibre GL, plan schema as the API contract. Runs against the local `tools/` server.

## 11. Testing

- Golden routes with expected measurements and flags, checked into the repo, run through the CLI with no model in the loop.
- Adapter contract tests with recorded responses.
- A small eval set of routes with human-judged "would you run this" labels alongside the golden tests.
- Test regions, chosen for contrast on the dimensions that vary rather than geography:
  1. Dense, cold, transit-rich (Boston or NYC metro): darkness tolerance, `sun.cool`, stop density, easy bailouts, strong 1 m LiDAR.
  2. Hot, exposed, sprawling (Phoenix or Tucson): near-zero canopy, wide arterials, long service gaps, WBGT hard flags in ordinary conditions; verifies a `sun.hot > 0` user still gets a sane plan.
  3. Rural, thin data (Appalachia or Ozarks): sparse OSM sidewalk/fountain tags, PAD-US resolving to USFS/NPS, no GTFS, cell gaps, likely no WZDx or 511 API. Tests whether the coverage manifest tells the truth. Confirm WZDx feed status from the current registry before choosing.
  4. One state-line-crossing route (Kansas City MO/KS or Portland–Vancouver OR/WA): two DOTs, two 511 systems, two adapter sets on one route; catches jurisdiction-discovery bugs.
  Plus the SF Bay Area as the development region.

## 12. Known limits

- OSM sidewalk, shoulder, and fountain tagging is uneven outside dense areas. Missing tags are treated as unknown, not as absent, and reported.
- AADT exists for arterials and collectors only; local streets default to low volume.
- Closure and construction data is fragmented across jurisdictions. WZDx and 511 coverage varies by state; where neither exists the extraction tier is best-effort, and every plan reports what was not checked.
- Data quality varies regionally within the US: OSM completeness, 1 m LiDAR availability, GTFS publication, and open-data portals are all denser in metro areas. The coverage manifest surfaces this per plan.
- Sidewalk-vs-roadway position cannot be resolved from GPS traces or heatmaps.
- Pacing extrapolation beyond the user's longest recorded effort is a guess and is labeled as one.
- Strava heatmap: not in the API, cookie-gated, not open data. Optional and off by default.
- Imagery interpretation by a vision model is unreliable for fine features; used only as a capped spot-check.

## 13. Region packaging

A region is a polygon plus a build. `build_region(polygon)`:

1. Fetch the intersecting OSM state extract(s); compute the offline LTS score per way; build the GraphHopper graph with LTS as an encoded value
2. Load OSM, HPMS, Overture buildings/places, PAD-US, NHD, TIGER, and all intersecting GTFS feeds into PostGIS
3. Fetch 3DEP DEM (best available resolution) and canopy height; derive DSM (SVF is computed per corridor on first use, not at build)
4. Resolve the jurisdictions crossed (states, counties, places, park agencies) and look up which adapters exist for each
5. Emit a coverage report: which layers loaded, at what resolution, which jurisdictions have no closure adapter

The only California-specific code in the repository lives in adapters registered for California jurisdictions.

## 14. Licensing

Relevant because the project is open source and some outputs are derivative databases.

| Source | License | Consequence |
|---|---|---|
| OSM (Geofabrik extracts) | ODbL | Attribution; the offline LTS table and any published graph are derivative databases → ODbL if distributed |
| Overture | Mixed by theme (ODbL for transportation-derived themes; CDLA-Permissive for others) | Check per theme at ingest; record in manifest |
| HPMS, 3DEP, NHD, PAD-US, NWS, NOAA CO-OPS, FCC BDC, TIGER | US public domain | None |
| Meta/WRI canopy height | CC-BY | Attribution |
| AirNow | Public, attribution requested | Attribution |
| PurpleAir | API key + terms | Per-user key; not redistributed |
| GTFS feeds | Per agency, mostly permissive | Record per feed |
| Nominatim (geocoding) | ODbL (serves OSM) | Attribution; contact string required by its usage policy (ADR 0018) |
| Strava/Garmin exports | User's own data | Processed locally; never redistributed; Strava API data not used for cross-user modeling |
| Strava heatmap | Not open; cookie-gated | Optional adapter, user-supplied credentials, off by default |

The README states which outputs are ODbL-encumbered and how attribution is rendered in the plan sheet and web UI.
