"""The surface the sun is blocked by: DEM + canopy + buildings (scope 5, 7.4).

`raycast.py` needs a metric grid of absolute heights, north-up, square cells. This builds
one — but never for a whole route at once. ADR 0003 measured the constraint and it is not
compute: a 100 km corridor bounding box at 1 m is 10 GB, the ribbon along the route is
720 MB, and **tiles are tens of megabytes whatever the route length**. So the unit here is
a tile, `iter_dsm_tiles` is a generator, and materialising it into a list puts the ribbon
back in memory and undoes the decision.

**Tiles are budgeted by area, not by route distance.** A 2 km chunk running along a grid
axis has a small bounding box; the same 2 km on a diagonal spans 1414 m each way and is
roughly twice the cells. Splitting purely on distance makes peak memory a property of
which way the route happens to point. A chunk whose box exceeds `max_cells` is bisected
until it fits, so flat residency is enforced rather than hoped for.

**North-up is a term of the contract, not a default.** Rotating each tile to the route
heading would keep every box tight, and `horizon_profile` measures azimuths from *grid*
north — so a rotated grid silently rotates every solar azimuth and returns a plausible
wrong answer. Do not.

**The height arithmetic, stated once so it cannot drift:**

    surface = ground + max(canopy, building)

Not `ground + canopy + building`. A tree beside a building is two competing claims about
the same column of air and the taller one wins; adding them puts a 20 m tree on a 10 m
roof. Ground is an absolute elevation and the other two are heights *above* it, so this
module only ever adds a height-above-ground to a single ground field — never two absolute
elevations, which is what will matter the day a lidar first-return DSM arrives with a
20-35 m geoid separation.

**Unknown ground reads as open sky, deliberately.** A DEM nodata value of -9999 fed into
`horizon_profile` would fail the obstruction test and read as open sky anyway; worse, a
sample point *sitting* on nodata makes every neighbour rise 10 km and reads as a canyon.
So invalid cells become NaN, `np.isfinite` discards them, and the point reports no
horizon. That is the conservative direction: claim *less* shade than there may be, so a
`sun.hot` runner is never told a hot arterial is shaded. Filling with a tile median would
invent plausible obstructions instead, which is worse — and `DsmCoverage` is what stops
"claimed less" from becoming "claimed silently".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
from pydantic import BaseModel, Field

from longrun.core.geo.projections import local_crs, transformer_from, transformer_to
from longrun.core.geo.raycast import LADDER
from longrun.core.models.coverage import CoverageEntry
from longrun.core.models.geometry import BBox

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator, Mapping, Sequence

    from numpy.typing import NDArray

    from longrun.core.geo.raycast import RaycastSettings
    from longrun.core.models.context import ScorerContext
    from longrun.core.models.geometry import Route

#: Working resolution. ADR 0003: 1 m is affordable, and scope 7.4 asks for it "where
#: LiDAR exists". Fixed rather than derived from the source raster, because deriving it
#: would make the grid depend on which DEM tile happened to answer.
DEFAULT_CELL_M = 1.0

#: Route covered by one tile before margins.
DEFAULT_TILE_M = 2000.0

#: Extra ground on every side, so a ray cast from a point this tile owns cannot leave the
#: raster. Equal to the ray-cast search radius by construction, not by coincidence.
DEFAULT_MARGIN_M = LADDER[0].max_radius_m

#: Scope 5's corridor half-width.
DEFAULT_CORRIDOR_M = 400.0

#: Cell ceiling per tile, ~64 MB of float32. A tile over this is bisected.
MAX_TILE_CELLS = 16_000_000

#: Layer names, in the store, for the three sources.
DEM_LAYER = "dem"
CANOPY_LAYER = "canopy"
BUILDINGS_LAYER = "buildings"

#: Columns a buildings frame may carry its height in, most explicit first.
HEIGHT_COLUMNS = ("height", "height_m", "building_height")

#: Columns carrying a storey count, used only when no height column answered.
FLOOR_COLUMNS = ("num_floors", "levels", "building:levels", "building_levels")

#: Storey height when a building gives floors but not metres. A stated assumption, and the
#: reason `building_height_m` reports which route it took.
FLOOR_HEIGHT_M = 3.2

#: Confidence in a shade measurement, by which layers answered. Terrain-only in a city is
#: the dangerous case: it reports a near-open horizon down every street and would claim
#: full sun with full confidence if this table did not say otherwise.
DSM_CONFIDENCE: dict[frozenset[str], float] = {
    frozenset({DEM_LAYER}): 0.5,
    frozenset({DEM_LAYER, CANOPY_LAYER}): 0.8,
    frozenset({DEM_LAYER, BUILDINGS_LAYER}): 0.7,
    frozenset({DEM_LAYER, CANOPY_LAYER, BUILDINGS_LAYER}): 1.0,
}


class LayerContribution(BaseModel):
    """What one source contributed to one tile, or why it did not."""

    model_config = {"frozen": True}

    layer: str
    available: bool
    valid_fraction: float = Field(default=0.0, ge=0.0, le=1.0)
    native_res_m: float | None = None
    reason: str | None = None
    rows_without_height: int = 0


@dataclass(frozen=True)
class TileSpec:
    """Where one tile sits, in the route's local UTM. Pure geometry, no data."""

    index: int
    route_start_m: float
    route_end_m: float
    point_indices: tuple[int, ...]
    bounds: tuple[float, float, float, float]
    epsg: int
    cell_m: float
    margin_m: float

    @property
    def shape(self) -> tuple[int, int]:
        min_x, min_y, max_x, max_y = self.bounds
        return (
            max(1, int(round((max_y - min_y) / self.cell_m))),
            max(1, int(round((max_x - min_x) / self.cell_m))),
        )

    @property
    def cells(self) -> int:
        rows, cols = self.shape
        return rows * cols

    @property
    def transform(self) -> Any:
        """North-up affine from grid (col, row) to UTM. See the module docstring."""
        from rasterio.transform import from_origin

        min_x, _, _, max_y = self.bounds
        return from_origin(min_x, max_y, self.cell_m, self.cell_m)

    def wgs84_bbox(self) -> BBox:
        """The tile in WGS84, for asking a store that indexes in degrees."""
        to_wgs = transformer_from(_crs_from_epsg(self.epsg))
        min_x, min_y, max_x, max_y = self.bounds
        xs = [min_x, min_x, max_x, max_x]
        ys = [min_y, max_y, min_y, max_y]
        lons, lats = to_wgs.transform(xs, ys)
        return BBox(min_lon=min(lons), min_lat=min(lats), max_lon=max(lons), max_lat=max(lats))


