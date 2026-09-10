# 0010 — pyosmium loads OSM into PostGIS, and the speed argument for osm2pgsql does not survive measurement

Status: accepted, 2026-09-10. Settles the choice ADR 0009 left open.

## Context

[ADR 0009](0009-osm-loads-into-postgis.md) decided that OSM loads into PostGIS and named the
one thing it deliberately did not decide:

> The tool is a decision M3 still has to make: `osm2pgsql` is the standard and needs a
> binary install; `pyosmium` is already a declared `ingest` extra and would mean writing
> the tag and geometry handling by hand. That is a smaller decision than this one and
> should be made against a measured load time for the 233 MB Bay Area extract.

So it was measured. Both arms were made to produce the *same* contract — `way_id bigint`,
`tags jsonb`, `geom geometry(LineString, 4326)` — because a comparison against osm2pgsql's
stock `planet_osm_line` schema would be comparing two different jobs.

## The measurement

`data/osm/bayarea.osm.pbf`, 233 MB, 30.2 M nodes and 3.49 M ways, against PostGIS 3.5.2 on
Postgres 17 in Docker. Both arms keep the ways carrying `highway`, `railway`, `footway` or
`cycleway`, and both return **938,678 ways** — an exact agreement between two independent
implementations, which is worth more as a correctness check than either timing.

| | read | insert | index | total |
|---|---|---|---|---|
| osm2pgsql 2.3.1, flex output + Lua | 13 s | ~2 s | ~2 s | **17 s** |
| pyosmium 4.3.1, first attempt | 71 s | 133 s | 1 s | **206 s** |
| pyosmium 4.3.1, as written | 15 s | 3 s | 1 s | **19 s** |

**The first pyosmium number was wrong, and the way it was wrong is the finding.** A 12×
gap looks like a language boundary and is easy to accept as one. It was two mistakes of
mine, neither of them Python's:

* **133 s of "COPY" was not COPY.** It was `bytes.fromhex()` at parse time and `.hex()`
  again at insert time — 938,678 geometries round-tripped through `bytes` for no reason.
  Passing the factory's hex output straight to `COPY` puts the insert at **3.0 s**.
* **56 s of "read" was the filter being on the wrong side of the C++ boundary.** Testing
  `"highway" in dict(obj.tags)` in Python materialises a Python object for each of the
  2.55 M ways that are then discarded. The same test as an `osmium.filter.KeyFilter` runs
  in the C++ chain and never crosses: **70.7 s → 15.1 s**, same 938,678 ways out.

Corrected, the two tools are **the same speed**, and the argument has to be decided on
something else.

## Decision

**pyosmium**, and the reasons are all about what else is already in the build.

**The build already reads this file with pyosmium, twice.**
`deploy/graphhopper/scripts/clip_pbf.py` cuts the region extract with an `IdTracker`, and
`add_lts_tags.py` writes the offline LTS score back into the `.pbf` for GraphHopper to
parse as an encoded value — which is the mechanism [ADR 0001](0001-graphhopper-lts-encoded-value.md)
turns on. Those are steps 1 and 2 of the same §13 build as this load. Adopting osm2pgsql
would add a *third* pass over the same bytes by a *different* tool, and the pyosmium
dependency would not go away.

**It is already declared and locked.** `ingest = ["duckdb>=1.1", "osmium>=4.3"]`, resolved
in `uv.lock`, installed by `uv sync --all-extras`. osm2pgsql is not on PyPI and — checked
today — not in winget either, so on the development platform it is a manual zip from
osm2pgsql.org onto `PATH`, or a third-party Docker image. The measurement above used
`iboates/osm2pgsql`, which is nobody's official build. A region build that cannot be run
from a clean checkout plus `uv sync` is a region build most people will not run.

**One language.** The osm2pgsql arm needs a Lua config to produce our schema, and the
classification it encodes — which `amenity` values are a water source, which `barrier`
values stop a runner — is the same vocabulary `core/scorers/services.py` and
`stop_density.py` already state in Python. Written in Lua it is a second copy that nothing
checks against the first.

## Consequences

* `core/data/osm.py` is ingest, alongside `overture.py`, and **nothing in `core/` imports
  it** — same rule, same reason: `osmium` is an extra and `core/` must import on a bare
  `uv sync`. The classification vocabulary is pure and unit-tested in CI; the pbf reading
  and the database write are `network`-marked.
* **The load is an upsert on the OSM id, not a truncate.** OSM ids are global, so two
  regions that share a boundary way are making the same claim about it and the second load
  is a no-op. That is what makes the step idempotent in the sense §13 asks for, and it is
  why the region is recorded in `meta.layer_vintage` rather than as a column on every row.
* A way *deleted* from OSM upstream is not removed by a reload, because an upsert has
  nothing to key the deletion on. `--truncate` exists for a rebuild from scratch; a
  differential update is `osmium derive-changes` territory and is not in this milestone.
* Both filter placement and the hex round trip are the sort of thing that comes back. The
  loader states the C++-side filter as the reason it is shaped that way, and the `network`
  test that loads a real extract is where a regression would show as a wall-clock change.

## What would make us revisit

A national load. The tables here are sliced per region because §13 slices per region, and
17 s versus 19 s on 233 MB says nothing about 80 GB of planet, where osm2pgsql's `--slim`
mode and its flat-node store are a genuinely different architecture rather than a faster
loop. If a source ever has to be loaded once nationally and queried across regions — HPMS
is the candidate in this same milestone — that source should be re-argued on its own.
