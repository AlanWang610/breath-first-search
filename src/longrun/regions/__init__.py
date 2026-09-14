"""Region build: a bounding polygon plus the five idempotent steps of scope 13.

    1. OSM state extract(s) -> offline LTS per way -> GraphHopper graph with LTS encoded
    2. OSM, HPMS, Overture, PAD-US, NHD, TIGER, intersecting GTFS feeds -> PostGIS
    3. 3DEP DEM and canopy height -> DSM (SVF is per corridor on first use, not at build)
    4. Resolve jurisdictions crossed and look up which adapters exist
    5. Emit the coverage report

Steps are idempotent and recorded in a manifest, so a partial build resumes.
"""
