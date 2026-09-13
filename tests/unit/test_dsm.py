"""The surface model, against geometry whose answer is arithmetic.

A DSM is easy to build wrongly and hard to notice: add canopy to building height instead
of taking the max and shade merely gets *more*, which looks like better data. Treat a
nodata cell as ground and a hillside grows a cliff. So every case here is one a reader can
check — a 20 m building 20 m north subtends 45 degrees, and that is what is asserted.

The end-to-end case is the important one. It runs the real path a scorer will take —
`tile_specs` to `build_tile` to `horizon_profile` — because each piece can be right on its
own while the composition puts the shadow on the wrong side of the street.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import Polygon

from longrun.core.data.cache import SqliteCache
from longrun.core.data.file_store import FileLayerStore, FileRasterStore
from longrun.core.geo.dsm import (
    BUILDINGS_LAYER,
    CANOPY_LAYER,
    DEM_LAYER,
    DsmCoverage,
    LayerContribution,
    accumulate_coverage,
    build_tile,
    building_height_m,
    iter_dsm_tiles,
    tile_rows_cols,
    tile_specs,
)
from longrun.core.geo.raycast import LADDER, horizon_profile
from longrun.core.models.context import Budget, FrozenClock, ScorerContext
from longrun.core.models.coverage import CoverageManifest
from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.models.profile import PreferenceProfile

LAT = 37.7955
LON = -122.4000

#: 0.00045 deg of longitude at this latitude is ~39.5 m.
STEP_DEG = 0.00045


def _route(points: int = 12, diagonal: bool = False) -> Route:
    lat_step = STEP_DEG if diagonal else 0.0
    return Route(
        id="dsm",
        points=[
            RoutePoint(
                lat=LAT + i * lat_step,
                lon=LON + i * STEP_DEG,
                cum_dist_m=i * (56.0 if diagonal else 39.5),
            )
            for i in range(points)
        ],
    )


def _write_dem(root: Path, value: float = 0.0, nodata_stripe: bool = False) -> None:
    """A flat DEM in WGS84 covering the test area, optionally with a nodata stripe."""
    import rasterio
    from rasterio.transform import from_origin

    res = 0.00005  # ~4.4 m
    west, north = LON - 0.02, LAT + 0.02
    size = int(0.04 / res)
    band = np.full((size, size), value, dtype="float32")
    if nodata_stripe:
        band[:, : size // 2] = -9999.0
    with rasterio.open(
        root / "dem.tif",
        "w",
        driver="GTiff",
        height=size,
        width=size,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(west, north, res, res),
        nodata=-9999.0,
    ) as dst:
        dst.write(band, 1)


def _write_buildings(root: Path, records: list[dict[str, Any]]) -> None:
    frame = gpd.GeoDataFrame(
        [{k: v for k, v in r.items() if k != "geometry"} for r in records],
        geometry=[r["geometry"] for r in records],
        crs="EPSG:4326",
    )
    frame.to_file(root / "buildings.geojson", driver="GeoJSON")


def _square(lat: float, lon: float, half_deg: float) -> Polygon:
    return Polygon(
        [
            (lon - half_deg, lat - half_deg),
            (lon + half_deg, lat - half_deg),
            (lon + half_deg, lat + half_deg),
            (lon - half_deg, lat + half_deg),
        ]
    )


@pytest.fixture
def ctx(tmp_path: Path) -> Any:
    with SqliteCache() as cache:
        yield ScorerContext(
            layers=FileLayerStore(tmp_path),
            rasters=FileRasterStore(tmp_path),
            cache=cache,
            clock=FrozenClock(__import__("datetime").datetime(2026, 9, 12, 12, 0)),
            coverage=CoverageManifest(),
            profile=PreferenceProfile(),
            budget=Budget(),
        )


# --- the tiler --------------------------------------------------------------


def test_every_route_point_belongs_to_exactly_one_tile() -> None:
    """A partition, not a cover: a point scored twice is double-counted in the summary."""
    route = _route(points=200)
    specs = tile_specs(route, tile_m=300.0)

    owned = [i for spec in specs for i in spec.point_indices]
    assert sorted(owned) == list(range(len(route.points)))
    assert len(specs) > 1, "the route should have needed more than one tile"


def test_a_margin_narrower_than_the_search_radius_is_refused() -> None:
    """A ray leaving its tile reads as open sky, which scope 3.6 forbids confusing."""
    with pytest.raises(ValueError, match="search radius"):
        tile_specs(_route(), margin_m=LADDER[0].max_radius_m - 1.0)


def test_a_diagonal_chunk_covers_more_ground_than_an_axis_aligned_one() -> None:
    """The reason tiles are budgeted by area. See the correction appended to ADR 0003."""
    straight = tile_specs(_route(points=60), tile_m=100_000.0)[0]
    diagonal = tile_specs(_route(points=60, diagonal=True), tile_m=100_000.0)[0]
    assert diagonal.cells > straight.cells


def test_an_oversized_tile_is_bisected_until_it_fits() -> None:
    """Peak residency is enforced, not hoped for."""
    route = _route(points=120, diagonal=True)
    cap = 4_000_000
    specs = tile_specs(route, tile_m=100_000.0, max_cells=cap)

    assert len(specs) > 1
    assert all(spec.cells <= cap for spec in specs)
    assert sorted(i for s in specs for i in s.point_indices) == list(range(len(route.points)))


def test_tile_grid_coordinates_land_inside_the_tile() -> None:
    route = _route()
    spec = tile_specs(route)[0]
    rows, cols = tile_rows_cols(spec, route)
    height, width = spec.shape

    assert len(rows) == len(spec.point_indices)
    assert np.all((rows >= 0) & (rows < height))
    assert np.all((cols >= 0) & (cols < width))


# --- building heights -------------------------------------------------------


def test_an_explicit_height_wins_over_a_floor_count() -> None:
    assert building_height_m({"height": 31.0, "num_floors": 2}) == (31.0, "height")


def test_floors_are_converted_when_no_height_is_given() -> None:
    height, how = building_height_m({"num_floors": 5})
    assert how == "floors"
    assert height == pytest.approx(16.0)


def test_a_building_with_no_height_is_not_a_zero_height_building() -> None:
    """Scope 3.6 on a footprint. Overture rows frequently lack the attribute."""
    assert building_height_m({"id": "x"}) == (None, "absent")


def test_a_nan_height_is_absent_rather_than_true() -> None:
    """geopandas hands back float('nan'), and `bool(float('nan'))` is True.

    A plain truthiness test would take the NaN branch and rasterize a NaN into the surface,
    which then propagates through every horizon on the tile.
    """
    assert building_height_m({"height": float("nan")}) == (None, "absent")
    assert building_height_m({"height": 0.0}) == (None, "absent")


# --- assembly ---------------------------------------------------------------


def test_a_building_subtends_the_angle_it_should(tmp_path: Path, ctx: Any) -> None:
    """The end-to-end case: DEM plus a building, read through a real horizon profile.

    A 20 m building whose near face is 20 m north of the sample point subtends 45 degrees.
    Everything between the tile bounds and the azimuth convention has to be right for this
    number to come out.
    """
    route = _route(points=4)
    _write_dem(tmp_path, value=0.0)
    # ~20 m north of the route: 0.00018 deg of latitude is 20.0 m.
    _write_buildings(
        tmp_path,
        [{"height": 20.0, "geometry": _square(LAT + 0.00027, LON, 0.00009)}],
    )

    spec = tile_specs(route, cell_m=1.0)[0]
    tile = build_tile(spec, route, ctx)
    rows, cols = tile_rows_cols(spec, route, indices=[0])
    horizon = horizon_profile(tile.surface, spec.cell_m, rows, cols)

    north = horizon[0, 0]
    assert north == pytest.approx(math.radians(45.0), abs=math.radians(4.0))
    # And nothing to the south, or the azimuth convention is inverted.
    assert horizon[0, 36] == 0.0


def test_canopy_and_a_building_do_not_stack(tmp_path: Path, ctx: Any) -> None:
    """`max`, not `+`. Summing puts a 20 m tree on a 10 m roof."""
    import rasterio
    from rasterio.transform import from_origin

    route = _route(points=4)
    _write_dem(tmp_path, value=100.0)
    _write_buildings(tmp_path, [{"height": 10.0, "geometry": _square(LAT + 0.00027, LON, 0.00009)}])

    res = 0.00005
    size = int(0.04 / res)
    with rasterio.open(
        tmp_path / "canopy.tif",
        "w",
        driver="GTiff",
        height=size,
        width=size,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(LON - 0.02, LAT + 0.02, res, res),
        nodata=-9999.0,
    ) as dst:
        dst.write(np.full((size, size), 20.0, dtype="float32"), 1)

    spec = tile_specs(route, cell_m=1.0)[0]
    tile = build_tile(spec, route, ctx)

    peak = float(np.nanmax(tile.surface))
    assert peak == pytest.approx(120.0, abs=0.5), "ground 100 + max(canopy 20, building 10)"
    assert peak < 129.0, "canopy and building were summed"


def test_unknown_ground_reads_as_open_sky_not_as_a_canyon(tmp_path: Path, ctx: Any) -> None:
    """The direction of the error is the point, and it is asserted rather than assumed.

    A DEM nodata of -9999 left in place makes every neighbouring cell rise 10 km, and the
    point reports an 89-degree horizon in all directions — a canyon that is not there, and
    a plan that claims deep shade on an exposed road.
    """
    route = _route(points=4)
    _write_dem(tmp_path, value=0.0, nodata_stripe=True)

    spec = tile_specs(route, cell_m=2.0)[0]
    tile = build_tile(spec, route, ctx, layers=(DEM_LAYER,))

    assert not tile.valid.all(), "the stripe should have produced invalid cells"
    assert np.isnan(tile.surface[~tile.valid]).all()

    rows, cols = tile_rows_cols(spec, route)
    horizon = horizon_profile(tile.surface, spec.cell_m, rows, cols)
    assert float(horizon.max()) < math.radians(5.0), "nodata was read as terrain"


def test_a_missing_layer_is_reported_not_silently_flat(tmp_path: Path, ctx: Any) -> None:
    route = _route(points=4)
    _write_dem(tmp_path)

    spec = tile_specs(route)[0]
    tile = build_tile(spec, route, ctx)
    by_layer = {c.layer: c for c in tile.contributions}

    assert by_layer[DEM_LAYER].available
    assert not by_layer[CANOPY_LAYER].available
    assert "no canopy raster" in (by_layer[CANOPY_LAYER].reason or "")
    assert not by_layer[BUILDINGS_LAYER].available


def test_tiles_are_yielded_lazily(tmp_path: Path, ctx: Any) -> None:
    """A generator, so peak memory does not grow with route length."""
    import types

    _write_dem(tmp_path)
    tiles = iter_dsm_tiles(_route(points=60), ctx, tile_m=300.0)
    assert isinstance(tiles, types.GeneratorType)
    assert next(tiles).surface.shape[0] > 0


# --- coverage ---------------------------------------------------------------


def test_terrain_only_is_reported_at_lower_confidence() -> None:
    """The dangerous case: a near-open horizon down every street, claimed as full sun."""
    terrain_only = DsmCoverage(
        cell_m=1.0,
        layers={
            DEM_LAYER: LayerContribution(layer=DEM_LAYER, available=True, valid_fraction=1.0),
            CANOPY_LAYER: LayerContribution(layer=CANOPY_LAYER, available=False),
            BUILDINGS_LAYER: LayerContribution(layer=BUILDINGS_LAYER, available=False),
        },
    )
    assert terrain_only.confidence == 0.5
    assert terrain_only.describe() == ("shade measured from dem; buildings and canopy unavailable")


def test_a_complete_surface_is_full_confidence() -> None:
    complete = DsmCoverage(
        cell_m=1.0,
        layers={
            name: LayerContribution(layer=name, available=True, valid_fraction=1.0)
            for name in (DEM_LAYER, CANOPY_LAYER, BUILDINGS_LAYER)
        },
    )
    assert complete.confidence == 1.0
    assert "unavailable" not in complete.describe()


def test_partial_canopy_coverage_is_averaged_not_rounded_to_true() -> None:
    """Canopy over the first tile and nothing after is a different claim from full cover."""
    coverage = accumulate_coverage(
        2,
        [
            LayerContribution(layer=CANOPY_LAYER, available=True, valid_fraction=1.0),
            LayerContribution(layer=CANOPY_LAYER, available=False),
        ],
        cell_m=1.0,
    )
    assert coverage.layers[CANOPY_LAYER].available
    assert coverage.layers[CANOPY_LAYER].valid_fraction == pytest.approx(0.5)


def test_buildings_without_heights_are_named_in_coverage() -> None:
    coverage = DsmCoverage(
        cell_m=1.0,
        layers={
            BUILDINGS_LAYER: LayerContribution(
                layer=BUILDINGS_LAYER, available=True, valid_fraction=1.0, rows_without_height=7
            )
        },
    )
    entry = coverage.entries()[0]
    assert entry.source == "overture"
    assert entry.checked
    assert "7 footprint read(s) across tiles" in (entry.reason or "")


# --- corridor horizons ------------------------------------------------------


def test_corridor_horizons_cover_every_route_point(tmp_path: Path, ctx: Any) -> None:
    """One row per route point, whatever the tiling.

    A point dropped between tiles would silently get an open sky and full sun, which is
    exactly the failure the partition test above exists to prevent one layer down.
    """
    from longrun.core.geo.svf import corridor_horizons

    route = _route(points=40)
    _write_dem(tmp_path, value=0.0)

    result = corridor_horizons(route, ctx, cell_m=2.0)
    assert result.horizons.shape == (len(route.points), 72)
    assert result.svf.shape == (len(route.points),)
    assert np.all(result.svf == pytest.approx(1.0)), "flat ground is open sky"


def test_a_wall_of_buildings_lowers_the_sky_view_factor(tmp_path: Path, ctx: Any) -> None:
    """SVF is the diffuse half of scope 7.4, and it has to respond to real geometry."""
    from longrun.core.geo.svf import corridor_horizons

    route = _route(points=6)
    _write_dem(tmp_path, value=0.0)
    _write_buildings(
        tmp_path,
        [
            {"height": 30.0, "geometry": _square(LAT + 0.00027, LON + i * STEP_DEG, 0.00012)}
            for i in range(6)
        ]
        + [
            {"height": 30.0, "geometry": _square(LAT - 0.00027, LON + i * STEP_DEG, 0.00012)}
            for i in range(6)
        ],
    )

    result = corridor_horizons(route, ctx, cell_m=1.0)
    middle = result.svf[len(route.points) // 2]
    assert 0.2 < middle < 0.9, f"a street canyon should cut the sky view, got {middle}"


def test_the_surface_sources_are_reported_on_the_corridor(tmp_path: Path, ctx: Any) -> None:
    from longrun.core.geo.svf import corridor_horizons

    _write_dem(tmp_path)
    result = corridor_horizons(_route(points=8), ctx, cell_m=2.0)

    assert result.coverage.answered == frozenset({DEM_LAYER})
    assert result.coverage.confidence == 0.5
    assert "canopy" in result.coverage.describe()


def test_two_fully_covered_tiles_average_to_one_rather_than_overflowing() -> None:
    """The regression: `valid_fraction` is constrained to [0, 1] on the model.

    Accumulating into it before dividing raised a ValidationError on any route long enough
    to need two tiles — which the existing averaging test missed, because its two
    contributions happened to sum to exactly 1.0.
    """
    coverage = accumulate_coverage(
        2,
        [
            LayerContribution(layer=DEM_LAYER, available=True, valid_fraction=1.0),
            LayerContribution(layer=DEM_LAYER, available=True, valid_fraction=1.0),
        ],
        cell_m=1.0,
    )
    assert coverage.layers[DEM_LAYER].valid_fraction == pytest.approx(1.0)
