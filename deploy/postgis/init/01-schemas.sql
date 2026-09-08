-- Extensions, layer schemas, and the vintage ledger.
--
-- Runs exactly once: `docker-entrypoint-initdb.d` executes only against an empty data
-- directory. Editing this file does nothing to a database that already exists; to apply a
-- change, `docker compose down -v` and bring it back up.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_raster;


-- One schema per layer group (scope 11, 13). Kept separate so a region rebuild can
-- truncate and reload one source without touching the others, and so `meta.layer_vintage`
-- has something stable to key on.
CREATE SCHEMA IF NOT EXISTS osm;       -- ways, nodes, amenities from the region extract
CREATE SCHEMA IF NOT EXISTS hpms;      -- AADT, arterials and collectors only (scope 12)
CREATE SCHEMA IF NOT EXISTS overture;  -- buildings, places
CREATE SCHEMA IF NOT EXISTS padus;     -- protected-area boundaries and access rules
CREATE SCHEMA IF NOT EXISTS nhd;       -- hydrography, for water crossings and hazards
CREATE SCHEMA IF NOT EXISTS tiger;     -- census boundaries, for jurisdiction discovery
CREATE SCHEMA IF NOT EXISTS gtfs;      -- transit feeds, for bailouts

-- Named `user_data`, not `user`, deviating from deploy/postgis/README.md: USER is a
-- reserved word, so a schema called `user` must be double-quoted in every statement that
-- touches it, forever. An unquoted `user.routes` is a parse error rather than an obvious
-- one, and that trap costs more than the honest name.
CREATE SCHEMA IF NOT EXISTS user_data; -- saved routes, locked segments, run history


-- Scope 6.4 requires the plan manifest to pin every source's vintage, so a plan is
-- reproducible and two plans that differ can be told *why* they differ. Recording it here,
-- at load time, means the manifest reads what the database actually holds rather than what
-- a loader remembered to report.
CREATE SCHEMA IF NOT EXISTS meta;

CREATE TABLE IF NOT EXISTS meta.layer_vintage (
    layer_schema text        NOT NULL,
    source       text        NOT NULL,
    -- Empty string, not NULL, for sources loaded once nationally: NULL is not comparable,
    -- and a primary key cannot contain one.
    region       text        NOT NULL DEFAULT '',
    -- Free text because vintages are not commensurable across sources: an OSM extract has
    -- a timestamp, HPMS has a year, GTFS has a feed version string. Storing each in its
    -- native form beats coercing all three into a date they do not have.
    vintage      text        NOT NULL,
    source_url   text,
    loaded_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (layer_schema, source, region)
);

COMMENT ON TABLE meta.layer_vintage IS
    'Source vintages for the plan manifest (scope 6.4). Written by regions/build.py.';


-- Index convention for the loaders (M3), recorded here because there are no tables yet to
-- index and the convention is easy to lose:
--
--   CREATE INDEX ON <schema>.<table> USING GIST (geom);
--
-- Every scorer begins by joining a route-corridor buffer against a layer, so this is the
-- one index that decides whether the ~3-minute end-to-end budget (scope 6.4) is met.
-- Geometry is stored in EPSG:4326 and intersected against a corridor polygon that was
-- buffered in the route's local UTM and transformed back - never buffered in degrees,
-- where 400 m of longitude is not 400 m of latitude.
