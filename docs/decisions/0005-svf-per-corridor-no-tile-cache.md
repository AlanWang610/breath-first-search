# 0005 — Sky view factor is computed per corridor, not cached as tiles

Status: accepted, 2026-09-10.

## Context

Scope §5 specifies sky view factor as a stored product:

> Derived from DSM = DEM + canopy + building heights; computed on demand per route
> corridor (300–500 m buffer), DSM downsampled to 2 m, **cached as COG tiles keyed by tile
> ID**. Region-wide 1 m SVF is 10⁹–10¹⁰ pixels for a metro region and mostly never used;
> the direct-sun ray-cast is on demand anyway and shares the corridor extraction.

The stated justification for the cache is cost. That argument was made before anyone had
measured the cost, and ADR 0003 has now measured it.

## Decision

**No COG tile cache. `core/geo/svf.py` computes horizons per corridor, keeps the answer,
and discards the tiles.**

The measurement is the whole argument. A 100 km route at 1 m, 72 azimuths, full rung takes
**0.46 s**. A cache exists to buy back time that is expensive to spend; half a second
against a ~180 s budget is not.

What the cache would cost is not nothing:

* **Invalidation.** A tile's SVF is a function of three inputs with independent vintages —
  3DEP, the Meta/WRI canopy release, and an Overture building snapshot. A cached tile is
  correct only until any one of them moves, and scope §6.4 requires the manifest to pin all
  three. So the key is not a tile id, it is a tile id plus three vintages, and a stale entry
  is a wrong shade figure that looks exactly like a right one.
* **A second storage format.** COG tiles on disk, with their own lifecycle, alongside the
  SQLite cache that already exists for external API results.
* **A false economy in the common case.** Two runs of the same corridor are rare; two runs
  of the same *route* re-derive from the same GPX anyway.

Scope §5's own reasoning supports this once the numbers are in. It rejects region-wide SVF
because it is "mostly never used" — the same objection applies with more force to caching
corridor tiles that cost half a second to rebuild.

## Consequences

* `corridor_horizons(route, ctx)` returns the horizon array, the SVF, a per-point support
  fraction and a `DsmCoverage`. The tiles it walked are freed as it goes; the answer for a
  100 km route is about half a megabyte against 22 MB per tile.
* Nothing writes rasters. `core/data/rasters.py` reads COGs; it does not produce them.
* `SnapshotPins.canopy_version` and `dem_resolution_m` still matter — they pin what the
  plan was computed *from*, which is what makes two plans comparable. That was always the
  more important half of §6.4's requirement, and it survives without a cache.

## What would make us revisit

`start_time_optimizer` (§7.7) sweeping start times over one corridor does **not** change
this — the horizon profile is already time-independent and is computed once per corridor,
which is exactly the reuse a cache would have provided, without the storage.

The case that would change it is a *shared* corridor: many users planning routes through
the same downtown, where one tile's horizons would be recomputed thousands of times. That
is a server-side concern at a scale this project does not have, and it would want a
different design anyway — precomputed per-tile horizons, not per-corridor SVF.
