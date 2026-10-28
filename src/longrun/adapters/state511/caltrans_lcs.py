"""California's lane closures, from Caltrans' own Lane Closure System (M20, ADR 0059).

**California had no statewide closure source.** It is the most populous state, and its only
adapter was 511 SF Bay - nine counties, behind a key. The USDOT WZDx registry, read on
2026-10-28, has exactly one California row, and it is that feed (MTC's `mtc`). Caltrans
publishes no WZDx.

What Caltrans does publish is the **Lane Closure System**, on its Commercial Wholesale Web
Portal (CWWP2): "every five minutes LCS reports all approved closures planned for the next 7
days, plus all current lane, ramp, and road closures". Keyless, one JSON file per district,
documented field by field (https://cwwp2.dot.ca.gov/lane-closure.html). Tier 2: a DOT's own
structured feed that is not WZDx (scope 7.10).

**Measured on 2026-10-28**, all twelve district files:

* 12,574 records, 0.2 MB (District 9) to 16.5 MB (District 7, Los Angeles, 19 s to read).
  Under `http.MAX_FEED_BYTES`, and the reason only the districts a corridor's box meets are
  fetched: a route in San Francisco reads District 4's 4.3 MB, never Los Angeles'.
* `typeOfClosure`: Full 5,388, Lane 5,380, One-Way Traffic 1,182, Alternating Lanes 423,
  Moving 192, Traffic Break 9. The documentation lists four; the file has six.
* 1,161 records are cancelled (`code1022`) and are dropped; 825 have been picked up
  (`code1098`), and end when they were.
* 4,480 records have the same begin and end point.
* Every one of 25,107 dated times agreed with its own epoch through America/Los_Angeles.
* "Standard" closures - 12,291 of them - are one record per occurrence, never longer than a
  day, so a nightly closure's window is its night and not the campaign.

**Undrawn, so below the hard-flag line.** A record is placed by its begin and end points:
a chord, or a point when they coincide. Confidence 0.75, under
`closures.HARD_FLAG_CONFIDENCE`. A closure from Bodega Bay to Jenner as a straight line
crosses the headland, not Highway 1, and ADR 0013's "does it run along the route?" cannot
be asked of it (ADR 0059: tier 2 fails a route only along a drawn line).

**No cassette.** The payload carries no licence. Caltrans' conditions of use say "in
general, information presented on this website, unless otherwise indicated, is considered
in the public domain", which is a statement about a website rather than a grant in the
payload - and ADR 0038 accepts only the second. So the parser is tested against a shape
fixture written from the field description (ADR 0055).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from longrun.adapters.base import AdapterResult, describe, local_to_utc

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext
    from longrun.core.models.coverage import Tier
    from longrun.core.models.features import Feature

#: One file per district; `{n}` unpadded in the directory, padded in the name.
URL_TEMPLATE = "https://cwwp2.dot.ca.gov/data/d{n}/lcs/lcsStatusD{n:02d}.json"

#: Where the documentation lives - what a reason or a sheet points a reader at.
DOCS_URL = "https://cwwp2.dot.ca.gov/lane-closure.html"

#: The zone LCS writes its dates and times in ("24-hour time in PST or PDT").
ZONE = "America/Los_Angeles"

#: Each district's box, (west, south, east, north), from Caltrans' own district boundaries
#: (`CHboundary/District_Tiger_Lines`, read 2026-10-28) and rounded outward to 0.1 degree.
#: Not from counties: Kern is split between Districts 6 and 9, and every District 9
#: closure south of 35.7 degrees is in Kern. Every record in the twelve files on
#: 2026-10-28 lay inside its own district's box.
DISTRICT_BOXES: dict[int, tuple[float, float, float, float]] = {
    1: (-124.5, 38.6, -122.3, 42.1),
    2: (-123.8, 39.5, -119.9, 42.1),
    3: (-123.0, 38.0, -119.8, 40.2),
    4: (-123.7, 36.8, -121.2, 38.9),
    5: (-122.4, 34.3, -119.4, 37.3),
    6: (-121.0, 34.7, -117.9, 37.8),
    7: (-119.6, 33.6, -117.6, 35.0),
    8: (-117.9, 33.4, -114.1, 35.9),
    9: (-119.7, 34.8, -115.6, 38.8),
    10: (-121.6, 36.7, -119.1, 39.0),
    11: (-117.7, 32.5, -114.4, 33.6),
    12: (-118.2, 33.3, -117.4, 34.0),
}

#: Scope 7.10: a DOT's own feed that is not WZDx.
CALTRANS_TIER: Tier = 2

#: Placed by two points, never drawn. Below `HARD_FLAG_CONFIDENCE` (ADR 0059).
CALTRANS_CONFIDENCE = 0.75

#: LCS's sentinel for an indefinite end: 2999-12-31 23:59 Pacific.
INDEFINITE_EPOCH = 32503708740

#: `typeOfClosure` values with a meaning `closures.blocks_pedestrians` knows. Anything else
#: passes through as its own slug, so a seventh value reaches the scorer as itself.
CLOSURE_TYPES = {"full": "road-closure", "lane": "some-lanes-closed"}

#: A `lanesClosed` entry that is the footway itself (the Lane Type Chart's "Sidewalk").
SIDEWALK = "sidewalk"

NO_DISTRICT_REASON = "the corridor meets no Caltrans district; there is no file to read"


def lcs_args(adapter: str, district: int, day: date | str) -> dict[str, Any]:
    """The cache key, as a named function so a hash-stability test can pin it. Per district,
    so two routes in one district share its file."""
    return {"adapter": adapter, "district": district, "day": str(day)}


def districts_meeting(bounds: tuple[float, float, float, float]) -> list[int]:
    """The districts whose box meets a corridor's (west, south, east, north)."""
    west, south, east, north = bounds
    return [
        district
        for district, (w, s, e, n) in DISTRICT_BOXES.items()
        if w <= east and e >= west and s <= north and n >= south
    ]


