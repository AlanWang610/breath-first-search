"""Where the national rasters live, and how to name the tile you need (scope 5, 13).

`FileRasterStore` already reads a COG over `/vsicurl/` given a URL; this is the part that
works out *which* URL. Two sources, and they are named very differently:

* **3DEP** is deterministic. Tiles are one degree square, named for their north-west
  corner — `n38w123` covers latitude 37-38 and longitude -123 to -122 — so the URL is
  arithmetic and needs no index and no network to compute.
* **Meta/WRI canopy** is not. Tiles are quadkeys and the mapping from a coordinate to a
  tile id lives in a 15 MB `tiles.geojson`, so resolving one means fetching an index —
  which goes through the cache like every other external read, and is keyed by nothing but
  the release, because a tile index has no date.

**Nothing here is used by default.** `repair` builds a `FileRasterStore` over a fixture
directory and reaches the network only when asked with `--remote-rasters`. That is not
timidity: `conftest._block_network` fails any non-`network` test that opens an off-host
socket, so a store that silently reached S3 would make every golden route unrunnable in CI
— and GDAL's `/vsicurl/` reads bypass `cache.fetch`, the budget and `LONGRUN_OFFLINE`
entirely, which is a hole worth keeping deliberate rather than accidental.

A corridor that spans more than one tile gets the tile under its centroid, and the parts
outside it read as nodata — which `DsmCoverage.valid_fraction` already reports. Mosaicking
is a region-build concern (scope 13 step 3), not something a single plan should do.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext
    from longrun.core.models.geometry import Route

#: 1/3 arc-second (~10 m) 3DEP, the resolution with national coverage. 1 m LiDAR exists for
#: some counties under a different prefix and a different tiling; scope 7.4 wants it "where
#: LiDAR exists", which is a region-build lookup rather than a URL template.
THREE_DEP_ROOT = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current"

CANOPY_ROOT = "https://dataforgood-fb-data.s3.amazonaws.com/forests/v1/alsgedi_global_v6_float"

#: The tile index, fetched once and cached. It has no date — a release is a release — so it
#: is keyed under a fixed day like the NWS `/points` lookup.
CANOPY_INDEX_URL = f"{CANOPY_ROOT}/tiles.geojson"
CANOPY_INDEX_DAY = "static"

HTTP_TIMEOUT_S = 60.0


def three_dep_tile(lat: float, lon: float) -> str:
    """The 3DEP tile name covering a coordinate, e.g. `n38w123`.

    Named for the north-west corner, so a point at 37.8 N is in the `n38` row and a point
    at 122.4 W is in the `w123` column. Off by one either way and the read succeeds against
    the wrong hill.
    """
    north = math.ceil(lat)
    west = math.ceil(abs(lon)) if lon < 0 else math.floor(lon)
    hemisphere = "n" if lat >= 0 else "s"
    meridian = "w" if lon < 0 else "e"
    return f"{hemisphere}{abs(north):02d}{meridian}{abs(west):03d}"


def three_dep_url(lat: float, lon: float) -> str:
    tile = three_dep_tile(lat, lon)
    return f"{THREE_DEP_ROOT}/{tile}/USGS_13_{tile}.tif"


def canopy_tile_url(tile_id: str) -> str:
    return f"{CANOPY_ROOT}/chm/{tile_id}.tif"


def canopy_tile_for(lat: float, lon: float, ctx: ScorerContext) -> str | None:
    """The canopy tile covering a coordinate, or None when the index does not have one.

    The index is fetched through `cache.fetch`, so an offline run replays it and a run
    without a recording says so rather than reaching out. Only the tile ids and their
    bounding boxes are kept — the raw file is 15 MB of geometry and nothing here needs the
    polygons.
    """
    from longrun.core.data.cache import fetch

    def producer() -> Any:
        import httpx

        ctx.budget.spend_api_call()
        response = httpx.get(CANOPY_INDEX_URL, timeout=HTTP_TIMEOUT_S, follow_redirects=True)
        response.raise_for_status()
        return _index_bounds(response.json())

    index = fetch(ctx.cache, "canopy.tile_index", {"release": "v6"}, CANOPY_INDEX_DAY, producer)
    for entry in index.get("tiles", []):
        min_lon, min_lat, max_lon, max_lat = entry["bbox"]
        if min_lon <= lon <= max_lon and min_lat <= lat <= max_lat:
            return str(entry["id"])
    return None


def _index_bounds(payload: dict[str, Any]) -> dict[str, Any]:
    """Reduce the tile index to ids and bounding boxes, which is all a lookup needs."""
    tiles = []
    for feature in payload.get("features", []):
        properties = feature.get("properties") or {}
        tile_id = properties.get("tile") or properties.get("id") or properties.get("quadkey")
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates") or []
        flat = _flatten(coords)
        if tile_id is None or not flat:
            continue
        lons = [x for x, _ in flat]
        lats = [y for _, y in flat]
        tiles.append({"id": str(tile_id), "bbox": [min(lons), min(lats), max(lons), max(lats)]})
    return {"tiles": tiles}


def _flatten(coords: Any) -> list[tuple[float, float]]:
    if (
        isinstance(coords, list)
        and len(coords) == 2
        and all(isinstance(v, int | float) for v in coords)
    ):
        return [(float(coords[0]), float(coords[1]))]
    if isinstance(coords, list):
        return [point for item in coords for point in _flatten(item)]
    return []


def remote_rasters(route: Route, ctx: ScorerContext | None = None) -> dict[str, str]:
    """URLs for the national rasters covering a route's centroid.

    Returned as the `remote` map `FileRasterStore` takes, so a local fixture of the same
    name still wins — which is what lets a golden route stay hermetic while a real run
    reaches for the same layer name.
    """
    from longrun.core.geo.projections import centroid

    middle = centroid(route)
    urls = {"dem": three_dep_url(middle.lat, middle.lon)}
    if ctx is not None:
        tile = canopy_tile_for(middle.lat, middle.lon, ctx)
        if tile is not None:
            urls["canopy"] = canopy_tile_url(tile)
    return urls


__all__ = [
    "CANOPY_INDEX_URL",
    "CANOPY_ROOT",
    "THREE_DEP_ROOT",
    "canopy_tile_for",
    "canopy_tile_url",
    "remote_rasters",
    "three_dep_tile",
    "three_dep_url",
]
