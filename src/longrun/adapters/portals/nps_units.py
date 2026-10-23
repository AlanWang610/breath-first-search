"""Which National Park Service units a corridor meets, by boundary box (M18, ADR 0054).

PAD-US tells a plan that a route is on National Park Service land - `padus:NPS` - and not
which park. The NPS API answers per park code: alerts, hours, everything. Until M18 the
adapter asked for no park at all and got the first fifty alerts in the country, every one
of them placed on any NPS route as though it were about that route's park. The codes have
to come from somewhere, and name-matching PAD-US's `Unit_Nm` against the API's `fullName`
is the guess this module exists not to make.

**The source is the NPS's own boundary layer**: the Land Resources Division's
`nps_boundary`, the authoritative boundary for every unit, keyed by the `UNIT_CODE` the API
uses. A work of the US federal government, so public domain, and committed here as
`nps_units.json`; `longrun nps-units` rebuilds it from a download of that layer.

**Boxes, not polygons.** Choosing which parks to *ask* needs only a box test, and a park
whose box meets the corridor but whose land does not costs one code in a request, not a
wrong flag - its alerts still have to be placed on the route. A polygon file would be tens
of megabytes to answer the same question. **One box per polygon part**, overlapping parts
merged, because a unit like the Appalachian Trail or the Blue Ridge Parkway is a long thin
line whose one box covers half a state. Rounded **outward** to three decimals (about 100 m),
so a box never shrinks inside the land it stands for.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

#: The committed file.
UNITS = Path(__file__).with_name("nps_units.json")

SOURCE = "NPS Land Resources Division, nps_boundary"
SOURCE_URL = (
    "https://services1.arcgis.com/fBc8EJBxQRMcHlei/arcgis/rest/services/"
    "NPS_Land_Resources_Division_Boundary_and_Tract_Data_Service/FeatureServer/2"
)
LICENCE = "public domain (a work of the US federal government, 17 U.S.C. 105)"

#: Decimal places a box edge is rounded outward to - about 100 m of latitude.
PLACES = 3

#: Boundary units the NPS API publishes under another code, mapped to that code. Read
#: 2026-10-23 by listing all 475 `/parks` and matching the 17 boundary codes it does not
#: know by name: a combined unit (Sequoia and Kings Canyon are `seki`, the North Cascades
#: complex is `noca`), a unit published under the park that administers it (Fort Caroline
#: under Timucuan, the Rockefeller Parkway under Grand Teton, Lower St. Croix under the
#: St. Croix riverway), or a plain difference of code. Asked under the boundary's code,
#: each answered nothing - which reads as "no alerts", not as "asked the wrong question".
API_ALIASES: dict[str, str] = {
    "crmp": "crmo",  # Craters of the Moon National Preserve -> National Monument & Preserve
    "dela": "dewa",  # Middle Delaware National Scenic River -> Delaware Water Gap NRA
    "foca": "timu",  # Fort Caroline National Memorial -> Timucuan Preserve
    "jefm": "thje",  # Thomas Jefferson Memorial
    "jodr": "grte",  # John D. Rockefeller, Jr., Memorial Parkway -> Grand Teton
    "kica": "seki",  # Kings Canyon -> Sequoia & Kings Canyon
    "lach": "noca",  # Lake Chelan NRA -> North Cascades
    "losa": "sacn",  # Lower Saint Croix -> Saint Croix National Scenic Riverway
    "mabe": "mamc",  # Mary McLeod Bethune Council House
    "mall": "nama",  # National Mall -> National Mall and Memorial Parks
    "obri": "obed",  # Obed Wild and Scenic River
    "perl": "valr",  # Pearl Harbor National Memorial
    "rola": "noca",  # Ross Lake NRA -> North Cascades
    "sequ": "seki",  # Sequoia -> Sequoia & Kings Canyon
}

#: Boundary units the API publishes nothing for under any code: National Capital Parks (a
#: DC-MD administrative grouping), Hohokam Pima (closed to the public), Ronald Reagan
#: Boyhood Home (an affiliated site). Not asked - there is nothing to ask.
UNPUBLISHED = frozenset({"naca", "pima", "rrbh"})


def api_codes(codes: list[str]) -> list[str]:
    """Boundary codes as the API's park codes, deduplicated and sorted."""
    return sorted({API_ALIASES.get(code, code) for code in codes if code not in UNPUBLISHED})


