"""Store interfaces: the seam that keeps scorers independent of PostGIS (scope 4.4, 5).

Every scorer opens the same way — buffer the route, query a layer — so that is the whole
interface. Two implementations satisfy it: `PostGISLayerStore` against the real database,
and `FileLayerStore` reading committed GeoPackage fixtures. Tests run on the latter, which
is why the golden suite is hermetic and CI needs no services; a `network`-marked
equivalence test keeps the two honest.

Nothing above this module knows which one it has.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from geopandas import GeoDataFrame

    from longrun.core.models.features import FeatureKind, FeatureSet
    from longrun.core.models.geometry import BBox, Corridor, Route
    from longrun.core.models.jurisdiction import AdapterInfo, Jurisdiction


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

    def has_layer(self, layer: str) -> bool:
        """Whether this store carries the layer at all (scope 3.6).

        On the protocol rather than only on the implementations, because it is the
        difference between "nobody loaded this" and "this corridor has none of it" — and
        both stores already implement it.
        """
        ...

    def vintage(self, layer: str) -> str | None:
        """Source vintage for the manifest's data-snapshot pins (scope 6.4)."""
        ...


class RasterWindow(NamedTuple):
    """A raster read, with the metadata needed to place it on the ground.

    `read_window` returns the array and transform alone, which is enough for `dem.py`
    because it samples in the raster's own coordinates. Assembling a DSM is not: three
    sources arrive in three CRSs at three resolutions and have to be warped onto one
    metric grid, and doing that needs to know what CRS each one was in and what its
    nodata value is.
    """

    array: Any
    transform: Any
    crs: Any
    nodata: float | None
    res_m: float | None


@runtime_checkable
class RasterStore(Protocol):
    """Windowed reads from COGs: DEM, canopy height, cached sky-view-factor tiles."""

    def read_window(self, layer: str, bbox: BBox) -> Any:
        """A numpy array plus affine transform for the requested window."""
        ...

    def read_window_meta(self, layer: str, bbox: BBox) -> RasterWindow | None:
        """The same window, with CRS, nodata and metric resolution attached."""
        ...

    def has_layer(self, layer: str) -> bool:
        """Whether the raster exists at all, distinct from having no coverage here."""
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


@runtime_checkable
class FeatureSource(Protocol):
    """Jurisdiction adapters, reached the way every other source is (scope 7.10).

    `adapters/` may legally be imported from `core/` — the layering arrow points that way.
    This Protocol exists anyway, and the reason is the same one `LayerStore` exists for: a
    scorer that reached the registry by import would be the only source in the codebase
    obtained as ambient module state. Every other thing a scorer reads — layers, rasters,
    cache, clock, profile — arrives on the context and can therefore be replaced by a stub
    in a test that needs no entry points, no `importlib.metadata`, and no installed package.

    It also keeps `core/` importable on a bare `uv sync`: whether an adapter needs `httpx`,
    `duckdb` or eventually a model client is structurally none of a scorer's business.
    """

    def fetch(
        self,
        kind: FeatureKind,
        jurisdictions: Sequence[Jurisdiction],
        polygon: Any,
        day: date,
    ) -> FeatureSet:
        """Everything of `kind` in `polygon` on `day`, and who was asked.

        Never raises. A feed that is down, a key that is not set and a jurisdiction nobody
        covers are all `JurisdictionAnswer`s with reasons — scope 3.6 applied to a source
        that is a whole registry rather than a single endpoint.
        """
        ...

    def adapters_for(self, kind: FeatureKind, jurisdiction: Jurisdiction) -> list[AdapterInfo]:
        """Who claims this jurisdiction, best tier first. No fetch, no budget spent.

        Scope 13 step 4 is "resolve the jurisdictions crossed **and look up which adapters
        exist for each**" — a question a region build asks with no corridor, no date and no
        `ScorerContext` to spend a budget from.
        """
        ...


class NullFeatureSource:
    """No adapters configured: every answer is an honest `checked=False`.

    The counterpart of `NullRouter`, and it earns its place for the same reason — a plan
    produced before the registry is wired up must say so rather than report a clean sheet.
    """

    #: Why nobody was asked. Named so a test can assert on the reason rather than on prose.
    reason = "no adapter registry configured for this plan"

    def fetch(
        self,
        kind: FeatureKind,
        jurisdictions: Sequence[Jurisdiction],
        polygon: Any,
        day: date,
    ) -> FeatureSet:
        from longrun.core.models.features import FeatureSet, JurisdictionAnswer

        return FeatureSet.from_answers(
            [],
            [
                JurisdictionAnswer(
                    jurisdiction=j.id, name=j.name, kind=kind, checked=False, reason=self.reason
                )
                for j in jurisdictions
            ],
        )

    def adapters_for(self, kind: FeatureKind, jurisdiction: Jurisdiction) -> list[AdapterInfo]:
        return []
