"""The 511 platforms IBI Group builds, read at tier 2 (scope 7.10; M20, ADR 0059).

**One vendor, many states.** 511GA, 511 Alaska, NVRoads, CTroads, 511 Idaho, AZ511, 511LA
and 511PA all serve the same "GET Events" endpoint (`/api/v2/get/event`), with the same
documented fields, and all of them need a free developer key. So this is one parser and one
adapter shape; a state is one instance of it, adopted in the commit that brings its key
(`state511.AWAITING_KEY` lists who is waiting).

**Tier 2, by source type.** Scope 7.10 lists "2 511 API". These are a DOT's own traveler
events, structured, with a window - but not WZDx: no `vehicle_impact`, no spec, a vendor's
shape. `MIN_HARD_FLAG_TIER` is 2, so a tier-2 record *may* fail a route. What decides
whether one can is its geometry:

* **Drawn** (`EncodedPolyline`): the road it covers, as the publisher drew it. Confidence
  0.9, above `closures.HARD_FLAG_CONFIDENCE`.
* **Undrawn** (the two published points, or one): a chord across whatever lies between
  them, or a point that cannot run along anything. Confidence 0.75, below the line. ADR
  0013's gate 2 asks whether a closure runs *along* the route, and a straight line between
  two ends of a curving road answers that wrongly in both directions.

ADR 0059 records the rule: **tier 2 fails a route only along a drawn line.**

**What the documentation leaves open, decided here:**

* `IsFullClosure` true is `road-closure`, which `closures.blocks_pedestrians` reads as shut.
  Anything else is `some-lanes-closed`: a lane is not a footway.
* Times are Unix seconds, so UTC by definition, and become naive UTC (ADR 0046).
* An incident with no `PlannedEndDate` is dropped. A crash nobody has scheduled the end of
  is over within the hour or it is not, and either way a plan for a run is no place for it.
  A roadwork with no end is kept, open-ended, and `is_standing_condition` keeps it soft.
* A recurring event is held below the line too. Its window is the whole campaign - three
  weeks for a closure that holds 9 PM to 5 AM - and `RecurrenceSchedules` is prose this
  parser does not read; a daytime run cannot be failed by a night's closure it never meets.

**Never raises on a malformed record**, for the reason `wzdx/feed.py` gives: one bad record
must not cost a route every closure in the state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from longrun.adapters.base import AdapterResult

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext
    from longrun.adapters.keys import ApiKey
    from longrun.core.models.coverage import Tier
    from longrun.core.models.features import Feature

#: Scope 7.10: "2 511 API".
IBI_TIER: Tier = 2

#: An event drawn as an encoded polyline - the road it covers. Clears
#: `closures.HARD_FLAG_CONFIDENCE` (0.8), and stays below WZDx's 0.95: a vendor's event
#: list is a structured record, not a structured *standard*.
DRAWN_CONFIDENCE = 0.9

#: An event placed by its end points alone, or one whose schedule this parser cannot read.
#: Below `HARD_FLAG_CONFIDENCE`, so it is reported and never fails a route (ADR 0059).
UNDRAWN_CONFIDENCE = 0.75

#: The incident type the documentation names. Dropped when it has no planned end.
INCIDENT = "accidentsAndIncidents"


def _instant(value: Any) -> datetime | None:
    """Unix seconds as a naive UTC instant (ADR 0046), or `None`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, UTC).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _point(lat: Any, lon: Any) -> tuple[float, float] | None:
    """A published (lat, lon) as GeoJSON's (lon, lat), or `None` when it is not one."""
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return (float(lon), float(lat))


