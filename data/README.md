# data/

Region builds, raster tiles, OSM extracts, and runtime caches. Gitignored: large,
regenerable, and in the case of OSM-derived artifacts, ODbL-encumbered if distributed
(scope 14).

Rebuild with `longrun build-region <polygon.geojson>`. User data (preference profile,
place notes, derived pacing curves) lives in `~/.longrun/`, not here, and run history is
processed locally with the raw files discarded after derived curves are extracted.
