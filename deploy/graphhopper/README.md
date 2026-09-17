# GraphHopper config

Flexible/LM mode so the custom model is a query-time parameter (scope 4.3, 7.1). CH is off
deliberately: a contraction hierarchy would freeze the custom model into the preparation.

Needs, per region:
- pedestrian and car profiles (car for `crew_points` drive times)
- encoded values including the offline LTS 1-4 score computed in PostGIS at import
- custom areas for avoid-polygons and per-polygon priority
- map matching enabled (history import and verification)
- elevation from the region DEM

## What is here

```
config-bayarea.yml           baseline: foot + car, LM, no LTS
config-bayarea-lts.yml       the same + the custom `lts` encoded value  <- the real one
config-bayarea-fallback.yml  decision 0001's escape hatch: LTS smuggled into `track_type`,
                             no custom Java anywhere
run.ps1                      start the server (stock jar; -Config selects the region)
import-lts.ps1               build a graph containing `lts` (needs the wrapper)
lts-wrapper/                 4-class Maven module: the custom ImportRegistry. See its README.
scripts/clip_pbf.py          bbox clip with complete-ways semantics (pyosmium IdTracker)
scripts/add_lts_tags.py      write `lts=1..4` from osm_lts.way_lts into a .pbf before import
scripts/verify_lts_routing.py  prove LTS actually steers routing (--ev lts | track_type)
```

`config-phoenix-lts.yml` is not listed above because it should not be hand-written. Region
configs are **generated from the region spec** — see "One config per region" below.

## Why import and serve are separate commands

GraphHopper consults an `ImportRegistry` only in `prepareImport()`, i.e. only on the import path.
`load()` rebuilds the EncodingManager from the stored graph, so **the stock
`graphhopper-web` jar serves an LTS graph unmodified**. Only the build step needs custom Java, and
`GraphHopperBundle.run()` gives no seam to inject the registry into a running server — hence
`import-lts.ps1` (wrapper) then `run.ps1` (stock).

Import and serve must use the same config file: `load()` refuses a graph whose stored `profiles`
string differs from the configured one.

Full evidence and timings: `docs/decisions/0001-graphhopper-lts-encoded-value.md`.

## Region build, start to finish

```powershell
# 1. clip the region out of a Geofabrik state extract
uv run python deploy/graphhopper/scripts/clip_pbf.py `
    data/osm/norcal-latest.osm.pbf data/osm/bayarea.osm.pbf "--bbox=-123.2,36.9,-121.5,38.5"

# 2. load OSM into PostGIS and compute the offline LTS score per way. This is what fills
#    `osm_lts.way_lts`, and step 3 will not run without it.
uv run longrun build-region deploy/regions/bayarea.yaml

# 3. write the score onto the ways as a synthetic `lts` tag
uv run python deploy/graphhopper/scripts/add_lts_tags.py `
    data/osm/bayarea.osm.pbf data/osm/bayarea-lts.osm.pbf

# 4. import with the wrapper, serve with the stock jar
mvn -f deploy/graphhopper/lts-wrapper/pom.xml package
./deploy/graphhopper/import-lts.ps1 -Config deploy/graphhopper/config-bayarea-lts.yml
./deploy/graphhopper/run.ps1        -Config deploy/graphhopper/config-bayarea-lts.yml
```

Step 2 is new in M9 and the ordering is not optional. `add_lts_tags.py` used to carry its own
`lts_for_way`, a tag-only approximation its docstring called a placeholder; it now reads
`osm_lts.way_lts`, computed by the same `lts_from_tags` every scorer calls (ADR 0025). On the
Ozarks the two disagreed about **469 of 1,600 ways**, so the LTS a plan reported was never the
LTS its route had been drawn to avoid.

There is no fallback scoring path, deliberately. The script refuses to write a `.pbf` when
fewer than 95% of the extract's roads carry a row, because a graph built on missing scores
imports cleanly, serves routes, and is wrong in a way nothing downstream can see.

`scripts/` needs pyosmium, which since M2 is a declared `ingest` extra rather than a
manual install: `uv sync --extra ingest`. ADR 0010 makes that the same dependency the OSM
loader uses, so a region build has one pbf reader and not two.
