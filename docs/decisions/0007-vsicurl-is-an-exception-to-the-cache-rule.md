# 0007 — `/vsicurl/` raster reads are a named exception to the cache rule

Status: accepted, 2026-09-10.

## Context

The build plan's cross-cutting rule 2 says:

> **No implicit network.** All outbound HTTP goes through `core/data/cache.py`, keyed
> `(tool, args_hash, date)` per §4.4. A `LONGRUN_OFFLINE=1` mode makes a cache miss an
> error rather than a fetch — this is what makes golden tests hermetic.

That rule is **already false**, and has been since M1.4. `FileRasterStore` reads COGs
through GDAL's `/vsicurl/` driver, which issues its own HTTP range requests from inside
the C library. Those reads do not pass through `cache.fetch`, do not spend `Budget`, are
not recorded in any cassette, and `LONGRUN_OFFLINE` cannot see them.

M2 made this matter rather than academic: 3DEP is how a real plan gets terrain, and
`--remote-rasters` is how it asks for it.

## Decision

**Accept the exception, name it, and close the two holes it leaves.**

Rasters are genuinely a different kind of resource from an API call. A COG at a URL is
content-addressed and immutable — the bytes at `USGS_13_n38w123.tif` do not change with the
date, which is the whole premise of the `(tool, args_hash, **date**)` key. GDAL maintains
its own block cache. And a cassette holding a windowed read of a 220 MB GeoTIFF would be a
cassette nobody commits.

So rule 2 is amended to read *"all outbound HTTP **except content-addressed raster reads**
goes through the cache"*, and the exception carries two obligations:

**Offline refuses a remote read outright.** `FileRasterStore(root, remote=…, offline=True)`
raises `LayerNotFound("remote raster 'dem' refused in offline mode")` rather than fetching.
A local raster of the same name still wins, so a golden route that carries its own DEM is
unaffected — which is the case that matters, since that is every golden route.

**A remote read that does happen is metered.** `Budget.spend_raster_window()`, capped at
500 per plan, separate from `api_calls_max`. A DSM over a 100 km corridor is 50 tiles of
windowed reads and nothing else in the budget would have noticed them.

## Consequences

* `conftest._block_network` is no longer the only thing standing between a test and a
  multi-gigabyte range-request session. That fixture only exists inside pytest, so anything
  relying on it was relying on a test harness for a production property.
* `--remote-rasters` stays opt-in. Belt and braces: the flag is what stops it happening by
  accident, and offline mode is what stops it happening in a golden.
* The manifest can report `raster_windows_used`, so "did this plan touch the network for
  terrain" becomes answerable from `plan.json`.
* The two obligations are tested directly, including that a local raster still wins when
  offline — the failure mode that would break every golden at once.

## What would make us revisit

If a raster source ever becomes date-dependent — a daily fire-perimeter raster, a snow
surface — it stops being content-addressed and the argument collapses. Such a source
belongs behind `cache.fetch` like any other dated API, fetched as bytes rather than read
through GDAL.
