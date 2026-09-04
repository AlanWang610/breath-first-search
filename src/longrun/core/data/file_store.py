"""File-backed stores: GeoPackage vectors and COG rasters (scope 4.4, 11).

The implementation the test suite runs on. A 30 km route buffered 400 m is a few
thousand OSM ways — tens of megabytes at most — so a corridor extract fits comfortably in
memory and a shapely 2 `STRtree` answers the four queries in `core.data.base` without a
database server anywhere near CI.

That is the whole payoff of the `LayerStore` seam: `PostGISLayerStore` is a second
implementation of an interface the golden suite has already exercised, rather than the
foundation everything is coupled to. A `network`-marked equivalence test keeps them
honest against each other.

Fixtures are produced by a dev command that runs the corridor queries once against the
live database, so the committed GeoPackage is literally the answer PostGIS gave.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import geopandas as gpd
from shapely.geometry import box

from longrun.core.geo.projections import transformer_from, transformer_to
from longrun.core.models.geometry import BBox, Corridor, Route

if TYPE_CHECKING:  # pragma: no cover
    from geopandas import GeoDataFrame

WGS84 = "EPSG:4326"


class LayerNotFound(FileNotFoundError):
    """A layer was requested that this fixture directory does not carry.

    Distinct from an empty result: "no water fountains in the corridor" and "nobody ever
    extracted the water layer" are different claims, and only the first is evidence
    (scope 3.6).
    """


class FileLayerStore:
    """Vector layers read from GeoPackages in a directory, one file per layer."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._cache: dict[str, GeoDataFrame] = {}
        self._vintages: dict[str, str] = {}

    def _load(self, layer: str) -> GeoDataFrame:
        if layer in self._cache:
            return self._cache[layer]
        path = self.root / f"{layer}.gpkg"
        if not path.exists():
            raise LayerNotFound(f"no {layer!r} layer at {path}")
        frame = gpd.read_file(path)
        if frame.crs is None:
            frame = frame.set_crs(WGS84)
        elif frame.crs.to_string() != WGS84:
            frame = frame.to_crs(WGS84)
        self._cache[layer] = frame
        return frame

    def has_layer(self, layer: str) -> bool:
        """Whether this fixture carries a layer at all (scope 3.6)."""
        return (self.root / f"{layer}.gpkg").exists()

    def _corridor_geometry(self, corridor: Corridor) -> Any:
        """The buffered corridor as a WGS84 polygon.

        Buffered in the corridor's local metric CRS and transformed back, never buffered
        in degrees: 400 m of longitude is not 400 m of latitude.
        """
        from pyproj import CRS

        from longrun.core.geo.projections import utm_epsg

        centre_lat = (corridor.bbox.min_lat + corridor.bbox.max_lat) / 2
        centre_lon = (corridor.bbox.min_lon + corridor.bbox.max_lon) / 2
        crs = CRS.from_epsg(utm_epsg(centre_lat, centre_lon))

        rect = box(
            corridor.bbox.min_lon,
            corridor.bbox.min_lat,
            corridor.bbox.max_lon,
            corridor.bbox.max_lat,
        )
        to_local, to_wgs = transformer_to(crs), transformer_from(crs)
        xs, ys = to_local.transform(*rect.exterior.coords.xy)
        local = box(min(xs), min(ys), max(xs), max(ys)).buffer(corridor.buffer_m)
        bx, by = to_wgs.transform(*local.exterior.coords.xy)
        return box(min(bx), min(by), max(bx), max(by))

    def ways_in_corridor(self, corridor: Corridor) -> GeoDataFrame:
        return self._clip("ways", self._corridor_geometry(corridor))

    def points_in_corridor(self, corridor: Corridor, kinds: list[str]) -> GeoDataFrame:
        frame = self._clip("amenities", self._corridor_geometry(corridor))
        if kinds and "kind" in frame.columns:
            frame = frame[frame["kind"].isin(kinds)]
        return frame

    def polygons_intersecting(self, corridor: Corridor, layer: str) -> GeoDataFrame:
        return self._clip(layer, self._corridor_geometry(corridor))

    def lines_crossing(self, route: Route, layer: str) -> GeoDataFrame:
        from shapely.geometry import LineString

        line = LineString([(p.lon, p.lat) for p in route.points])
        frame = self._load(layer)
        hits = frame.sindex.query(line, predicate="intersects")
        return frame.iloc[hits]

    def _clip(self, layer: str, geometry: Any) -> GeoDataFrame:
        """Spatial-index query then exact predicate, as PostGIS would."""
        frame = self._load(layer)
        if frame.empty:
            return frame
        candidates = frame.sindex.query(geometry, predicate="intersects")
        return frame.iloc[candidates]

    def vintage(self, layer: str) -> str | None:
        """Source vintage for the manifest's snapshot pins (scope 6.4)."""
        return self._vintages.get(layer)

    def set_vintage(self, layer: str, vintage: str) -> None:
        self._vintages[layer] = vintage


class FileRasterStore:
    """Rasters read from GeoTIFFs in a directory, one file per layer.

    Also serves remote COGs through GDAL's `/vsicurl/`, which is how 3DEP is read
    without staging a national DEM locally — the tiles on
    `prd-tnm.s3.amazonaws.com` support range requests.
    """

    def __init__(self, root: Path | str, remote: dict[str, str] | None = None) -> None:
        self.root = Path(root)
        self.remote = remote or {}

    def _source(self, layer: str) -> str:
        local = self.root / f"{layer}.tif"
        if local.exists():
            return str(local)
        if layer in self.remote:
            return f"/vsicurl/{self.remote[layer]}"
        raise LayerNotFound(f"no raster {layer!r} under {self.root} or in remote map")

    def read_window(self, layer: str, bbox: BBox) -> tuple[Any, Any] | None:
        """An (array, affine transform) window, or None where there is no coverage."""
        import rasterio
        from rasterio.errors import RasterioIOError
        from rasterio.windows import from_bounds

        try:
            with rasterio.open(self._source(layer)) as src:
                window = from_bounds(
                    bbox.min_lon,
                    bbox.min_lat,
                    bbox.max_lon,
                    bbox.max_lat,
                    transform=src.transform,
                )
                array = src.read(1, window=window, boundless=True, fill_value=src.nodata)
                return array, src.window_transform(window)
        except (LayerNotFound, RasterioIOError):
            return None

    def resolution_m(self, layer: str) -> float | None:
        """Ground resolution, recorded in the manifest (1 m LiDAR vs 10 m 3DEP)."""
        import rasterio

        try:
            with rasterio.open(self._source(layer)) as src:
                return abs(float(src.transform.a))
        except Exception:
            return None
