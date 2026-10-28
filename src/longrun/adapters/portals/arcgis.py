"""ArcGIS: the feature-layer query a catalog row becomes (M21).

`GET <layer>/query` with an envelope - the corridor's snapped box in WGS84 - intersected
against the layer, `f=geojson` so a geometry arrives in the shape `Feature` stores, and
`outSR=4326` so it arrives in degrees whatever the layer is drawn in. Pages are read while
the server says `exceededTransferLimit`, which is the only signal it gives: the number of
records per page is the server's to cap and may be lower than asked.

**Times are epoch milliseconds.** An ArcGIS date field is milliseconds since 1970 UTC in a
GeoJSON response; a date-only field is midnight of the day, stored as UTC or as local
midnight converted. `catalog.py` reads both formats.

**An ArcGIS licence is free HTML**, and nothing in a layer's metadata is a licence id a test
could check. So no ArcGIS payload is committed (ADR 0061): `metadata` records only when the
layer was last edited.
"""

from __future__ import annotations

from datetime import UTC, datetime
from datetime import date as date_type
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable
    from datetime import date

    from longrun.adapters.base import AdapterContext
    from longrun.adapters.portals.catalog import CatalogEntry
    from longrun.adapters.portals.client import Box


def where(entry: CatalogEntry, day: date) -> str:
    """The SQL `where` for one day: the row's own filter and the window."""
    from longrun.adapters.portals.client import window_bounds

    parts: list[str] = []
    if entry.where:
        parts.append(f"({entry.where})")
    if entry.window_filter:
        ends_after, starts_before = window_bounds(day)
        if entry.end is not None:
            column = entry.end.field
            parts.append(
                f"({column} IS NULL OR {column} >= TIMESTAMP '{ends_after.isoformat()} 00:00:00')"
            )
        if entry.start is not None:
            column = entry.start.field
            parts.append(
                f"({column} IS NULL OR {column} <= "
                f"TIMESTAMP '{starts_before.isoformat()} 23:59:59')"
            )
    return " AND ".join(parts) or "1=1"


def produce(
    entry: CatalogEntry, bbox: Box | None, day: date, ctx: AdapterContext
) -> Callable[[], Any]:
    """The producer for one query: every page of features, or an error."""
    from longrun.adapters.http import get_json
    from longrun.adapters.portals.client import MAX_PAGES, PAGE_SIZE

    clause = where(entry, day)

    def run() -> Any:
        features: list[Any] = []
        for _ in range(MAX_PAGES):
            ctx.budget.spend_api_call()
            params: dict[str, Any] = {
                "where": clause,
                "outFields": "*",
                "outSR": 4326,
                "f": "geojson",
                "resultOffset": len(features),
                "resultRecordCount": PAGE_SIZE,
            }
            if bbox is not None:
                params.update(
                    geometry=",".join(str(v) for v in bbox),
                    geometryType="esriGeometryEnvelope",
                    inSR=4326,
                    spatialRel="esriSpatialRelIntersects",
                )
            page = get_json(entry.url, ctx, params=params)
            _raise_for_error(page)
            batch = page.get("features") if isinstance(page, dict) else None
            if not isinstance(batch, list):
                raise RuntimeError("the layer answered without a features list")
            features.extend(batch)
            if not exceeded(page) or not batch:
                return {"type": "FeatureCollection", "features": features}
        raise RuntimeError(
            f"more than {MAX_PAGES} pages of records in this corridor; refused rather than "
            f"truncated"
        )

    return run


def exceeded(page: Any) -> bool:
    """Whether the server held records back. GeoJSON responses put the flag either at the
    top level or under `properties`, depending on the server's version."""
    if not isinstance(page, dict):
        return False
    nested = page.get("properties")
    flagged = isinstance(nested, dict) and nested.get("exceededTransferLimit")
    return bool(page.get("exceededTransferLimit") or flagged)


def _raise_for_error(page: Any) -> None:
    """ArcGIS reports a bad query as HTTP 200 with an `error` object."""
    if isinstance(page, dict) and isinstance(page.get("error"), dict):
        error = page["error"]
        raise RuntimeError(f"ArcGIS error {error.get('code')}: {error.get('message')}")


def rows(payload: Any) -> list[dict[str, Any]]:
    """A cached payload's features, each flattened to its attributes plus `geometry`."""
    found = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(found, list):
        return []
    out: list[dict[str, Any]] = []
    for feature in found:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties")
        out.append(
            {
                **(properties if isinstance(properties, dict) else {}),
                "geometry": feature.get("geometry"),
            }
        )
    return out


def metadata(entry: CatalogEntry, ctx: AdapterContext) -> Callable[[], Any]:
    """The producer for the weekly metadata read: when the layer's data last changed."""
    from longrun.adapters.http import get_json

    def run() -> Any:
        ctx.budget.spend_api_call()
        layer = get_json(f"https://{entry.domain}{entry.dataset}", ctx, params={"f": "json"})
        _raise_for_error(layer)
        if not isinstance(layer, dict):
            raise RuntimeError("the layer's metadata is not an object")
        stamp = updated(layer)
        if stamp is None and entry.freshness_field:
            ctx.budget.spend_api_call()
            stamp = newest(entry, ctx)
        return {"updated": stamp, "licence": None}

    return run


def newest(entry: CatalogEntry, ctx: AdapterContext) -> str | None:
    """The newest value of a row's `freshness_field`, as an ISO date: when a map service
    with no `editingInfo` last gained a record. A date field arrives as epoch milliseconds,
    a text one as whatever the publisher wrote, read only if it starts with an ISO date."""
    import json

    from longrun.adapters.http import get_json

    statistic = [
        {
            "statisticType": "max",
            "onStatisticField": entry.freshness_field,
            "outStatisticFieldName": "newest",
        }
    ]
    page = get_json(
        f"https://{entry.domain}{entry.dataset}/query",
        ctx,
        params={"where": "1=1", "outStatistics": json.dumps(statistic), "f": "json"},
    )
    _raise_for_error(page)
    features = page.get("features") if isinstance(page, dict) else None
    if not features:
        return None
    value = (features[0].get("attributes") or {}).get("newest")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000.0, UTC).date().isoformat()
    text = str(value or "")[:10]
    try:
        return date_type.fromisoformat(text).isoformat()
    except ValueError:
        return None


def updated(layer: dict[str, Any]) -> str | None:
    """When a layer's data last changed, as an ISO date. `dataLastEditDate` where the server
    separates data from schema edits; `lastEditDate` otherwise."""
    info = layer.get("editingInfo")
    if not isinstance(info, dict):
        return None
    stamp = info.get("dataLastEditDate") or info.get("lastEditDate")
    if not isinstance(stamp, (int, float)):
        return None
    return datetime.fromtimestamp(float(stamp) / 1000.0, UTC).date().isoformat()


__all__ = ["exceeded", "metadata", "newest", "produce", "rows", "updated", "where"]