@dataclass(frozen=True)
class DsmTile:
    """One tile's surface, and an account of what went into it.

    Deliberately not a pydantic model: tens of megabytes of float32 has no business in
    `plan.json`. What reaches a plan is `DsmCoverage`, which is.
    """

    surface: NDArray[np.float32]
    valid: NDArray[np.bool_]
    spec: TileSpec
    contributions: tuple[LayerContribution, ...]

    @property
    def cell_m(self) -> float:
        return self.spec.cell_m


def _crs_from_epsg(epsg: int) -> Any:
    from pyproj import CRS

    return CRS.from_epsg(epsg)


def tile_specs(
    route: Route,
    *,
    cell_m: float = DEFAULT_CELL_M,
    tile_m: float = DEFAULT_TILE_M,
    margin_m: float = DEFAULT_MARGIN_M,
    corridor_m: float = DEFAULT_CORRIDOR_M,
    max_cells: int = MAX_TILE_CELLS,
    settings: RaycastSettings | None = None,
) -> list[TileSpec]:
    """Lay tiles along the route. Pure geometry: no store, no I/O, no rasters.

    Every route point belongs to exactly one tile, so no point is scored twice and none is
    dropped. Bounds are snapped outward to a whole number of cells from a fixed origin, so
    adjacent tiles are co-registered — two tiles that disagree about where a cell boundary
    falls cannot have their horizons compared.
    """
    config = settings or LADDER[0]
    if margin_m < config.max_radius_m:
        raise ValueError(
            f"margin {margin_m} m is narrower than the {config.max_radius_m} m search "
            "radius: rays would leave the tile and read as open sky"
        )
    if not route.points:
        return []

    crs = local_crs(route)
    epsg = crs.to_epsg()
    if epsg is None:  # pragma: no cover - local_crs always returns a UTM zone
        raise ValueError("route CRS has no EPSG code; cannot tile")
    to_local = transformer_to(crs)
    xs, ys = to_local.transform([p.lon for p in route.points], [p.lat for p in route.points])
    coords = list(zip(xs, ys, strict=True))

    groups = _split_by_distance(route, tile_m)
    pad = corridor_m + margin_m

    specs: list[TileSpec] = []
    queue = list(groups)
    while queue:
        indices = queue.pop(0)
        bounds = _snapped_bounds(coords, indices, pad, cell_m)
        spec = TileSpec(
            index=0,
            route_start_m=route.points[indices[0]].cum_dist_m,
            route_end_m=route.points[indices[-1]].cum_dist_m,
            point_indices=tuple(indices),
            bounds=bounds,
            epsg=epsg,
            cell_m=cell_m,
            margin_m=margin_m,
        )
        # Bisect an over-large box rather than accept it: a diagonal chunk covers roughly
        # twice the area of an axis-aligned one, and a hairpin more, so budgeting by route
        # distance alone makes peak memory depend on which way the route points.
        if spec.cells > max_cells and len(indices) > 2:
            middle = len(indices) // 2
            queue.insert(0, indices[middle:])
            queue.insert(0, indices[:middle])
            continue
        specs.append(spec)

    return [
        TileSpec(
            index=i,
            route_start_m=s.route_start_m,
            route_end_m=s.route_end_m,
            point_indices=s.point_indices,
            bounds=s.bounds,
            epsg=s.epsg,
            cell_m=s.cell_m,
            margin_m=s.margin_m,
        )
        for i, s in enumerate(specs)
    ]


