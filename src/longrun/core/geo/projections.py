"""Coordinate reference systems (scope 4.1).

One rule, applied everywhere: coordinates are stored as WGS84 lon/lat and every length,
buffer and area is computed in a per-route local UTM projection. Mixing the two is the
classic way a geospatial codebase rots — a buffer in degrees is wider in longitude than
in latitude, and by a factor that changes with where you are.

The projection is chosen from the route centroid, so a route that spans a zone boundary
still gets a single consistent metric frame rather than a discontinuity mid-route. Over a
100 km route (scope 1) the scale error from staying in one zone is far smaller than the
error from switching frames partway.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyproj import CRS, Transformer

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.geometry import BBox, LatLon, Route

WGS84 = CRS.from_epsg(4326)


def utm_epsg(lat: float, lon: float) -> int:
    """The EPSG code of the UTM zone containing a point.

    326xx north of the equator, 327xx south.
    """
    zone = int((lon + 180.0) // 6.0) + 1
    zone = min(max(zone, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


def centroid(route: Route) -> LatLon:
    """The mean position of a route's points, used to pick its projection."""
    from longrun.core.models.geometry import LatLon

    n = len(route.points)
    return LatLon(
        lat=sum(p.lat for p in route.points) / n,
        lon=sum(p.lon for p in route.points) / n,
    )


def local_crs(route: Route) -> CRS:
    """The metric CRS this route's measurements are computed in."""
    c = centroid(route)
    return CRS.from_epsg(utm_epsg(c.lat, c.lon))


def transformer_to(crs: CRS) -> Transformer:
    """A lon/lat to `crs` transformer.

    `always_xy=True` because pyproj otherwise honours each CRS's declared axis order,
    which for EPSG:4326 is lat/lon — a silent source of transposed coordinates.
    """
    return Transformer.from_crs(WGS84, crs, always_xy=True)


def transformer_from(crs: CRS) -> Transformer:
    """The inverse of `transformer_to`."""
    return Transformer.from_crs(crs, WGS84, always_xy=True)


def bbox_of(route: Route, pad_deg: float = 0.0) -> BBox:
    """The route's bounding box, optionally padded, for scoping raster reads."""
    from longrun.core.models.geometry import BBox

    lats = [p.lat for p in route.points]
    lons = [p.lon for p in route.points]
    return BBox(
        min_lon=max(-180.0, min(lons) - pad_deg),
        min_lat=max(-90.0, min(lats) - pad_deg),
        max_lon=min(180.0, max(lons) + pad_deg),
        max_lat=min(90.0, max(lats) + pad_deg),
    )
