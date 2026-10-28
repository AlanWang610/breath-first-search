"""What a portal publishes, read for a person deciding whether to adopt it (M21).

`longrun survey-portal` is the tool every row in `catalog.yaml` was adopted or refused
with. It reads, prints and stops: the dataset's name, when its rows last changed, its
licence **verbatim** and whether that licence would let a payload be committed (ADR 0061),
its columns with their types, a row count, and a stub row for the catalog with the obvious
mappings guessed. **It never writes** - the decision, and the jurisdiction, are a person's.

Two questions, on two platforms:

* *Describe one dataset*: Socrata by domain and four-by-four id; ArcGIS by layer URL.
* *Search*: Socrata's Discovery API on one domain, or ArcGIS Online's item search inside a
  box - the two catalogs a city's datasets are found in.

Not on the plan path, so not through the cache: a survey is a person asking what a portal
says today, and a recorded answer to that would be the one thing it must not be.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

TIMEOUT_S = 60.0

#: Socrata column types that hold a geometry, and the spatial clause each answers.
_SOCRATA_GEOMETRY = {
    "point": "intersects",
    "line": "intersects",
    "multiline": "intersects",
    "polygon": "intersects",
    "multipolygon": "intersects",
    "multipoint": "intersects",
    "location": "within_box",
}


def _get(url: str, params: dict[str, Any] | None = None) -> Any:
    response = httpx.get(url, params=params, timeout=TIMEOUT_S, follow_redirects=True)
    response.raise_for_status()
    return response.json()


def _date(seconds: Any, *, ms: bool = False) -> str | None:
    if not isinstance(seconds, (int, float)):
        return None
    return datetime.fromtimestamp(seconds / (1000.0 if ms else 1.0), UTC).date().isoformat()


# --- Socrata ----------------------------------------------------------------


def describe_socrata(domain: str, dataset: str) -> list[str]:
    """One Socrata dataset, as lines to print, ending in a catalog stub."""
    from longrun.adapters.portals.catalog import committable

    view = _get(f"https://{domain}/api/views/{dataset}.json")
    columns = [(c.get("fieldName"), c.get("dataTypeName")) for c in view.get("columns", [])]
    count = _get(f"https://{domain}/resource/{dataset}.json", {"$select": "count(*)"})
    rows = count[0].get("count") if isinstance(count, list) and count else None
    verdict = committable(view)
    lines = [
        f"{view.get('name')} ({domain}/{dataset})",
        f"  rows: {rows}; rows last updated {_date(view.get('rowsUpdatedAt'))}",
        f"  licenseId (verbatim): {view.get('licenseId')!r}",
        f"  licence name: {(view.get('license') or {}).get('name')!r}",
        "  a payload may be committed" if verdict is None else f"  not committable: {verdict}",
        "  columns:",
        *(f"    {name}: {kind}" for name, kind in columns),
        "",
        "  stub (nothing written; fill in jurisdiction, source and the mappings):",
        *_stub(
            platform="socrata",
            domain=domain,
            dataset=dataset,
            licence=view.get("licenseId"),
            licence_from=f"https://{domain}/api/views/{dataset}.json",
            rows=rows,
            updated=_date(view.get("rowsUpdatedAt")),
            geometry=next((n for n, k in columns if k in _SOCRATA_GEOMETRY), None),
            spatial=next(
                (_SOCRATA_GEOMETRY[k] for _, k in columns if k in _SOCRATA_GEOMETRY), None
            ),
            dates=[n for n, k in columns if k in ("calendar_date", "date")],
        ),
    ]
    return lines


def search_socrata(domain: str, query: str) -> list[str]:
    """Datasets on one Socrata domain matching a query, by the Discovery API."""
    found = _get(
        "https://api.us.socrata.com/api/catalog/v1",
        {"q": query, "domains": domain, "search_context": domain, "limit": 50},
    )
    lines = [f"Socrata {domain}, {query!r}:"]
    for item in found.get("results", []):
        resource, meta = item.get("resource", {}), item.get("metadata", {})
        updated = str(resource.get("data_updated_at") or resource.get("updatedAt") or "")[:10]
        lines.append(
            f"  {resource.get('id')} [{resource.get('type')}] {resource.get('name')!r} "
            f"updated {updated or '?'}; licence {meta.get('license')!r}"
        )
    return lines


# --- ArcGIS -----------------------------------------------------------------


def describe_arcgis(url: str) -> list[str]:
    """One ArcGIS layer (or a service's list of layers), as lines to print."""
    layer = _get(url, {"f": "json"})
    if isinstance(layer.get("error"), dict):
        error = layer["error"]
        return [f"{url}: ArcGIS error {error.get('code')}: {error.get('message')}"]
    if "fields" not in layer:
        return [
            f"{url} is a service; its layers:",
            *(
                f"  {url.rstrip('/')}/{entry.get('id')}  {entry.get('name')} "
                f"({entry.get('geometryType')})"
                for entry in layer.get("layers", [])
            ),
        ]
    info = layer.get("editingInfo") or {}
    updated = _date(info.get("dataLastEditDate") or info.get("lastEditDate"), ms=True)
    count = _get(f"{url}/query", {"where": "1=1", "returnCountOnly": "true", "f": "json"})
    fields = [
        (f.get("name"), str(f.get("type", "")).removeprefix("esriFieldType"))
        for f in layer.get("fields", [])
    ]
    host, _, path = url.removeprefix("https://").partition("/")
    return [
        f"{layer.get('name')} ({url})",
        f"  geometry {layer.get('geometryType')}; rows: {count.get('count')}; "
        f"last edited {updated or 'not stated (no editingInfo)'}",
        "  licence: an ArcGIS layer declares no licence id; not committable (ADR 0061)",
        "  fields:",
        *(f"    {name}: {kind}" for name, kind in fields),
        "",
        "  stub (nothing written; fill in jurisdiction, source and the mappings):",
        *_stub(
            platform="arcgis",
            domain=host,
            dataset=f"/{path}",
            licence=None,
            licence_from=f"{url}?f=json",
            rows=count.get("count"),
            updated=updated,
            geometry="geometry",
            spatial="intersects",
            dates=[n for n, k in fields if k == "Date"],
        ),
    ]


def search_arcgis(query: str, bbox: str) -> list[str]:
    """Public feature and map services whose extent meets a box, by ArcGIS Online search."""
    found = _get(
        "https://www.arcgis.com/sharing/rest/search",
        {
            "q": f'{query} AND (type:"Feature Service" OR type:"Map Service")',
            "bbox": bbox,
            "num": 100,
            "f": "json",
        },
    )
    lines = [f"ArcGIS Online, {query!r} within {bbox}:"]
    for item in found.get("results", []):
        lines.append(
            f"  {item.get('title')!r} by {item.get('owner')}, modified "
            f"{_date(item.get('modified'), ms=True)}: {item.get('url')}"
        )
    return lines


# --- the stub ---------------------------------------------------------------


def _stub(
    *,
    platform: str,
    domain: str,
    dataset: str,
    licence: Any,
    licence_from: str,
    rows: Any,
    updated: str | None,
    geometry: str | None,
    spatial: str | None,
    dates: list[str],
) -> list[str]:
    today = datetime.now(UTC).date().isoformat()
    start = next((d for d in dates if "start" in d.lower() or "begin" in d.lower()), None)
    end = next((d for d in dates if "end" in d.lower() or "expir" in d.lower()), None)
    fmt = "epoch_ms" if platform == "arcgis" else "date"
    body = [
        f"- name: portal.{platform}.<slug>",
        "  jurisdiction: tiger:place:<GEOID>",
        "  kind: closures",
        f"  platform: {platform}",
        f"  domain: {domain}",
        f"  dataset: {dataset}",
        "  source: portal_<publisher>",
        "  zone: America/<Zone>",
        f"  geometry: {{kind: field, field: {geometry or '<column>'}}}",
        "  category: {kind: const, value: road-closure}",
        f"  start: {{kind: field, field: {start or '<column>'}, format: {fmt}}}",
        f"  end: {{kind: field, field: {end or '<column>'}, format: {fmt}}}",
    ]
    if spatial == "within_box":
        body.append("  spatial: within_box")
    body += [
        f"  licence: {{id: {'null' if licence is None else licence}, read_on: {today}, "
        f"from: {licence_from}}}",
        f"  survey: {{read_on: {today}, rows: {rows or 0}, updated: {updated or '<date>'}, "
        "max_staleness_days: 14}",
    ]
    return [f"    {line}" for line in body]


__all__ = ["describe_arcgis", "describe_socrata", "search_arcgis", "search_socrata"]
