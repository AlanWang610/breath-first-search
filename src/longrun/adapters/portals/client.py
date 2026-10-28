"""Asking a portal dataset, through the cache, for one corridor and one day (M21).

Shared by every catalog row, so the rules `wzdx/client.py` set are kept in one place:

*Everything goes through the cache*, and the cache holds the platform's rows as returned -
not features - because the mapping in `catalog.yaml` will be corrected long after a cassette
was recorded, and a cassette of parsed output would have to be re-recorded for every fix.

*The key is the dataset, the snapped box and the day* (`portal_args`). No credential: a
Socrata app token raises a rate limit and changes no answer, and a key in a cache key would
put a secret in a committed fixture's name. The box is snapped outward to `SNAP_DEG`, so two
routes through the same few blocks share one recorded answer instead of missing each other
by a metre.

*Refused, never truncated.* A query that would need more than `MAX_PAGES` pages is a
reason: an answer missing its last page reads exactly like a corridor with fewer closures.

*Stale is a reason.* Once a week the dataset's own metadata is read (`adapter.<name>.meta`),
and a dataset not updated within its row's `max_staleness_days` of the plan's day answers
nothing. A closure table that stopped being maintained still returns rows - old ones, and
none of this month's - and "checked, nothing here" from it would be the false negative
`massdot.py` names.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from types import ModuleType

    from longrun.adapters.base import AdapterContext, AdapterResult
    from longrun.adapters.portals.catalog import CatalogEntry

#: Records asked for per request. Both platforms serve at least this many in one page.
PAGE_SIZE = 1000

#: Pages read before a query is refused: ten thousand records in one corridor's box is a
#: permits table nobody filtered, not a street's worth of closures.
MAX_PAGES = 10

#: The grid a corridor's box is snapped outward to, in degrees: about a kilometre.
SNAP_DEG = 0.01

Box = tuple[float, float, float, float]


def snap_bbox(bounds: Any, step: float = SNAP_DEG) -> Box | None:
    """A (west, south, east, north) box snapped outward to the grid, or `None` if there is
    no finite box to snap. Outward, so the snapped box always contains the corridor's."""
    try:
        west, south, east, north = (float(v) for v in bounds)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (west, south, east, north)) or west > east:
        return None
    scale = round(1 / step)
    return (
        math.floor(west * scale) / scale,
        math.floor(south * scale) / scale,
        math.ceil(east * scale) / scale,
        math.ceil(north * scale) / scale,
    )


def portal_args(adapter: str, bbox: Box | None, day: date | str) -> dict[str, Any]:
    """The records' cache key, as a named function so a hash-stability test can pin it."""
    return {
        "adapter": adapter,
        "bbox": None if bbox is None else [round(v, 4) for v in bbox],
        "day": str(day),
    }


def meta_args(adapter: str, week: date | str) -> dict[str, Any]:
    """The weekly metadata read's cache key."""
    return {"adapter": adapter, "meta": True, "week": str(week)}


def week_of(day: date) -> date:
    """The Monday of a day's ISO week - the metadata's cache day."""
    return day - timedelta(days=day.weekday())


def platform(name: str) -> ModuleType:
    """The module that speaks one platform's query language."""
    if name == "socrata":
        from longrun.adapters.portals import socrata

        return socrata
    if name == "arcgis":
        from longrun.adapters.portals import arcgis

        return arcgis
    raise ValueError(f"no client for platform {name!r}")


def stale_reason(entry: CatalogEntry, updated: date | None, day: date) -> str | None:
    """Why a dataset is too old to answer for `day`, or `None` when it is fresh enough."""
    if updated is None:
        return (
            f"{entry.name} does not say when it was last updated, so it cannot be told "
            f"from a dataset nobody maintains"
        )
    limit = entry.survey.max_staleness_days
    if (day - updated).days > limit:
        return (
            f"{entry.name} was last updated {updated.isoformat()}, more than {limit} days "
            f"before {day.isoformat()}; a stale table would answer 'nothing here'"
        )
    return None


def fetch_portal(
    entry: CatalogEntry, polygon: Any, day: date, ctx: AdapterContext
) -> AdapterResult:
    """One dataset's answer for one corridor and day, cached, mapped and never raised out of."""
    from longrun.adapters.base import AdapterResult, describe
    from longrun.adapters.portals.catalog import to_features
    from longrun.core.data.cache import fetch

    client = platform(entry.platform)
    week = week_of(day)
    try:
        meta = fetch(
            ctx.cache,
            f"adapter.{entry.name}.meta",
            meta_args(entry.name, week),
            week,
            client.metadata(entry, ctx),
        )
    except Exception as exc:  # noqa: BLE001 - a portal that is down is a reason
        return AdapterResult(
            reason=f"its metadata could not be read: {describe(exc)}", source_url=entry.page
        )
    updated = _date(meta.get("updated") if isinstance(meta, dict) else None)
    stale = stale_reason(entry, updated, day)
    if stale is not None:
        return AdapterResult(reason=stale, source_url=entry.page)

    bbox: Box | None = None
    if entry.scope == "polygon":
        bbox = snap_bbox(getattr(polygon, "bounds", None))
        if bbox is None:
            return AdapterResult(
                reason="no corridor to ask the portal about", source_url=entry.page
            )
    try:
        payload = fetch(
            ctx.cache,
            f"adapter.{entry.name}",
            portal_args(entry.name, bbox, day),
            day,
            client.produce(entry, bbox, day, ctx),
        )
    except Exception as exc:  # noqa: BLE001 - a portal that is down is a reason
        return AdapterResult(reason=describe(exc), source_url=entry.page)

    records = client.rows(payload)
    mapped = to_features(entry, records)
    reason = None
    if records and not mapped.features:
        # Every record failing is one wrong column name far more often than many bad rows,
        # and "checked, nothing here" from it would be the silent failure ADR 0039 names.
        reason = (
            f"the dataset returned {len(records)} record(s) and none could be read "
            f"({mapped.first_problem})"
        )
    return AdapterResult(
        features=mapped.features,
        reason=reason,
        vintage=f"{entry.platform}-{updated.isoformat()}" if updated else None,
        source_url=entry.page,
    )


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def window_bounds(day: date) -> tuple[date, date]:
    """The dates a server-side window filter keeps: anything ending on or after the day
    before, and starting on or before the day after. A day either side because a dataset's
    dates are local and a plan's day is the runner's - the exact cut is `Feature.active_at`'s,
    applied to every record after mapping; this one only keeps the table small."""
    return day - timedelta(days=1), day + timedelta(days=1)


__all__ = [
    "MAX_PAGES",
    "PAGE_SIZE",
    "SNAP_DEG",
    "fetch_portal",
    "meta_args",
    "platform",
    "portal_args",
    "snap_bbox",
    "stale_reason",
    "week_of",
    "window_bounds",
]