def _split_by_distance(route: Route, tile_m: float) -> list[list[int]]:
    """Contiguous point-index runs of at most `tile_m` of route, partitioning the route."""
    groups: list[list[int]] = []
    current: list[int] = [0]
    start_m = route.points[0].cum_dist_m
    for i in range(1, len(route.points)):
        # Closed *before* `i` is added, so `i` opens the next run: the groups partition the
        # point indices rather than overlapping at the boundary. Overlap would score a
        # point twice and double-count it in every route summary.
        if route.points[i].cum_dist_m - start_m >= tile_m and i < len(route.points) - 1:
            groups.append(current)
            current = []
            start_m = route.points[i].cum_dist_m
        current.append(i)
    if current:
        groups.append(current)
    return [g for g in groups if g]


def _snapped_bounds(
    coords: Sequence[tuple[float, float]],
    indices: Sequence[int],
    pad_m: float,
    cell_m: float,
) -> tuple[float, float, float, float]:
    chunk = [coords[i] for i in indices]
    min_x = min(x for x, _ in chunk) - pad_m
    max_x = max(x for x, _ in chunk) + pad_m
    min_y = min(y for _, y in chunk) - pad_m
    max_y = max(y for _, y in chunk) + pad_m
    return (
        math.floor(min_x / cell_m) * cell_m,
        math.floor(min_y / cell_m) * cell_m,
        math.ceil(max_x / cell_m) * cell_m,
        math.ceil(max_y / cell_m) * cell_m,
    )


