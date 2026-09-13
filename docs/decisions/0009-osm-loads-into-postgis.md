# 0009 — OSM loads into PostGIS, not straight to GeoPackages

Status: accepted, 2026-09-10. Decides the shape of M3's first step.

## Context

Six of the twelve scorers report `unavailable` on the `bay-urban` golden because there is
no `ways` layer. `longrun freeze-fixture` is written, tested against a live database, and
has nothing to freeze from: `DEFAULT_LAYER_TABLES` maps `ways` to `osm.ways` and nothing
creates that table. This is the gap M1.7b named and M2 did not close.

M2 introduced a second, simpler shape while solving the building-height problem.
`core/data/overture.py` reads GeoParquet from S3 with duckdb and writes a GeoPackage
directly — **no database at all** — and `FileLayerStore` serves it. That worked well enough
that the obvious question is whether OSM should follow it.

## Decision

**OSM loads into PostGIS.** The Overture shortcut is not the pattern to generalise.

Three reasons, in order of weight.

**The freeze machinery already assumes a database, and it is tested.**
`freeze-fixture` goes through `PostGISLayerStore` specifically so that a committed
GeoPackage is *literally what the database answered* rather than a hand-drawn
approximation, and `tests/contract/test_freeze_fixture.py` holds it to that with a round
trip — down to the LTS level `lts_from_tags` reads off way 2. A GeoPackage-first OSM path
would bypass all of that, and the equivalence test between the two stores would be testing
a seam that half the layers no longer cross.

**Scope §13 is a region build, not a corridor fetch.** Its five idempotent steps load
national sources once and slice them per region. Overture-to-GeoPackage works because a
building corridor is a bbox query answerable in 45 seconds; OSM needs way-node
relationships, tag indexes and a spatial index that a per-corridor GeoPackage rebuild
would recompute every time. `deploy/postgis/init/01-schemas.sql` already creates the eight
schemas and `meta.layer_vintage` for exactly this.

**One ingest shape, not two.** HPMS, PAD-US, NHD, TIGER and GTFS all follow in M3 and all
are national extracts sliced per region. A second path that some layers take and others do
not is a fork in the freeze command, the vintage ledger and the equivalence test.

**Overture stays as it is, and is the exception rather than the precedent.** It earns that
because Overture publishes *cloud-native* GeoParquet with a published bbox column — a bbox
query is a row-group scan, not a table scan — and because M2 needed building heights before
M3 existed. When the region build lands, `overture.py` becomes its buildings step and loads
into `overture.buildings` like everything else; the direct-to-GeoPackage entry point stays
for ad-hoc corridors.

## Consequences

* M3 step 1 is `osmium extract` (already done for the Bay Area at M0.4) plus a load into
  `osm.ways`, `osm.nodes` and `osm.amenities`, with a `meta.layer_vintage` row.
* Anyone running the golden suite still needs no database — that is the whole point of the
  `LayerStore` seam and it is unaffected. PostGIS is a *build* dependency, not a test one.
* The tool is a decision M3 still has to make: `osm2pgsql` is the standard and needs a
  binary install; `pyosmium` is already a declared `ingest` extra and would mean writing
  the tag and geometry handling by hand. That is a smaller decision than this one and
  should be made against a measured load time for the 233 MB Bay Area extract.

## What would make us revisit

If the region build turns out to be the only consumer of PostGIS — if no scorer ever needs
a live query and every read is served from a frozen fixture — then the database is an
intermediate format and a directory of GeoPackages would do the same job with less to
install. That is worth re-asking after M3, when there is a second region to build and a
real measurement of what the build costs.
