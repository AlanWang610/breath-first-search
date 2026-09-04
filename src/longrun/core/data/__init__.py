"""Store clients (scope 5). All base layers have national coverage.

Planned modules:
    postgis.py       OSM ways+tags, HPMS, Overture buildings/places, PAD-US, NHD, TIGER, GTFS
    rasters.py       COG store with windowed reads; DEM, canopy, cached SVF tiles
    cache.py         external API results keyed by (tool, args hash, date)
    overture.py      GeoParquet on S3 via DuckDB spatial with a bbox filter
    gtfs.py          feed ingest from Mobility Database / Transitland; GTFS-RT where published
"""
