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
scripts/add_lts_tags.py      write the synthetic `lts=1..4` tag into a .pbf before import
scripts/verify_lts_routing.py  prove LTS actually steers routing (--ev lts | track_type)
```

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
uv run python deploy/graphhopper/scripts/clip_pbf.py `
    data/osm/norcal-latest.osm.pbf data/osm/bayarea.osm.pbf "--bbox=-123.2,36.9,-121.5,38.5"
uv run python deploy/graphhopper/scripts/add_lts_tags.py `
    data/osm/bayarea.osm.pbf data/osm/bayarea-lts.osm.pbf
mvn -f deploy/graphhopper/lts-wrapper/pom.xml package
./deploy/graphhopper/import-lts.ps1 -Config deploy/graphhopper/config-bayarea-lts.yml
./deploy/graphhopper/run.ps1        -Config deploy/graphhopper/config-bayarea-lts.yml
```

`add_lts_tags.py` currently holds a crude tag-only LTS placeholder. Scope 7.1 replaces
`lts_for_way` with a lookup against the PostGIS Furth computation keyed on OSM way id; nothing
else in this directory changes.

`scripts/` needs pyosmium, which since M2 is a declared `ingest` extra rather than a
manual install: `uv sync --extra ingest`. ADR 0010 makes that the same dependency the OSM
loader uses, so a region build has one pbf reader and not two.
