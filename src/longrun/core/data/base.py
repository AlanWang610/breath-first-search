"""Store interfaces: the seam that keeps scorers independent of PostGIS (scope 4.4, 5).

Every scorer opens the same way — buffer the route, query a layer — so that is the whole
interface. Two implementations satisfy it: `PostGISLayerStore` against the real database,
and `FileLayerStore` reading committed GeoPackage fixtures. Tests run on the latter, which
is why the golden suite is hermetic and CI needs no services; a `network`-marked
equivalence test keeps the two honest.

Nothing above this module knows which one it has.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from geopandas import GeoDataFrame

    from longrun.core.models.geometry import BBox, Corridor, Route


@runtime_checkable
class LayerStore(Protocol):
    """Vector layers, queried by route corridor."""

    def ways_in_corridor(self, corridor: Corridor) -> GeoDataFrame:
        """OSM ways with tags intersecting the corridor."""
        ...

    def points_in_corridor(
        self, corridor: Corridor, kinds: list[str], layer: str = "amenities"
    ) -> GeoDataFrame:
        """Point features of the given kinds within the corridor.

        `layer` matters for honest coverage, not just tidiness. Signals and gates come
        from `nodes`; water, toilets and food from `amenities`. Sharing one layer would
        make a region that extracted fountains but never extracted signal nodes look like
        a region with no signals - turning "unknown" into a confident "none".
        """
        ...

    def polygons_intersecting(self, corridor: Corridor, layer: str) -> GeoDataFrame:
        """Areal features — parks, jurisdictions, protected areas — meeting the corridor."""
        ...

    def lines_crossing(self, route: Route, layer: str) -> GeoDataFrame:
        """Features the route line actually crosses: rail, hydrography, boundaries."""
        ...

    def vintage(self, layer: str) -> str | None:
        """Source vintage for the manifest's data-snapshot pins (scope 6.4)."""
        ...


@runtime_checkable
class RasterStore(Protocol):
    """Windowed reads from COGs: DEM, canopy height, cached sky-view-factor tiles."""

    def read_window(self, layer: str, bbox: BBox) -> Any:
        """A numpy array plus affine transform for the requested window."""
        ...

    def resolution_m(self, layer: str) -> float | None:
        """Ground resolution, recorded in the manifest (1 m LiDAR vs 10 m 3DEP)."""
        ...


@runtime_checkable
class Cache(Protocol):
    """External API results keyed by (tool, args hash, date) (scope 4.4).

    In offline mode a miss is an error rather than a fetch. That single behaviour is what
    makes golden tests hermetic, and it doubles as the cassette mechanism for adapter
    contract tests — so there is no second recording system to maintain.
    """

    def get(self, tool: str, args_hash: str, day: str) -> Any | None: ...

    def put(self, tool: str, args_hash: str, day: str, value: Any) -> None: ...

    @property
    def offline(self) -> bool: ...
