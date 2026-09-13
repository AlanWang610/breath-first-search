"""Protected areas from PAD-US, over ArcGIS (scope 7.5, 7.6, 13 step 2).

The `parks` layer. `services_along` reads it to say whether a water point is inside a park
— which is what raises its confidence from "a node exists" to "a node exists somewhere a
fountain is maintained" — and M4's `trail_status` and `access_hours` will read the *agency*
columns to decide whose alerts page to consult at all.

**A bbox query, not a national download**, and the reason is the same one that earned
Overture its exception in [ADR 0009](../../../docs/decisions/0009-osm-loads-into-postgis.md):
USGS publishes PAD-US as a public ArcGIS FeatureServer with a spatial index, so a region's
protected areas come back in a handful of paged requests rather than out of a multi-gigabyte
national GeoPackage. The rows still land in `padus.units` like every other layer, so nothing
downstream knows the transport was different — which is the half of the Overture precedent
worth generalising.

**The agency columns are the point, not decoration.** `Mang_Name` is what resolves a park to
a managing agency and therefore to an adapter; §13 step 4's "look up which adapters exist"
has nothing to look up without it. They are loaded now, months before the registry that
reads them, because a reload costs a region build and adding a column does not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.core.data.national import NationalSource

if TYPE_CHECKING:  # pragma: no cover
    from geopandas import GeoDataFrame

    from longrun.core.models.geometry import BBox

#: USGS's public PAD-US service. `PADUS_Management_Areas` rather than `Fee_Managers`: a
#: runner cares who *manages* the ground and posts its closures, not who holds the title.
PADUS_ROOT = (
    "https://services.arcgis.com/v01gqwM5QqNysAAi/arcgis/rest/services/"
    "PADUS_Management_Areas/FeatureServer/0/query"
)

#: Rows per request. The server caps this itself and says so with `exceededTransferLimit`,
#: which is what the pager keys off rather than this number.
PAGE_SIZE = 1000

#: How many pages before giving up. A region of protected areas is thousands of polygons,
#: not millions, so hitting this means a query that is not doing what it looks like.
MAX_PAGES = 60

#: PAD-US fields worth carrying, and what each is for.
FIELDS: tuple[str, ...] = (
    "Unit_Nm",  # the park's name, for the plan sheet
    "Mang_Name",  # managing agency - what M4 resolves an adapter from
    "Mang_Type",  # federal / state / local / private
    "Des_Tp",  # designation: national park, local park, wilderness area
    "Own_Type",
    "Loc_Ds",  # the agency's own designation, where it differs
)

PADUS = NationalSource(
    layer="parks",
    schema="padus",
    table="units",
    key="unit_id",
    geometry="MultiPolygon",
    columns={
        "name": "text",
        "agency": "text",
        "agency_type": "text",
        "designation": "text",
        "owner_type": "text",
        "local_designation": "text",
    },
    source="padus",
    licence="US public domain",
    url_template=PADUS_ROOT,
    vintage="padus-4.1",
)


def query_params(bbox: BBox, offset: int = 0, page_size: int = PAGE_SIZE) -> dict[str, Any]:
    """One page of an ArcGIS spatial query.

    A named function with its own test because the parameter names are unforgiving and the
    failure is quiet: `inSR` omitted makes the server read the envelope in the *layer's*
    projection, which for a WGS84 envelope over California selects a bbox in the Pacific
    and returns zero features rather than an error.
    """
    return {
        "where": "1=1",
        "geometry": f"{bbox.min_lon},{bbox.min_lat},{bbox.max_lon},{bbox.max_lat}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": ",".join(FIELDS),
        "f": "geojson",
        # ArcGIS documents `resultOffset` as requiring a sort: without one the server may
        # page in any order it likes, so a second page can repeat or skip rows from the
        # first. Checked against this service and it happened to be stable either way -
        # which is exactly the kind of thing that stops being true after an upgrade.
        "orderByFields": "OBJECTID",
        "resultOffset": offset,
        "resultRecordCount": page_size,
    }


def fetch_units(bbox: BBox, *, timeout_s: float = 120.0) -> GeoDataFrame:
    """Every protected area meeting a bbox, paged until the server stops truncating.

    Paged on `exceededTransferLimit` rather than on a row count, because the cap is the
    server's and it moves: a pager that stopped at its own `PAGE_SIZE` would silently take
    the first thousand parks of a region and call it the region.
    """
    import geopandas as gpd
    import httpx
    from shapely.geometry import shape

    records: list[dict[str, Any]] = []
    geometries: list[Any] = []
    offset = 0

    for _ in range(MAX_PAGES):
        response = httpx.get(PADUS_ROOT, params=query_params(bbox, offset), timeout=timeout_s)
        response.raise_for_status()
        payload = response.json()
        features = payload.get("features") or []
        for index, feature in enumerate(features):
            raw = feature.get("geometry")
            if not raw:
                continue
            properties = feature.get("properties") or {}
            for part, piece in enumerate(_parts(shape(raw))):
                records.append(_normalise(properties, offset + index, part))
                geometries.append(piece)
        offset += len(features)
        if not features or not (payload.get("properties") or {}).get("exceededTransferLimit"):
            break

    if not records:
        columns = [PADUS.key, *PADUS.columns]
        return gpd.GeoDataFrame({c: [] for c in columns}, geometry=[], crs="EPSG:4326")
    return gpd.GeoDataFrame(records, geometry=geometries, crs="EPSG:4326")


def _parts(geometry: Any) -> list[Any]:
    """One row's geometry as separate polygons.

    **PAD-US publishes aggregates, and they are enormous.** The Bay Area corridor pulled
    back two unnamed `CITY`-managed rows of **15,243 and 18,573 parts** - 19.7 MB of a
    19.9 MB fixture, for 106 features. They are every local park in a jurisdiction dissolved
    into one row.

    Kept whole, such a row answers a corridor query with "somewhere inside this eighteen
    thousand part blob there is a park near you", which is true, useless, and costs a
    point-in-polygon test against the whole thing. Split, each part is a park with its own
    boundary, and a corridor query returns the two or three that actually meet it.

    The agency and designation are copied to every part, which is what they mean: an
    aggregate row is one *manager*, not one place.
    """
    parts = list(getattr(geometry, "geoms", [geometry]))
    return [part for part in parts if part is not None and not part.is_empty]


def _normalise(properties: dict[str, Any], index: int, part: int) -> dict[str, Any]:
    """PAD-US field names to ours, with a stable id derived from the row.

    PAD-US's `OBJECTID` is a service-side row number that changes between releases, so it
    is not an id an upsert can key on across two builds. The name and agency together are
    what identifies a unit to a reader; `index` disambiguates the genuinely repeated ones -
    a county with nine unnamed `CITY` mini parks is ordinary - and `part` the pieces of an
    exploded aggregate.
    """
    name = str(properties.get("Unit_Nm") or "").strip() or "unnamed"
    agency = str(properties.get("Mang_Name") or "").strip() or "unknown"
    return {
        PADUS.key: f"{agency}:{name}:{index}:{part}",
        "name": name,
        "agency": agency,
        "agency_type": properties.get("Mang_Type"),
        "designation": properties.get("Des_Tp"),
        "owner_type": properties.get("Own_Type"),
        "local_designation": properties.get("Loc_Ds"),
    }


def load_padus(connection: Any, region: str, bbox: BBox) -> int:
    """Load a region's protected areas into `padus.units`."""
    from longrun.core.data.national import load_frame

    return load_frame(connection, fetch_units(bbox), PADUS, region=region, source_url=PADUS_ROOT)


__all__ = [
    "FIELDS",
    "MAX_PAGES",
    "PADUS",
    "PADUS_ROOT",
    "PAGE_SIZE",
    "fetch_units",
    "load_padus",
    "query_params",
]