def _true(value: Any) -> bool:
    return str(value).strip().lower() == "true"


def _epoch(value: Any) -> datetime | None:
    try:
        seconds = int(str(value).strip())
    except ValueError:
        return None
    if seconds >= INDEFINITE_EPOCH:
        return None
    return datetime.fromtimestamp(seconds, UTC).replace(tzinfo=None)


def _stamp(stamps: dict[str, Any], prefix: str) -> datetime | None:
    """`<prefix>Epoch` as naive UTC, else `<prefix>Date` and `<prefix>Time` read as Pacific.

    The epoch first: it is unambiguous inside the hour a fall-back repeats, which a wall
    clock is not. The two agreed on every one of 25,107 times measured.
    """
    instant = _epoch(stamps.get(f"{prefix}Epoch"))
    if instant is not None:
        return instant
    day, clock = stamps.get(f"{prefix}Date"), stamps.get(f"{prefix}Time")
    if not day or not clock or str(day).startswith("2999"):
        return None
    try:
        local = datetime.fromisoformat(f"{day}T{clock}")
    except ValueError:
        return None
    return local_to_utc(local, ZONE)


def _point(place: dict[str, Any], side: str) -> tuple[float, float] | None:
    try:
        lat = float(place[f"{side}Latitude"])
        lon = float(place[f"{side}Longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (32.0 <= lat <= 42.5 and -125.0 <= lon <= -114.0):
        return None  # outside California: a typo, not a closure
    return (lon, lat)


def _geometry(location: dict[str, Any]) -> dict[str, Any] | None:
    begin = _point(location.get("begin") or {}, "begin")
    end = _point(location.get("end") or {}, "end")
    if begin is not None and end is not None and begin != end:
        return {"type": "LineString", "coordinates": [list(begin), list(end)]}
    only = begin or end
    return {"type": "Point", "coordinates": list(only)} if only is not None else None


def _lanes(closure: dict[str, Any]) -> list[str]:
    parts = str(closure.get("lanesClosed") or "").split(",")
    return [part.strip() for part in parts if part.strip()]


def _category(closure: dict[str, Any]) -> str:
    if any(lane.lower() == SIDEWALK for lane in _lanes(closure)):
        return "sidewalk-closed"
    kind = str(closure.get("typeOfClosure") or "").strip().lower()
    if kind in CLOSURE_TYPES:
        return CLOSURE_TYPES[kind]
    return re.sub(r"[^a-z0-9]+", "-", kind).strip("-") or "closure-unspecified"


def _text(record: dict[str, Any], key: str) -> str:
    return str(record.get(key) or "").strip()


def _detail(location: dict[str, Any], closure: dict[str, Any]) -> str | None:
    """`SR-1 South, Taylor St to Kent Ave - Storm Damage Repairs; Lane closure; lanes
    closed: 1 of 2; Conventional Hwy`: where, then what, in the publisher's own words."""
    begin, end = location.get("begin") or {}, location.get("end") or {}
    direction = _text(location, "travelFlowDirection")
    road = " ".join(p for p in (_text(begin, "beginRoute"), direction) if p and p != "N/A")
    names = (_text(begin, "beginLocationName"), _text(end, "endLocationName"))
    where = " to ".join(dict.fromkeys(n for n in names if n))
    lanes = ", ".join(_lanes(closure))
    total = _text(closure, "totalExistingLanes")
    facts = (
        _text(closure, "typeOfWork"),
        f"{_text(closure, 'typeOfClosure')} closure" if _text(closure, "typeOfClosure") else "",
        (f"lanes closed: {lanes}" + (f" of {total}" if total.isdigit() else "")) if lanes else "",
        _text(closure, "facility"),
    )
    parts = (", ".join(p for p in (road, where) if p), "; ".join(f for f in facts if f))
    return " - ".join(p for p in parts if p) or None


def parse_lcs(payload: Any, *, source_url: str | None = None) -> list[Feature]:
    """Every live or planned closure in one district file, as tier-2 `Feature`s. Never
    raises; a record that cannot be placed or dated costs itself."""
    from longrun.core.models.features import Feature

    records = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return []
    out: list[Feature] = []
    for item in records:
        lcs = item.get("lcs") if isinstance(item, dict) else None
        if not isinstance(lcs, dict):
            continue
        closure = lcs.get("closure") or {}
        location = lcs.get("location") or {}
        if not isinstance(closure, dict) or not isinstance(location, dict):
            continue
        if _true((closure.get("code1022") or {}).get("isCode1022")):
            continue  # cancelled
        geometry = _geometry(location)
        if geometry is None:
            continue
        stamps = closure.get("closureTimestamp") or {}
        start = _stamp(stamps, "closureStart")
        end = None if _true(stamps.get("isClosureEndIndefinite")) else _stamp(stamps, "closureEnd")
        ended = closure.get("code1098") or {}
        if _true(ended.get("isCode1098")):
            # Picked up: it ended when it did, whatever was planned.
            picked_up = _stamp(ended.get("code1098Timestamp") or {}, "code1098")
            if picked_up is not None and (end is None or picked_up < end):
                end = picked_up
        try:
            out.append(
                Feature(
                    kind="closures",
                    category=_category(closure),
                    geometry=geometry,
                    tier=CALTRANS_TIER,
                    confidence=CALTRANS_CONFIDENCE,
                    start=start,
                    end=end,
                    source_url=source_url,
                    detail=_detail(location, closure),
                    ref=str(lcs.get("index") or "") or None,
                )
            )
        except ValueError:
            continue  # an end before its start
    return out


class CaltransLaneClosures:
    """Caltrans' Lane Closure System, statewide, at tier 2.

    `scope = "polygon"`: which district files are read depends on the corridor, so an
    answer for one corridor is not an answer for another. Each file is cached on its own,
    so two corridors in one district share it.
    """

    name = "state511.caltrans"
    kind = "closures"
    tier: Tier = CALTRANS_TIER
    scope = "polygon"
    jurisdictions = ("tiger:state:06",)
    source = "state511_caltrans"
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        from longrun.adapters.state511.client import cached_json

        try:
            west, south, east, north = (float(v) for v in polygon.bounds)
        except Exception:  # noqa: BLE001 - no corridor, no districts to choose
            return AdapterResult(reason=NO_DISTRICT_REASON, source_url=DOCS_URL)
        districts = districts_meeting((west, south, east, north))
        if not districts:
            return AdapterResult(reason=NO_DISTRICT_REASON, source_url=DOCS_URL)
        features: list[Feature] = []
        for district in districts:
            url = URL_TEMPLATE.format(n=district)
            try:
                payload = cached_json(url, self.name, lcs_args(self.name, district, day), day, ctx)
            except Exception as exc:  # noqa: BLE001 - a district that is down is a reason
                # The whole answer, not the districts that came back: a corridor across
                # two districts missing one reads exactly like one with fewer closures.
                return AdapterResult(reason=f"district {district}: {describe(exc)}", source_url=url)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                return AdapterResult(
                    reason=f"district {district}: the file carried no `data` list",
                    source_url=url,
                )
            features.extend(parse_lcs(payload, source_url=url))
        return AdapterResult(
            features=features,
            vintage="caltrans-lcs",
            source_url=URL_TEMPLATE.format(n=districts[0]) if len(districts) == 1 else DOCS_URL,
        )


CLOSURES = CaltransLaneClosures()

__all__ = [
    "CALTRANS_CONFIDENCE",
    "CALTRANS_TIER",
    "CLOSURES",
    "DISTRICT_BOXES",
    "NO_DISTRICT_REASON",
    "URL_TEMPLATE",
    "CaltransLaneClosures",
    "districts_meeting",
    "lcs_args",
    "parse_lcs",
]
