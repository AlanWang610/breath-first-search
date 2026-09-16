"""A name the runner wants to stay off becomes an area the router will not enter (scope 6.4).

> **Via points** and **must-avoid ways/areas** by name or polygon.

The polygon half has worked since M1 and the *name* half has not: `PlanRequest.avoid_names`
was written by `agent/intent.py` and read by nothing, so "avoid El Camino" reached the
planner, was recorded on the request, and changed no route. `core/routing/detour.py` says as
much beside `round_coordinates`, which it made public for this.

**A name resolves to an extent, not a point.** A road is a line and a geocoder's `lat/lon`
is one arbitrary spot on it, so a disc around that spot would avoid two hundred metres of a
road somebody asked to stay off for its whole length. Nominatim's bounding box is the extent
it knows, so that is what is used where there is one.

**And an extent is capped.** A bounding box around a long expressway is tens of square
kilometres, and closing that much city takes the parallel streets with it - the streets a
detour was going to use. `detour_area` makes the same argument about boxes and buffers a
line instead. Past the cap the area is refused *by name*, with its size in the reason, which
is a thing the sheet can say and a runner can act on. Silently routing through an avoid they
asked for is the one outcome ruled out.

Rounding is `detour.round_coordinates` at `AREA_PRECISION`, and sharing it is not cosmetic:
an avoid-area travels inside `custom_model`, which `CachedRouter` hashes into its key, and
M5.13 is the milestone that was spent finding out what unrounded coordinates do to a cache
key across two platforms.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.core.routing.detour import round_coordinates

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    from longrun.core.data.geocode import Place
    from longrun.core.models.context import ScorerContext

#: Radius closed around a geocoded point when the endpoint gave no bounding box. Enough to
#: cover a junction and its approaches, and small enough not to close the next street.
POINT_RADIUS_M = 150.0

#: The largest area one named avoid may close. A named road's box is long and thin, so this
#: admits a city street and refuses a whole expressway corridor - see the module docstring
#: for why refusing is better than closing it.
MAX_AREA_KM2 = 4.0


def polygons_for_names(
    names: Sequence[str], ctx: ScorerContext
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve each name to an avoid-area, and say what could not be resolved.

    Returns the areas and the notes. Notes rather than exceptions, because scope 3.6's rule
    holds here too: a plan that quietly dropped an avoid is worse than one that says it
    could not honour it.
    """
    from longrun.core.data.geocode import MAX_LOOKUPS_PER_PLAN, geocode

    areas: list[dict[str, Any]] = []
    notes: list[str] = []
    asked = list(names)[:MAX_LOOKUPS_PER_PLAN]
    if len(names) > len(asked):
        notes.append(
            f"only the first {MAX_LOOKUPS_PER_PLAN} avoid-by-name lookups were made; "
            f"{len(names) - len(asked)} not looked up"
        )

    for index, name in enumerate(asked):
        found = geocode(name, ctx)
        for entry in found.coverage():
            ctx.coverage.record(entry)
        place = found.best
        if place is None:
            notes.append(f"avoid {name!r} was not honoured: {found.reason or 'no match'}")
            continue
        area = area_for(place, area_id=f"avoid-{index}")
        if area is None:
            notes.append(
                f"avoid {name!r} was not honoured: its extent is larger than "
                f"{MAX_AREA_KM2:g} km2, and closing that much would take the streets "
                "around it with it"
            )
            continue
        areas.append(area)
    return areas, notes


def area_for(place: Place, *, area_id: str = "avoid") -> dict[str, Any] | None:
    """One place as a GeoJSON feature `route_body` can pass as an area, or `None` if too big."""
    from pyproj import CRS
    from shapely.geometry import Point, box, mapping
    from shapely.ops import transform as shapely_transform

    from longrun.core.geo.projections import transformer_from, transformer_to, utm_epsg

    crs = CRS.from_epsg(utm_epsg(place.lat, place.lon))
    to_local, to_wgs = transformer_to(crs), transformer_from(crs)

    if place.bbox is not None:
        south, north, west, east = place.bbox
        xs, ys = to_local.transform([west, east], [south, north])
        local = box(min(xs), min(ys), max(xs), max(ys))
        # A box is the extent Nominatim knows, and a *degenerate* box is a point that
        # happened to carry one - a zero-width road bbox is not an area, so it is buffered
        # like any other point rather than sent as an empty polygon.
        if local.area <= 0:
            local = local.centroid.buffer(POINT_RADIUS_M)
    else:
        x, y = to_local.transform(place.lon, place.lat)
        local = Point(x, y).buffer(POINT_RADIUS_M)

    if local.area / 1_000_000 > MAX_AREA_KM2:
        return None

    geometry = mapping(shapely_transform(lambda x, y: to_wgs.transform(x, y), local))
    return {
        "type": "Feature",
        "id": area_id,
        "properties": {},
        "geometry": round_coordinates(geometry),
    }


__all__ = ["MAX_AREA_KM2", "POINT_RADIUS_M", "area_for", "polygons_for_names"]
