"""Region build: a bounding polygon plus the idempotent steps of scope 13.

    1. OSM state extract(s) -> GraphHopper graph with LTS encoded  (`build.py`)
    2. OSM, HPMS, Overture, PAD-US, NHD, TIGER, intersecting GTFS feeds -> PostGIS
    -. offline LTS per way -> `osm_lts.way_lts`                    (`lts.py`)
    3. 3DEP DEM and canopy height -> DSM (SVF is per corridor on first use, not at build)
    4. Resolve jurisdictions crossed and look up which adapters exist
    5. Emit the coverage report

Six steps for scope 13's five. The offline LTS score is the middle clause of §13 step 1 and
runs between steps 2 and 3, because it reads `osm.ways` and step 2 is what loads it — the
scope's own ordering puts a PostGIS computation before the PostGIS load. See ADR 0025.

Steps are idempotent and recorded in a manifest, so a partial build resumes.
"""