Box = tuple[float, float, float, float]


def build_units(collection: dict[str, Any], *, read_on: str) -> dict[str, Any]:
    """The committed structure, from a GeoJSON FeatureCollection of `nps_boundary`.

    Deterministic: the same collection and `read_on` give the same dict, and `write_units`
    the same bytes, so a rebuild from an unchanged download is no diff at all.
    """
    boxes: dict[str, list[Box]] = {}
    for feature in collection.get("features", []):
        code = str((feature.get("properties") or {}).get("UNIT_CODE") or "").strip().lower()
        geometry = feature.get("geometry")
        if not code or not geometry:
            continue
        for ring in _outer_rings(geometry):
            box = _box(ring)
            if box is not None:
                boxes.setdefault(code, []).append(box)
    return {
        "source": SOURCE,
        "source_url": SOURCE_URL,
        "licence": LICENCE,
        "read_on": read_on,
        "units": {code: [list(b) for b in _merged(found)] for code, found in sorted(boxes.items())},
    }


def write_units(units: dict[str, Any], path: Path) -> None:
    """Sorted keys, no whitespace: a rebuild that changes nothing changes no byte."""
    path.write_text(json.dumps(units, sort_keys=True, separators=(",", ":")) + "\n", "utf-8")


def _outer_rings(geometry: dict[str, Any]) -> list[list[Any]]:
    """Each polygon part's outer ring. A hole cannot widen a box, so holes are skipped."""
    coordinates = geometry.get("coordinates") or []
    if geometry.get("type") == "Polygon":
        return [coordinates[0]] if coordinates else []
    if geometry.get("type") == "MultiPolygon":
        return [part[0] for part in coordinates if part]
    return []


def _box(ring: list[Any]) -> Box | None:
    xs = [float(point[0]) for point in ring]
    ys = [float(point[1]) for point in ring]
    if not xs:
        return None
    scale = 10**PLACES
    return (
        math.floor(min(xs) * scale) / scale,
        math.floor(min(ys) * scale) / scale,
        math.ceil(max(xs) * scale) / scale,
        math.ceil(max(ys) * scale) / scale,
    )


def _meets(one: Box, other: Box) -> bool:
    return one[0] <= other[2] and other[0] <= one[2] and one[1] <= other[3] and other[1] <= one[3]


def _merged(boxes: list[Box]) -> list[Box]:
    """Overlapping boxes as one, repeated until none overlap; sorted."""
    pending = sorted(set(boxes))
    changed = True
    while changed:
        changed = False
        out: list[Box] = []
        for box in pending:
            for i, held in enumerate(out):
                if _meets(held, box):
                    out[i] = (
                        min(held[0], box[0]),
                        min(held[1], box[1]),
                        max(held[2], box[2]),
                        max(held[3], box[3]),
                    )
                    changed = True
                    break
            else:
                out.append(box)
        pending = sorted(set(out))
    return pending


@lru_cache(maxsize=4)
def load_units(path: Path = UNITS) -> dict[str, tuple[Box, ...]]:
    """Park code -> boxes, from the committed file."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        code: tuple((float(b[0]), float(b[1]), float(b[2]), float(b[3])) for b in boxes)
        for code, boxes in raw["units"].items()
    }


def units_meeting(bounds: tuple[float, float, float, float], path: Path = UNITS) -> list[str]:
    """The park codes whose boxes meet `(west, south, east, north)`, sorted."""
    west, south, east, north = (float(v) for v in bounds)
    query = (west, south, east, north)
    return sorted(
        code for code, boxes in load_units(path).items() if any(_meets(b, query) for b in boxes)
    )


__all__ = [
    "API_ALIASES",
    "LICENCE",
    "SOURCE",
    "SOURCE_URL",
    "UNITS",
    "UNPUBLISHED",
    "api_codes",
    "build_units",
    "load_units",
    "units_meeting",
    "write_units",
]
