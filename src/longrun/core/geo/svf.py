"""Horizons and sky view factor for a whole route corridor (scope 5, 7.4).

`raycast.py` answers for one grid; `dsm.py` produces grids a tile at a time. This is the
join: walk the tiles, cast from the route points each tile owns, and assemble one
`(n_points, n_azimuths)` horizon array for the route — which is what `sun_exposure` needs
for direct sun and, through `sky_view_factor`, for diffuse.

**No COG tile cache, and that is a decision rather than an omission.** Scope 5 specifies
sky view factor "cached as COG tiles keyed by tile ID", and its stated reason is cost:
region-wide SVF is 10⁹–10¹⁰ pixels for a metro area. ADR 0003 measured the per-corridor
cost at 0.46 s for a 100 km route. A cache that saves half a second, in exchange for an
invalidation problem the moment a canopy vintage or a building footprint changes, is not
worth having. Recomputing is cheaper than remembering here.

The corridor is walked once and the horizons are kept; the tiles are not. A horizon array
for a 100 km route at 72 azimuths is 1000 x 72 floats — half a megabyte — while the tiles
it came from are 22 MB each. Keeping the answer and discarding the evidence is the whole
reason the tile is the unit of work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from longrun.core.geo.dsm import (
    BUILDINGS_LAYER,
    CANOPY_LAYER,
    DEFAULT_CELL_M,
    DEM_LAYER,
    LayerContribution,
    accumulate_coverage,
    iter_dsm_tiles,
    tile_rows_cols,
)
from longrun.core.geo.raycast import LADDER, horizon_profile, sky_view_factor

if TYPE_CHECKING:  # pragma: no cover
    from numpy.typing import NDArray

    from longrun.core.geo.dsm import DsmCoverage
    from longrun.core.geo.raycast import RaycastSettings
    from longrun.core.models.context import ScorerContext
    from longrun.core.models.geometry import Route


@dataclass(frozen=True)
class CorridorHorizons:
    """The skyline around every route point, and what it was built from."""

    horizons: NDArray[np.float64]
    svf: NDArray[np.float64]
    support: NDArray[np.float64]
    coverage: DsmCoverage
    settings: RaycastSettings

    @property
    def azimuths(self) -> int:
        return int(self.horizons.shape[1])

    def shaded_by_skyline(self, threshold: float = 0.5) -> NDArray[np.bool_]:
        """Points whose sky is more than half obstructed, by view factor."""
        return self.svf < threshold


def corridor_horizons(
    route: Route,
    ctx: ScorerContext,
    *,
    settings: RaycastSettings | None = None,
    cell_m: float = DEFAULT_CELL_M,
    layers: tuple[str, ...] = (DEM_LAYER, CANOPY_LAYER, BUILDINGS_LAYER),
) -> CorridorHorizons:
    """Cast a horizon profile at every route point, tile by tile.

    Points whose tile produced no valid surface keep a zero horizon — an open sky — and
    are marked in `support`. That is the conservative direction stated in `dsm.py`: claim
    less shade than there may be, and report having done so. A caller that treats
    `support` as decoration turns "we could not tell" back into "there is nothing there".
    """
    config = settings or LADDER[0]
    count = len(route.points)
    horizons = np.zeros((count, config.azimuths), dtype=np.float64)
    support = np.zeros(count, dtype=np.float64)

    contributions: list[LayerContribution] = []
    tiles = 0
    for tile in iter_dsm_tiles(route, ctx, cell_m=cell_m, layers=layers, settings=config):
        tiles += 1
        contributions.extend(tile.contributions)
        indices = list(tile.spec.point_indices)
        if not indices:
            continue

        rows, cols = tile_rows_cols(tile.spec, route, indices=indices)
        horizons[indices] = horizon_profile(tile.surface, tile.cell_m, rows, cols, config)
        # How much of each sample's surroundings the tile actually knew. A point whose
        # neighbourhood is half nodata has a horizon built partly from ground never seen.
        support[indices] = float(tile.valid.mean())

    return CorridorHorizons(
        horizons=horizons,
        svf=sky_view_factor(horizons),
        support=support,
        coverage=accumulate_coverage(tiles, contributions, cell_m),
        settings=config,
    )


__all__ = ["CorridorHorizons", "corridor_horizons"]