def tile_rows_cols(
    spec: TileSpec, route: Route, indices: Sequence[int] | None = None
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Fractional grid coordinates of route points, ready for `horizon_profile`."""
    wanted = list(spec.point_indices if indices is None else indices)
    to_local = transformer_to(_crs_from_epsg(spec.epsg))
    xs, ys = to_local.transform(
        [route.points[i].lon for i in wanted], [route.points[i].lat for i in wanted]
    )
    inverse = ~spec.transform
    cols, rows = inverse @ (np.asarray(xs, dtype=float), np.asarray(ys, dtype=float))
    return np.asarray(rows, dtype=np.float64), np.asarray(cols, dtype=np.float64)


def building_height_m(tags: Mapping[str, Any]) -> tuple[float | None, str]:
    """Height above ground for one building, and how it was arrived at.

    `(None, "absent")` when the record does not say. A footprint with no height is not a
    zero-height building, and defaulting one would invent shade — Overture records
    frequently lack the attribute, so this is the common case rather than the edge one.
    """
    for column in HEIGHT_COLUMNS:
        value = _finite(tags.get(column))
        if value is not None and value > 0:
            return value, "height"
    for column in FLOOR_COLUMNS:
        value = _finite(tags.get(column))
        if value is not None and value > 0:
            return value * FLOOR_HEIGHT_M, "floors"
    return None, "absent"


def _finite(value: Any) -> float | None:
    """Float, or None for anything that is not a real number.

    NaN needs naming: geopandas hands back `float("nan")` for a missing cell and
    `bool(float("nan"))` is **True**, so a plain truthiness test rasterizes a NaN.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


# --- assembly ---------------------------------------------------------------


def rasterize_buildings(frame: Any, spec: TileSpec) -> tuple[NDArray[np.float32], int]:
    """Building heights above ground on the tile grid, and how many rows had no height.

    **One `rasterize` call, not one per footprint.** A per-building mask is
    `O(buildings x tile cells)`, which on a downtown corridor is 8,000 footprints against
    5 million cells and turns a two-second plan into a twenty-minute one. Sorting by height
    ascending and letting later shapes overwrite earlier ones gives the same `max` semantics
    in a single pass: where two footprints overlap, the taller was drawn last.

    Taller wins for the same reason `surface` takes a max — two claims about one column of
    air, and the sun is blocked by the higher one.
    """
    from pyproj import CRS
    from pyproj import Transformer as PyprojTransformer
    from rasterio.features import rasterize
    from shapely.ops import transform as shapely_transform

    heights = np.zeros(spec.shape, dtype=np.float32)
    if frame is None or len(frame) == 0:
        return heights, 0

    to_local = PyprojTransformer.from_crs(
        CRS.from_epsg(4326), CRS.from_epsg(spec.epsg), always_xy=True
    ).transform

    shapes: list[tuple[Any, float]] = []
    missing = 0
    for _, row in frame.iterrows():
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        height, _how = building_height_m({k: v for k, v in row.items() if k != "geometry"})
        if height is None:
            missing += 1
            continue
        shapes.append((shapely_transform(to_local, geometry), float(height)))

    if shapes:
        shapes.sort(key=lambda pair: pair[1])
        rasterize(
            shapes,
            out=heights,
            transform=spec.transform,
            all_touched=False,
            default_value=0.0,
        )
    return heights, missing


def _warp_into(window: Any, spec: TileSpec, resampling: Any) -> NDArray[np.float32]:
    """Warp one source window onto the tile grid; NaN wherever it said nothing."""
    from pyproj import CRS
    from rasterio.warp import reproject

    dest = np.full(spec.shape, np.nan, dtype=np.float32)
    reproject(
        source=np.asarray(window.array, dtype=np.float32),
        destination=dest,
        src_transform=window.transform,
        src_crs=window.crs,
        src_nodata=window.nodata,
        dst_transform=spec.transform,
        dst_crs=CRS.from_epsg(spec.epsg),
        dst_nodata=float("nan"),
        resampling=resampling,
    )
    return dest


def _raster_contribution(
    ctx: ScorerContext, layer: str, bbox: BBox, spec: TileSpec, resampling: Any
) -> tuple[NDArray[np.float32] | None, LayerContribution]:
    """Read and warp one raster layer, or say precisely why there is nothing."""
    window = ctx.rasters.read_window_meta(layer, bbox)
    if window is None:
        # Three different claims that `read_window` alone would collapse into one None.
        reason = (
            f"no {layer} raster in this store"
            if not ctx.rasters.has_layer(layer)
            else f"{layer} has no coverage over this tile"
        )
        return None, LayerContribution(layer=layer, available=False, reason=reason)
    warped = _warp_into(window, spec, resampling)
    return warped, LayerContribution(
        layer=layer,
        available=True,
        valid_fraction=float(np.isfinite(warped).mean()),
        native_res_m=window.res_m,
    )


def build_tile(
    spec: TileSpec,
    route: Route,
    ctx: ScorerContext,
    layers: tuple[str, ...] = (DEM_LAYER, CANOPY_LAYER, BUILDINGS_LAYER),
) -> DsmTile:
    """Read the sources for one tile and combine them into a surface."""
    from rasterio.enums import Resampling

    from longrun.core.data.file_store import LayerNotFound
    from longrun.core.models.geometry import Corridor

    bbox = spec.wgs84_bbox()
    contributions: list[LayerContribution] = []

    ground = np.full(spec.shape, np.nan, dtype=np.float32)
    if DEM_LAYER in layers:
        # Bilinear for terrain: it is smooth, and 10 m to 1 m is pure upsampling.
        warped, contribution = _raster_contribution(ctx, DEM_LAYER, bbox, spec, Resampling.bilinear)
        contributions.append(contribution)
        if warped is not None:
            ground = warped
    valid = np.isfinite(ground)

    above = np.zeros(spec.shape, dtype=np.float32)

    if CANOPY_LAYER in layers:
        # `max`, never `average`: a horizon is the tallest thing on the ray, and averaging
        # a canopy gap into a tree systematically under-reports shade.
        warped, contribution = _raster_contribution(ctx, CANOPY_LAYER, bbox, spec, Resampling.max)
        contributions.append(contribution)
        if warped is not None:
            above = np.fmax(above, np.nan_to_num(warped, nan=0.0))

    if BUILDINGS_LAYER in layers:
        try:
            frame = ctx.layers.polygons_intersecting(
                Corridor(route_id=route.id, buffer_m=0.0, bbox=bbox), BUILDINGS_LAYER
            )
        except (LayerNotFound, FileNotFoundError):
            # Deliberately without the exception text: a LayerNotFound names an absolute
            # path, and a coverage reason is both a user-facing sentence and a value a
            # golden expectation pins. Neither can carry a machine-specific path.
            contributions.append(
                LayerContribution(
                    layer=BUILDINGS_LAYER,
                    available=False,
                    reason="no buildings layer in this store",
                )
            )
        else:
            built, missing = rasterize_buildings(frame, spec)
            above = np.fmax(above, built)
            contributions.append(
                LayerContribution(
                    layer=BUILDINGS_LAYER,
                    available=True,
                    valid_fraction=1.0,
                    rows_without_height=missing,
                )
            )

    # ground + max(canopy, building). See the module docstring: never a sum.
    surface = ground + above
    surface[~valid] = np.nan

    return DsmTile(surface=surface, valid=valid, spec=spec, contributions=tuple(contributions))


def iter_dsm_tiles(
    route: Route,
    ctx: ScorerContext,
    *,
    cell_m: float = DEFAULT_CELL_M,
    tile_m: float = DEFAULT_TILE_M,
    margin_m: float = DEFAULT_MARGIN_M,
    corridor_m: float = DEFAULT_CORRIDOR_M,
    max_cells: int = MAX_TILE_CELLS,
    layers: tuple[str, ...] = (DEM_LAYER, CANOPY_LAYER, BUILDINGS_LAYER),
    settings: RaycastSettings | None = None,
) -> Iterator[DsmTile]:
    """Tiles along the route, one at a time.

    **A generator on purpose.** Materialising it into a list puts the whole ribbon back in
    memory — 720 MB for a 100 km route at 1 m — and undoes the one thing ADR 0003 asked
    for. Consume it lazily.
    """
    for spec in tile_specs(
        route,
        cell_m=cell_m,
        tile_m=tile_m,
        margin_m=margin_m,
        corridor_m=corridor_m,
        max_cells=max_cells,
        settings=settings,
    ):
        yield build_tile(spec, route, ctx, layers=layers)


class DsmCoverage(BaseModel):
    """What the surface was built from, in a form a plan sheet can print (scope 3.6)."""

    cell_m: float
    tiles: int = 0
    layers: dict[str, LayerContribution] = Field(default_factory=dict)

    @property
    def answered(self) -> frozenset[str]:
        return frozenset(name for name, c in self.layers.items() if c.available)

    @property
    def confidence(self) -> float:
        """Lower when a source is missing; terrain-only is the dangerous case.

        A terrain-only DSM reports a near-open horizon down every street and would claim
        full sun at full confidence. This is what stops that.
        """
        return DSM_CONFIDENCE.get(self.answered, 0.3 if self.answered else 0.0)

    def describe(self) -> str:
        """One line for the sheet: what was measured, and what was not."""
        have = sorted(self.answered)
        missing = sorted(set(self.layers) - self.answered)
        if not have:
            return "no surface model available; shade not measured"
        text = "shade measured from " + _join(have)
        if missing:
            text += f"; {_join(missing)} unavailable"
        return text

    def entries(self) -> list[CoverageEntry]:
        """One coverage entry per source, named so `attribution.py` can licence it."""
        sources = {DEM_LAYER: "3dep", CANOPY_LAYER: "canopy", BUILDINGS_LAYER: "overture"}
        out: list[CoverageEntry] = []
        for layer, contribution in sorted(self.layers.items()):
            reason = contribution.reason
            if contribution.available and contribution.rows_without_height:
                # Counted per tile, and tiles overlap by their ray margins, so a footprint
                # near a boundary is seen twice. Said plainly rather than presented as a
                # count of distinct buildings, which it is not.
                reason = (
                    f"{contribution.rows_without_height} footprint read(s) across tiles "
                    "carried no height and were not raised"
                )
            out.append(
                CoverageEntry(
                    source=sources.get(layer, layer),
                    kind="surface_model",
                    checked=contribution.available,
                    reason=reason,
                    confidence=contribution.valid_fraction if contribution.available else None,
                )
            )
        return out


def _join(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def accumulate_coverage(
    tiles_seen: int, contributions: list[LayerContribution], cell_m: float
) -> DsmCoverage:
    """Fold per-tile contributions into one route-level statement.

    A layer counts as available when it answered for **any** tile, with the valid fraction
    averaged over tiles: a canopy raster covering the first 3 km and then stopping is a
    different claim from one covering none of the route, and both differ from full
    coverage. Collapsing those to a boolean would lose the only distinction that matters
    to a reader deciding whether to trust the shade figure.
    """
    # Accumulated as plain floats, not into `LayerContribution.valid_fraction`, which the
    # model constrains to [0, 1]: summing two full tiles into it fails validation before the
    # divide ever happens. Found by the scorer error boundary turning it into an honest
    # `unavailable`, which is what that boundary is for.
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    available: dict[str, bool] = {}
    native: dict[str, float | None] = {}
    reasons: dict[str, str | None] = {}
    missing_heights: dict[str, int] = {}

    for contribution in contributions:
        layer = contribution.layer
        counts[layer] = counts.get(layer, 0) + 1
        totals[layer] = totals.get(layer, 0.0) + contribution.valid_fraction
        available[layer] = available.get(layer, False) or contribution.available
        native[layer] = native.get(layer) or contribution.native_res_m
        reasons[layer] = reasons.get(layer) or contribution.reason
        missing_heights[layer] = missing_heights.get(layer, 0) + contribution.rows_without_height

    merged = {
        layer: LayerContribution(
            layer=layer,
            available=available[layer],
            valid_fraction=min(1.0, totals[layer] / max(1, counts[layer])),
            native_res_m=native[layer],
            reason=reasons[layer],
            rows_without_height=missing_heights[layer],
        )
        for layer in counts
    }
    return DsmCoverage(cell_m=cell_m, tiles=tiles_seen, layers=merged)


__all__ = [
    "BUILDINGS_LAYER",
    "CANOPY_LAYER",
    "DEFAULT_CELL_M",
    "DEFAULT_CORRIDOR_M",
    "DEFAULT_MARGIN_M",
    "DEFAULT_TILE_M",
    "DEM_LAYER",
    "DSM_CONFIDENCE",
    "FLOOR_HEIGHT_M",
    "MAX_TILE_CELLS",
    "DsmCoverage",
    "DsmTile",
    "LayerContribution",
    "TileSpec",
    "accumulate_coverage",
    "build_tile",
    "building_height_m",
    "iter_dsm_tiles",
    "rasterize_buildings",
    "tile_rows_cols",
    "tile_specs",
]
