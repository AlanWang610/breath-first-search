"""Assigning OSM ways to route points without a router (scope 6.1, 7.1).

Repair mode is the entry path scope 6.1 expects most experienced users to take, and it
never invokes a router — so `map_match` is not available and nothing has told us which
way each point lies on. Without that, every way-tag scorer (hostility, legality, surface)
has nothing to read and reports `unknown` for the whole route, which makes the common
entry mode the least useful one.

This is nearest-way snapping, and it is deliberately not called map matching. A real map
matcher is a hidden Markov model over the road graph that uses topology and travel
direction to resolve ambiguity. This picks the closest line within a tolerance, point by
point. It is adequate for reading tags off an urban corridor and it is *not* adequate for
building the accepted-road set in scope 6.2, which is why that waits for the router
(risk R4, retired: GraphHopper returns real `osm_way_id` values from `POST /match`).

Two safeguards keep it honest: a point further than `tolerance_m` from every way gets
`None` rather than the least-bad guess, and the whole result carries a match rate the
caller can report.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from longrun.core.geo.projections import local_crs, transformer_to
from longrun.core.models.geometry import Route

if TYPE_CHECKING:  # pragma: no cover
    from geopandas import GeoDataFrame

#: How far a route point may sit from a way and still be considered on it.
#:
#: Wide enough to cover a sidewalk mapped separately from its roadway centreline and
#: ordinary GPS drift; narrow enough not to snap across a divided highway's carriageways.
DEFAULT_TOLERANCE_M = 25.0


class WayMatch(BaseModel):
    """Per-point way assignment plus how well it went."""

    way_ids: list[int | None] = Field(default_factory=list)
    distances_m: list[float] = Field(default_factory=list)
    tolerance_m: float = DEFAULT_TOLERANCE_M

    @property
    def matched(self) -> int:
        return sum(1 for w in self.way_ids if w is not None)

    @property
    def match_rate(self) -> float:
        return self.matched / len(self.way_ids) if self.way_ids else 0.0

    @property
    def is_usable(self) -> bool:
        """Whether enough of the route matched to be worth reporting tags from.

        Below this the tag-driven scorers are describing a minority of the route, and
        saying so is more useful than a mostly-empty measurement.
        """
        return self.match_rate >= 0.5


def assign_way_ids(
    route: Route,
    ways: GeoDataFrame,
    tolerance_m: float = DEFAULT_TOLERANCE_M,
    id_column: str = "way_id",
) -> WayMatch:
    """Snap each route point to the nearest way within `tolerance_m`.

    Distances are computed in the route's local metric CRS, never in degrees.
    """
    if ways is None or len(ways) == 0 or id_column not in ways.columns:
        return WayMatch(
            way_ids=[None] * len(route.points),
            distances_m=[float("inf")] * len(route.points),
            tolerance_m=tolerance_m,
        )

    from shapely.geometry import Point
    from shapely.strtree import STRtree

    crs = local_crs(route)
    to_local = transformer_to(crs)

    projected = ways.to_crs(crs.to_epsg())
    geometries = list(projected.geometry)
    identifiers = list(projected[id_column])
    tree = STRtree(geometries)

    way_ids: list[int | None] = []
    distances: list[float] = []

    for point in route.points:
        x, y = to_local.transform(point.lon, point.lat)
        here = Point(x, y)
        index = tree.nearest(here)
        if index is None:
            way_ids.append(None)
            distances.append(float("inf"))
            continue
        distance = float(geometries[int(index)].distance(here))
        distances.append(distance)
        way_ids.append(_as_int(identifiers[int(index)]) if distance <= tolerance_m else None)

    return WayMatch(way_ids=way_ids, distances_m=distances, tolerance_m=tolerance_m)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