def event_geometry(event: dict[str, Any]) -> tuple[dict[str, Any], bool] | None:
    """`(GeoJSON, drawn)` for one event, or `None` when it cannot be placed at all.

    The polyline first, because it is the road; a polyline that will not decode falls back
    to the points rather than costing the record, and says it is undrawn.
    """
    from longrun.adapters.state511.polyline import decode

    encoded = event.get("EncodedPolyline")
    if isinstance(encoded, str) and encoded.strip():
        try:
            line = decode(encoded.strip())
        except ValueError:
            line = []
        if len(line) >= 2:
            return {"type": "LineString", "coordinates": [list(p) for p in line]}, True
    first = _point(event.get("Latitude"), event.get("Longitude"))
    second = _point(event.get("LatitudeSecondary"), event.get("LongitudeSecondary"))
    if first is not None and second is not None and first != second:
        return {"type": "LineString", "coordinates": [list(first), list(second)]}, False
    if first is not None or second is not None:
        only = first or second
        assert only is not None
        return {"type": "Point", "coordinates": list(only)}, False
    return None


def _category(event: dict[str, Any]) -> str:
    return "road-closure" if event.get("IsFullClosure") is True else "some-lanes-closed"


def _detail(event: dict[str, Any]) -> str | None:
    parts = [
        str(event.get(key)).strip()
        for key in ("RoadwayName", "Description")
        if isinstance(event.get(key), str) and str(event.get(key)).strip()
    ]
    return " - ".join(parts) if parts else None


def _recurring(event: dict[str, Any]) -> bool:
    return any(
        isinstance(event.get(key), str) and str(event.get(key)).strip()
        for key in ("Recurrence", "RecurrenceSchedules")
    )


def parse_events(
    payload: Any, *, source_url: str | None = None, jurisdiction: str | None = None
) -> list[Feature]:
    """Every placeable event in an IBI event list, as closures `Feature`s. Never raises."""
    from longrun.core.models.features import Feature

    if not isinstance(payload, list):
        return []
    out: list[Feature] = []
    for event in payload:
        if not isinstance(event, dict):
            continue
        end = _instant(event.get("PlannedEndDate"))
        if event.get("EventType") == INCIDENT and end is None:
            continue
        placed = event_geometry(event)
        if placed is None:
            continue
        geometry, drawn = placed
        confidence = DRAWN_CONFIDENCE if drawn and not _recurring(event) else UNDRAWN_CONFIDENCE
        try:
            out.append(
                Feature(
                    kind="closures",
                    category=_category(event),
                    geometry=geometry,
                    tier=IBI_TIER,
                    confidence=confidence,
                    jurisdiction=jurisdiction,
                    start=_instant(event.get("StartDate")),
                    end=end,
                    source_url=source_url,
                    detail=_detail(event),
                    ref=str(event["ID"]) if event.get("ID") not in (None, "") else None,
                )
            )
        except ValueError:
            # An end before its start; one bad record must not cost the state.
            continue
    return out


class IbiClosures:
    """One IBI platform's events, as a state's tier-2 closures adapter.

    A class rather than a module per state, because the states differ in four strings and a
    key. Adopting one is constructing it in its own module, with its key in
    `adapters/keys.py` and an entry point - not registered here, so a platform nobody holds
    a key for never counts as a claimant.
    """

    kind = "closures"
    tier: Tier = IBI_TIER
    scope = "feed"
    vintage: str | None = None

    def __init__(
        self, name: str, base_url: str, jurisdictions: tuple[str, ...], source: str, key: ApiKey
    ) -> None:
        self.name = name
        self.base_url = base_url
        self.jurisdictions = jurisdictions
        self.source = source
        self.key = key

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        from longrun.adapters.state511.client import EVENTS_PATH, fetch_events

        found = fetch_events(self.base_url, self.name, day, ctx, self.key)
        if isinstance(found, AdapterResult):
            return found
        url = self.base_url.rstrip("/") + EVENTS_PATH
        return AdapterResult(
            features=parse_events(found, source_url=url), vintage="ibi-v2", source_url=url
        )


__all__ = [
    "DRAWN_CONFIDENCE",
    "IBI_TIER",
    "INCIDENT",
    "UNDRAWN_CONFIDENCE",
    "IbiClosures",
    "event_geometry",
    "parse_events",
]
