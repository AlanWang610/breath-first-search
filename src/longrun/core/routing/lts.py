"""Level of Traffic Stress from OSM tags (scope 7.1, 7.2, 12).

Furth's LTS methodology adapted for pedestrians: what matters walking is whether there is
a separated place to walk, how fast the adjacent traffic moves, and how many lanes of it
there are. Wasserman et al. (TRR 2019) established that OSM tags alone support a
defensible LTS analysis, which is what makes this useful before any AADT conflation.

**This is LTS *scoring*, in pure Python, and is deliberately separate from LTS *routing***
(the GraphHopper encoded value, ADR 0001). Scorers and the plan sheet use only this, so a
problem in the Java import pipeline can never block the core library.

Missing tags produce `unknown`, never `absent` (scope 12). A road with no `maxspeed` is
not a slow road; it is a road whose speed we do not know, and the result says so by
lowering `confidence` rather than by inventing a level.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, Field

#: Ways with a separated walking surface: LTS 1 whatever the adjacent traffic does.
SEPARATED_HIGHWAYS = frozenset(
    {"footway", "path", "pedestrian", "steps", "track", "living_street", "cycleway"}
)

#: Ordered by how much traffic stress the class implies for someone on foot.
STRESS_BY_HIGHWAY: dict[str, int] = {
    "residential": 1,
    "unclassified": 2,
    "service": 2,
    "tertiary": 2,
    "tertiary_link": 2,
    "secondary": 3,
    "secondary_link": 3,
    "primary": 4,
    "primary_link": 4,
    "trunk": 4,
    "trunk_link": 4,
    "motorway": 4,
    "motorway_link": 4,
    # `road` means "class unsurveyed", common in thin-data regions (scope 11 region 3).
    # Treated as a middling street rather than as an unknown tag, so it does not take the
    # full unknown-class confidence penalty on top of an already vague answer.
    "road": 2,
    "busway": 3,
}

#: Above this posted speed, an unseparated way is high stress regardless of class.
HIGH_SPEED_KPH = 60.0
MODERATE_SPEED_KPH = 40.0

#: Confidence penalties. A level derived without knowing the speed or whether there is a
#: sidewalk is a weaker claim than one derived with both, and the plan sheet says so.
MISSING_SPEED_PENALTY = 0.2
MISSING_SIDEWALK_PENALTY = 0.25
UNKNOWN_HIGHWAY_PENALTY = 0.5

#: Which revision of the rules below produced a level. Bump it whenever a change here would
#: give an existing way a different level or a different confidence.
#:
#: It exists because M9 writes these levels into a routing graph (`osm_lts.way_lts`, then a
#: synthetic `lts` tag, then a GraphHopper encoded value — ADR 0001). A graph is built once
#: and served for a week, so "the router steered on rules this process no longer holds" is a
#: state the system can be in, and nothing else can detect it: the extract date pins the
#: *input* and says nothing about the rules applied to it. The composite vintage
#: `<extract>+lts<version>` is what makes a stale graph visible, and it is the graph identity
#: the route cache keys on.
LTS_VERSION = 1

#: Columns an HPMS conflation may have written onto the ways frame (scope 5, 7.2). AADT
#: is a confidence-raiser for `lts_from_tags`, never a requirement.
AADT_COLUMNS = ("aadt", "AADT", "aadt_veh_day")

#: The column a region build's speed-survey merge puts on a way (M18, ADR 0053): the
#: 85th-percentile operating speed a survey measured on it, in km/h, and where it came from.
#: One name in every place a way's tags are read - the plan-time `ways` layer, so every
#: frozen fixture, and the build's own scoring pass - which is what keeps the router's LTS
#: and the scorer's reading the speed from the same place (ADR 0025).
SURVEY_SPEED_COLUMN = "survey_speed_85_kph"
SURVEY_SOURCE_COLUMN = "survey_speed_source"


class LTSResult(BaseModel):
    """A level, how confident we are in it, and why it came out that way."""

    level: int = Field(ge=1, le=4)
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)

    @property
    def is_separated(self) -> bool:
        return "separated_path" in self.reasons


def parse_maxspeed(value: Any) -> float | None:
    """Posted speed in km/h, or None when it cannot be read.

    OSM carries `50`, `30 mph`, `RU:urban` and worse. Anything not confidently numeric
    returns None so the caller lowers confidence rather than guessing.
    """
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip().lower()
    if not text:
        return None
    if text.endswith("mph"):
        number = text[:-3].strip()
        try:
            return float(number) * 1.609344
        except ValueError:
            return None
    try:
        return float(text.split()[0])
    except (ValueError, IndexError):
        return None


def parse_lanes(value: Any) -> int | None:
    try:
        return int(str(value).split(";")[0])
    except (ValueError, TypeError, AttributeError):
        return None


def has_sidewalk(tags: dict[str, Any]) -> bool | None:
    """True, False, or None for unknown (scope 12).

    The three-way answer is the point. Most of the US is untagged for sidewalks, and
    treating absent tags as "no sidewalk" would flag whole cities as high stress.
    """
    for key in ("sidewalk", "sidewalk:both"):
        value = tags.get(key)
        if value is None:
            continue
        text = str(value).lower()
        if text in {"no", "none"}:
            return False
        if text == "separate":
            # The sidewalk exists but is mapped as its own way, so *this* way - the
            # roadway - carries none. Reading it as "has sidewalk" would decrement the
            # stress level on evidence pointing the other way, and on a fast arterial
            # that is the difference between LTS 2 and LTS 4.
            return False
        if text == "crossing":
            # Says something about a junction, not about walking the length of the way.
            return None
        return True
    if tags.get("sidewalk:left") or tags.get("sidewalk:right"):
        return True
    if str(tags.get("foot", "")).lower() == "designated":
        return True
    return None


def aadt_of(tags: dict[str, Any]) -> float | None:
    """Annual average daily traffic from a conflated column, if the frame carries one."""
    for column in AADT_COLUMNS:
        if column not in tags:
            continue
        try:
            return float(tags[column])
        except (TypeError, ValueError):
            return None
    return None


def survey_speed_of(tags: dict[str, Any]) -> float | None:
    """The surveyed 85th-percentile speed a region build merged onto this way, or `None`.

    `None` for a NaN as well as for an absent column: a frame read from PostGIS carries the
    column on every way, null where no survey matched.
    """
    try:
        value = float(tags[SURVEY_SPEED_COLUMN])
    except (KeyError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def lts_for_way(tags: dict[str, Any]) -> LTSResult:
    """The one way to score a way, whoever is asking (ADR 0025, 0053).

    Reads every input a way's tags can carry - the posted speed and the rest of its OSM
    tags, a volume, a surveyed speed - so that the hostility scorer, the build's `way_lts`
    table and the tuning classes cannot each pick a different subset of them. Before M18
    `preferences.way_class` already read fewer than the other two.
    """
    return lts_from_tags(tags, aadt=aadt_of(tags), operating_speed_kph=survey_speed_of(tags))


def lts_from_tags(
    tags: dict[str, Any],
    aadt: float | None = None,
    operating_speed_kph: float | None = None,
) -> LTSResult:
    """Level of traffic stress 1-4 for someone on foot.

    `aadt` is a confidence-raiser, never a requirement (risk R2). HPMS covers arterials
    and collectors only (scope 12), and conflating it onto OSM geometry is unreliable, so
    the level is computed from tags and refined only when a volume is actually supplied.

    `operating_speed_kph` is a surveyed 85th percentile (M18). The speed read is the higher
    of it and the posted limit - a 40 km/h street where traffic does 60 is a 60 km/h street
    to someone beside it, and a survey that came in under the limit is no reason to believe
    a sign less. A survey is a measured speed, so it also lifts the missing-speed penalty.
    """
    reasons: list[str] = []
    confidence = 1.0

    highway = str(tags.get("highway", "")).lower()
    posted = parse_maxspeed(tags.get("maxspeed"))
    speed, label = posted, "maxspeed"
    if operating_speed_kph is not None and (posted is None or operating_speed_kph > posted):
        speed, label = operating_speed_kph, "speed85"
    lanes = parse_lanes(tags.get("lanes"))
    sidewalk = has_sidewalk(tags)

    if highway in SEPARATED_HIGHWAYS:
        reasons.append("separated_path")
        return LTSResult(level=1, confidence=1.0, reasons=reasons)

    base = STRESS_BY_HIGHWAY.get(highway)
    if base is None:
        base = 2
        confidence -= UNKNOWN_HIGHWAY_PENALTY
        reasons.append(f"unknown_highway_class:{highway or 'missing'}")
    else:
        reasons.append(f"highway:{highway}")

    level = base

    if sidewalk is True:
        reasons.append("sidewalk_present")
        if level > 1:
            level -= 1
    elif sidewalk is False:
        reasons.append("no_sidewalk")
        level += 1
    else:
        confidence -= MISSING_SIDEWALK_PENALTY
        reasons.append("sidewalk_unknown")

    if speed is None:
        confidence -= MISSING_SPEED_PENALTY
        reasons.append("maxspeed_unknown")
    elif speed >= HIGH_SPEED_KPH:
        level += 1
        reasons.append(f"{label}_{speed:.0f}kph")
    elif speed <= MODERATE_SPEED_KPH:
        level -= 1
        reasons.append(f"{label}_{speed:.0f}kph")

    if lanes is not None and lanes >= 4:
        level += 1
        reasons.append(f"lanes_{lanes}")

    if aadt is not None:
        confidence = min(confidence + 0.1, 1.0)
        if aadt >= 20_000:
            level += 1
            reasons.append(f"aadt_{aadt:.0f}")
        elif aadt <= 1_500:
            level -= 1
            reasons.append(f"aadt_{aadt:.0f}")

    return LTSResult(
        level=min(max(level, 1), 4),
        confidence=min(max(confidence, 0.0), 1.0),
        reasons=reasons,
    )
