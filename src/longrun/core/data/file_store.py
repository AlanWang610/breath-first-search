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

import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

import geopandas as gpd

from longrun.core.data.base import RasterWindow
from longrun.core.geo.segments import corridor_polygon
from longrun.core.models.geometry import BBox, Corridor, Route

if TYPE_CHECKING:  # pragma: no cover
    from geopandas import GeoDataFrame

WGS84 = "EPSG:4326"

#: Fixture formats, in precedence order, one file per layer.
#:
#: GeoPackage is what a fixture freeze writes and what a real golden route carries — it is
#: compact and it is exactly what the database returned. GeoJSON exists for the *synthetic*
#: goldens, whose entire value is that a reviewer can read every tag in a diff and check the
#: expectation by hand; a GeoPackage is a binary blob and `.gitattributes` treats it as one.
LAYER_SUFFIXES = (".gpkg", ".geojson")


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

    def _path(self, layer: str) -> Path | None:
        """The file backing a layer, or None when the fixture does not carry it."""
        for suffix in LAYER_SUFFIXES:
            candidate = self.root / f"{layer}{suffix}"
            if candidate.exists():
                return candidate
        return None

    def _load(self, layer: str) -> GeoDataFrame:
        if layer in self._cache:
            return self._cache[layer]
        path = self._path(layer)
        if path is None:
            wanted = ", ".join(f"{layer}{suffix}" for suffix in LAYER_SUFFIXES)
            raise LayerNotFound(f"no {layer!r} layer under {self.root}: looked for {wanted}")
        frame = gpd.read_file(path)
        if frame.crs is None:
            frame = frame.set_crs(WGS84)
        elif frame.crs.to_string() != WGS84:
            frame = frame.to_crs(WGS84)
        self._cache[layer] = frame
        return frame

    def has_layer(self, layer: str) -> bool:
        """Whether this fixture carries a layer at all (scope 3.6)."""
        return self._path(layer) is not None

    def ways_in_corridor(self, corridor: Corridor) -> GeoDataFrame:
        return self._clip("ways", corridor_polygon(corridor))

    def points_in_corridor(
        self, corridor: Corridor, kinds: list[str], layer: str = "amenities"
    ) -> GeoDataFrame:
        frame = self._clip(layer, corridor_polygon(corridor))
        if kinds and "kind" in frame.columns:
            frame = frame[frame["kind"].isin(kinds)]
        return frame

    def polygons_intersecting(self, corridor: Corridor, layer: str) -> GeoDataFrame:
        return self._clip(layer, corridor_polygon(corridor))

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

    def has_layer(self, layer: str) -> bool:
        """Whether the raster exists at all (scope 3.6).

        Distinct from `read_window` returning None, which today means any of "no such
        raster", "the read failed" and "no coverage at this bbox". Only the first is a
        statement about the *store*, and only this method makes it.
        """
        if (self.root / f"{layer}.tif").exists():
            return True
        return layer in self.remote

    def read_window(self, layer: str, bbox: BBox) -> tuple[Any, Any] | None:
        """An (array, affine transform) window, or None where there is no coverage."""
        window = self.read_window_meta(layer, bbox)
        return None if window is None else (window.array, window.transform)

    def read_window_meta(self, layer: str, bbox: BBox) -> RasterWindow | None:
        """The window plus the metadata a reprojection needs.

        The bbox arrives in WGS84 and the raster is in whatever CRS it was published in,
        so the bounds are transformed into the source CRS before the window is cut. That
        transform is not optional and its absence was a silent wrong answer: feeding
        degrees to a projected raster's transform produces a window a few pixels wide, and
        because the read is `boundless=True` it comes back as a full-size array of nodata
        rather than as an error. 3DEP at 1 m is projected and the Meta/WRI canopy is Web
        Mercator, so every source this store exists to read except the 10 m DEM would have
        been quietly empty.
        """
        import rasterio
        from rasterio.errors import RasterioIOError
        from rasterio.warp import transform_bounds
        from rasterio.windows import from_bounds

        try:
            with rasterio.open(self._source(layer)) as src:
                bounds = (bbox.min_lon, bbox.min_lat, bbox.max_lon, bbox.max_lat)
                if src.crs is not None and src.crs.to_string() != WGS84:
                    bounds = transform_bounds(WGS84, src.crs, *bounds)
                window = from_bounds(*bounds, transform=src.transform)
                array = src.read(1, window=window, boundless=True, fill_value=src.nodata)
                return RasterWindow(
                    array=array,
                    transform=src.window_transform(window),
                    crs=src.crs,
                    nodata=None if src.nodata is None else float(src.nodata),
                    res_m=_resolution_m(src),
                )
        except (LayerNotFound, RasterioIOError):
            return None

    def resolution_m(self, layer: str) -> float | None:
        """Ground resolution in **metres**, recorded in the manifest (1 m vs 10 m 3DEP)."""
        import rasterio

        try:
            with rasterio.open(self._source(layer)) as src:
                return _resolution_m(src)
        except Exception:
            return None


def _resolution_m(src: Any) -> float | None:
    """Pixel size in metres, whatever the raster's CRS.

    `transform.a` is degrees for a geographic raster and metres for a projected one, and
    returning the raw number would mean a 10 m DEM reporting `0.0000926`. Converted at the
    window's own latitude rather than at the equator, because the manifest pin is compared
    against a 1 m LiDAR figure and a 15% error would make the two look like different
    products.
    """
    size = abs(float(src.transform.a))
    if src.crs is None or src.crs.is_projected:
        return size
    centre_lat = (src.bounds.bottom + src.bounds.top) / 2.0
    return size * 111320.0 * math.cos(math.radians(centre_lat))
