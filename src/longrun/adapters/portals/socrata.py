"""Socrata: the SODA query a catalog row becomes (M21).

`GET /resource/<id>.json` with a SoQL `$where`, paged by `$offset` in `:id` order so a page
boundary cannot skip or repeat a row while the table is being written. The clause is the
row's own `where`, the corridor, and - unless the row's dates are text - the window:

* **The corridor** by how the row maps its geometry. A GeoJSON column takes `intersects`
  against the box as a polygon, which works for points, lines and polygons alike; a legacy
  `location` column answers only `within_box`; a `point` mapping of two numeric columns is a
  `between` on each.
* **The window**: records ending no earlier than the day before and starting no later than
  the day after (`client.window_bounds`). A permits table holds a decade of history, and the
  corridor alone would page through all of it.

A Socrata app token (`LONGRUN_SOCRATA_APP_TOKEN`) is sent when set. It lifts the anonymous
rate limit and changes no answer, so it is neither a cache key nor an `adapter.key`: a row
whose token is missing must still be asked, which a declared key would forbid.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable
    from datetime import date

    from longrun.adapters.base import AdapterContext
    from longrun.adapters.portals.catalog import CatalogEntry
    from longrun.adapters.portals.client import Box


def headers() -> dict[str, str] | None:
    from longrun.adapters.keys import SOCRATA

    token = SOCRATA.value()
    return {"X-App-Token": token} if token else None


def where(entry: CatalogEntry, bbox: Box | None, day: date) -> str | None:
    """The `$where` clause for one corridor and day."""
    from longrun.adapters.portals.client import window_bounds

    parts: list[str] = []
    if entry.where:
        parts.append(f"({entry.where})")
    if bbox is not None:
        parts.append(_spatial(entry, bbox))
    if entry.window_filter:
        ends_after, starts_before = window_bounds(day)
        if entry.end is not None:
            column = entry.end.field
            parts.append(f"({column} IS NULL OR {column} >= '{ends_after.isoformat()}T00:00:00')")
        if entry.start is not None:
            column = entry.start.field
            parts.append(
                f"({column} IS NULL OR {column} <= '{starts_before.isoformat()}T23:59:59')"
            )
    return " AND ".join(parts) or None


def _spatial(entry: CatalogEntry, bbox: Box) -> str:
    west, south, east, north = bbox
    mapping = entry.geometry
    if mapping.kind == "point":
        return (
            f"{mapping.lat} BETWEEN {south} AND {north} AND {mapping.lon} BETWEEN {west} AND {east}"
        )
    column = mapping.field  # type: ignore[union-attr]
    if entry.spatial == "within_box":
        return f"within_box({column}, {north}, {west}, {south}, {east})"
    ring = f"{west} {south}, {east} {south}, {east} {north}, {west} {north}, {west} {south}"
    return f"intersects({column}, 'POLYGON(({ring}))')"


def produce(
    entry: CatalogEntry, bbox: Box | None, day: date, ctx: AdapterContext
) -> Callable[[], Any]:
    """The producer for one query: every page of rows, or an error."""
    from longrun.adapters.http import get_json
    from longrun.adapters.portals.client import MAX_PAGES, PAGE_SIZE

    clause = where(entry, bbox, day)

    def run() -> Any:
        rows: list[Any] = []
        for _ in range(MAX_PAGES):
            ctx.budget.spend_api_call()
            params: dict[str, Any] = {"$limit": PAGE_SIZE, "$offset": len(rows), "$order": ":id"}
            if clause:
                params["$where"] = clause
            page = get_json(entry.url, ctx, params=params, headers=headers())
            if not isinstance(page, list):
                raise RuntimeError(f"the dataset answered {type(page).__name__}, not rows")
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                return {"rows": rows}
        raise RuntimeError(
            f"more than {MAX_PAGES * PAGE_SIZE:,} records in this corridor; refused rather "
            f"than truncated"
        )

    return run


def rows(payload: Any) -> list[dict[str, Any]]:
    """A cached payload's rows. Socrata's are already one flat dict each."""
    found = payload.get("rows") if isinstance(payload, dict) else None
    return [r for r in found if isinstance(r, dict)] if isinstance(found, list) else []


def metadata(entry: CatalogEntry, ctx: AdapterContext) -> Callable[[], Any]:
    """The producer for the weekly metadata read: when the rows last changed, and the
    licence the dataset declares, verbatim."""
    from longrun.adapters.http import get_json

    def run() -> Any:
        ctx.budget.spend_api_call()
        view = get_json(f"https://{entry.domain}/api/views/{entry.dataset}.json", ctx)
        if not isinstance(view, dict):
            raise RuntimeError("the dataset's metadata is not an object")
        return {"updated": updated(view), "licence": view.get("licenseId")}

    return run


def updated(view: dict[str, Any]) -> str | None:
    """When a view's rows last changed, as an ISO date. `rowsUpdatedAt` is when the data
    changed; `viewLastModified` moves when someone edits a description, which says nothing
    about whether the closures are current."""
    stamp = view.get("rowsUpdatedAt")
    if not isinstance(stamp, (int, float)):
        return None
    return datetime.fromtimestamp(float(stamp), UTC).date().isoformat()


__all__ = ["headers", "metadata", "produce", "rows", "updated", "where"]
