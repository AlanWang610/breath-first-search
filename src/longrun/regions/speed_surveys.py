"""Speed surveys, merged onto the ways they measured, at region build (M18, ADR 0053).

A city's speed survey says traffic on Main Street does 61 km/h at the 85th percentile where
the sign says 40. Scope 7.10 makes that an adapter kind; this is where it is consumed - once,
at build, never at plan time. LTS is computed once (ADR 0025) and the router's graph is built
from it, so a survey read at plan time could change the scorer's level without changing the
graph's, which is the disagreement that ADR exists to end.

**Matching is geometry, and a little name.** A survey is a line with a speed; a way is a
line with tags. A survey is merged onto a way when, in a metric frame:

* the way lies within `MATCH_DISTANCE_M` of the survey for at least `MIN_OVERLAP` of the
  shorter of the two - so a cross street that touches the survey at one end is not it;
* the two run within `MATCH_BEARING_DEG` of each other, undirected - a line digitised the
  other way round is the same street;
* and, where both carry one, their names agree after the usual suffix abbreviations - two
  parallel streets 12 m apart (a frontage road) are told apart by name and nothing else.

**Only what can be trusted is merged:** tier 3 or better at confidence 0.8 or more. A
tier-4 speed read off a PDF by a model is a lead, not a measurement to build a graph from.
Where two surveys claim one way, the better tier wins, then the higher confidence, then the
newer count, then the higher speed - the conservative reading for a pedestrian.

This sits in `regions/` beside `lts.py` for the reason it does: a build writes, and `core/`
may not depend on a database being there. The matcher itself is pure and is what the unit
tests hold.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable, Iterable, Sequence

    from longrun.core.models.features import Feature

SCHEMA = "speed_survey"
TABLE = "way_speed"
QUALIFIED = f"{SCHEMA}.{TABLE}"

#: How far a way may lie from a survey line and still be the street it measured. Twice the
#: half-width of a four-lane arterial, which is where a survey digitised on one carriageway
#: and a way on the centreline part company.
MATCH_DISTANCE_M = 15.0

#: How far the two may disagree in direction. Tighter than closures' 30 degrees: a closure
#: that crosses the route still matters to it, a survey of the cross street does not.
MATCH_BEARING_DEG = 20.0

#: The share of the shorter line that must lie inside the other's reach.
MIN_OVERLAP = 0.5

#: What may be merged into a routing graph.
MAX_TIER = 3
MIN_CONFIDENCE = 0.8

#: Candidate ways are fetched by an index-friendly box in degrees before the exact test in
#: metres; this is comfortably wider than `MATCH_DISTANCE_M` at any US latitude.
SEARCH_DEG = 0.0005

_SUFFIXES = {
    "street": "st",
    "avenue": "ave",
    "road": "rd",
    "boulevard": "blvd",
    "drive": "dr",
    "lane": "ln",
    "parkway": "pkwy",
    "place": "pl",
    "court": "ct",
    "highway": "hwy",
    "terrace": "ter",
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
}


@dataclass(frozen=True)
class Candidate:
    """A way that might be the one a survey measured: its id, its tags, its line (4326)."""

    way_id: int
    tags: dict[str, Any]
    line: Any


@dataclass(frozen=True)
class SurveyMatch:
    """One survey merged onto one way, with what made the match."""

    way_id: int
    speed_85_kph: float
    source: str
    survey_ref: str | None
    observed_on: date | None
    tier: int
    confidence: float
    offset_m: float
    bearing_diff_deg: float
    name_matched: bool


def usable(feature: Feature) -> bool:
    """Whether a survey may be merged into a graph at all."""
    return (
        feature.kind == "speed_survey"
        and int(feature.tier) <= MAX_TIER
        and feature.confidence >= MIN_CONFIDENCE
        and bool(feature.measures)
    )


def normalise_name(name: Any) -> str | None:
    """A street name compared the way people write it: case, punctuation and suffixes."""
    if not name:
        return None
    words = re.sub(r"[^a-z0-9 ]", " ", str(name).lower()).split()
    return " ".join(_SUFFIXES.get(word, word) for word in words) or None


def _names(tags: dict[str, Any]) -> set[str]:
    found = {normalise_name(tags.get(key)) for key in ("name", "ref", "alt_name", "official_name")}
    return {name for name in found if name}


def _lines(geometry: dict[str, Any]) -> Any:
    from shapely.geometry import shape
    from shapely.ops import linemerge

    line = shape(geometry)
    if line.geom_type == "MultiLineString":
        line = linemerge(line)
    return line


def _bearing(line: Any) -> float | None:
    """The axis of a projected line, start to end, in degrees from north."""
    if line.is_empty or line.length == 0:
        return None
    if line.geom_type == "MultiLineString":
        line = max(line.geoms, key=lambda part: part.length)
    (x1, y1), (x2, y2) = line.coords[0], line.coords[-1]
    if x1 == x2 and y1 == y2:
        return None
    return math.degrees(math.atan2(x2 - x1, y2 - y1)) % 360.0


def _bearing_difference(one: float, other: float) -> float:
    delta = abs(one - other) % 180.0
    return min(delta, 180.0 - delta)


def match_survey(
    survey: Feature, candidates: Sequence[Candidate], source: str
) -> list[SurveyMatch]:
    """Every way this survey measured, by the three rules in the module docstring."""
    from pyproj import Transformer
    from shapely.ops import transform

    from longrun.core.geo.projections import utm_epsg

    if not survey.measures:
        return []
    line = _lines(survey.geometry)
    if line.is_empty:
        return []
    centre = line.centroid
    to_metres = Transformer.from_crs(4326, utm_epsg(centre.y, centre.x), always_xy=True).transform
    measured = transform(to_metres, line)
    if measured.length == 0:
        return []
    reach = measured.buffer(MATCH_DISTANCE_M)
    wanted = normalise_name(survey.name)
    speed = float(survey.measures["speed_85_kph"])

    found: list[SurveyMatch] = []
    for candidate in candidates:
        way = transform(to_metres, candidate.line)
        if way.is_empty or way.length == 0:
            continue
        inside = way.intersection(reach)
        if inside.is_empty or inside.length / min(way.length, measured.length) < MIN_OVERLAP:
            continue
        along = measured.intersection(way.buffer(MATCH_DISTANCE_M))
        way_bearing, survey_bearing = _bearing(inside), _bearing(along)
        if way_bearing is None or survey_bearing is None:
            continue
        difference = _bearing_difference(way_bearing, survey_bearing)
        if difference > MATCH_BEARING_DEG:
            continue
        names = _names(candidate.tags)
        if wanted and names and wanted not in names:
            continue
        middle = inside.interpolate(0.5, normalized=True)
        found.append(
            SurveyMatch(
                way_id=candidate.way_id,
                speed_85_kph=speed,
                source=source,
                survey_ref=survey.ref,
                observed_on=survey.observed_on,
                tier=int(survey.tier),
                confidence=float(survey.confidence),
                offset_m=round(float(middle.distance(measured)), 2),
                bearing_diff_deg=round(difference, 2),
                name_matched=bool(wanted and names),
            )
        )
    return found


def resolve_conflicts(matches: Iterable[SurveyMatch]) -> list[SurveyMatch]:
    """One survey per way: better tier, then confidence, then newer, then faster."""
    best: dict[int, SurveyMatch] = {}
    for match in matches:
        held = best.get(match.way_id)
        if held is None or _rank(match) < _rank(held):
            best[match.way_id] = match
    return [best[way_id] for way_id in sorted(best)]


def _rank(match: SurveyMatch) -> tuple[int, float, int, float]:
    newest = -match.observed_on.toordinal() if match.observed_on else 0
    return (match.tier, -match.confidence, newest, -match.speed_85_kph)


def vintage_of(rows: Sequence[SurveyMatch]) -> str | None:
    """A content hash of what was merged - the `+ss<vintage>` on the graph's identity.

    Content rather than a date, so a rebuild that merges the same surveys onto the same ways
    keeps the identity every cached route was keyed on, and one that changes anything does
    not.
    """
    if not rows:
        return None
    text = "\n".join(
        f"{r.way_id}|{r.speed_85_kph:.2f}|{r.source}|{r.observed_on or ''}" for r in rows
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def create_statements() -> list[str]:
    """DDL, idempotent. Its own schema for the reason `osm_lts` has one: a vintage row under
    `osm` would become the vintage every plan reports for `ways`."""
    return [
        f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
        f"CREATE TABLE IF NOT EXISTS {QUALIFIED} ("
        "way_id bigint PRIMARY KEY, "
        "speed_85_kph real NOT NULL, "
        "source text NOT NULL, "
        "survey_ref text, "
        "observed_on date, "
        "tier smallint NOT NULL, "
        "confidence real NOT NULL, "
        "offset_m real NOT NULL, "
        "bearing_diff_deg real NOT NULL, "
        "name_matched boolean NOT NULL, "
        "region text NOT NULL, "
        "vintage text NOT NULL)",
        f"CREATE INDEX IF NOT EXISTS {TABLE}_region_idx ON {QUALIFIED} (region)",
    ]


@dataclass
class SurveyReport:
    """What one merge did, for the build manifest."""

    surveys: int = 0
    usable: int = 0
    merged: int = 0
    vintage: str | None = None
    changed: bool = False
    by_source: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        if not self.surveys:
            return "no speed survey answered for this region"
        sources = ", ".join(f"{n} from {s}" for s, n in sorted(self.by_source.items()))
        return (
            f"{self.merged} way(s) carry a surveyed speed ({sources or 'none'}) from "
            f"{self.usable} usable of {self.surveys} survey record(s); vintage "
            f"{self.vintage or 'none'}{'' if self.changed else ', unchanged'}"
        )


def _candidates(connection: Any, survey: Feature) -> list[Candidate]:
    """Ways near a survey: an index box in degrees, refined in metres by the matcher."""
    from shapely import from_wkb
    from shapely.geometry import shape

    west, south, east, north = shape(survey.geometry).bounds
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT way_id, tags, ST_AsBinary(geom) FROM osm.ways "
            "WHERE tags ? 'highway' AND geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)",
            (west - SEARCH_DEG, south - SEARCH_DEG, east + SEARCH_DEG, north + SEARCH_DEG),
        )
        return [
            Candidate(way_id=int(way_id), tags=dict(tags or {}), line=from_wkb(bytes(wkb)))
            for way_id, tags, wkb in cursor.fetchall()
        ]


def compute(
    connection: Any,
    *,
    region: str,
    surveys: Sequence[Feature],
    log: Callable[[str], None] = lambda _: None,
) -> SurveyReport:
    """Match a region's surveys to its ways and write `speed_survey.way_speed`.

    The region's rows are replaced whole, so a survey that stopped matching - or stopped
    being published - stops steering the graph on the next build. `changed` says whether
    anything differs from what was there, which is what tells the build to rescore
    `way_lts`.
    """
    report = SurveyReport(surveys=len(surveys))
    with connection.cursor() as cursor:
        for statement in create_statements():
            cursor.execute(statement)
        cursor.execute(
            f"SELECT way_id, speed_85_kph, source FROM {QUALIFIED} WHERE region = %s", (region,)
        )
        before = {(int(w), round(float(s), 2), src) for w, s, src in cursor.fetchall()}

    matches: list[SurveyMatch] = []
    for survey in surveys:
        if not usable(survey):
            continue
        report.usable += 1
        source = survey.source_url or survey.jurisdiction or "speed_survey"
        matches.extend(match_survey(survey, _candidates(connection, survey), source))
    rows = resolve_conflicts(matches)
    report.merged = len(rows)
    report.vintage = vintage_of(rows)
    for row in rows:
        report.by_source[row.source] = report.by_source.get(row.source, 0) + 1
    after = {(r.way_id, round(r.speed_85_kph, 2), r.source) for r in rows}
    report.changed = after != before

    with connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {QUALIFIED} WHERE region = %s", (region,))
        for r in rows:
            cursor.execute(
                f"INSERT INTO {QUALIFIED} (way_id, speed_85_kph, source, survey_ref, observed_on, "
                "tier, confidence, offset_m, bearing_diff_deg, name_matched, region, vintage) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (way_id) DO UPDATE SET speed_85_kph = EXCLUDED.speed_85_kph, "
                "source = EXCLUDED.source, survey_ref = EXCLUDED.survey_ref, "
                "observed_on = EXCLUDED.observed_on, tier = EXCLUDED.tier, "
                "confidence = EXCLUDED.confidence, offset_m = EXCLUDED.offset_m, "
                "bearing_diff_deg = EXCLUDED.bearing_diff_deg, "
                "name_matched = EXCLUDED.name_matched, region = EXCLUDED.region, "
                "vintage = EXCLUDED.vintage",
                (
                    r.way_id,
                    r.speed_85_kph,
                    r.source,
                    r.survey_ref,
                    r.observed_on,
                    r.tier,
                    r.confidence,
                    r.offset_m,
                    r.bearing_diff_deg,
                    r.name_matched,
                    region,
                    report.vintage,
                ),
            )
        if report.vintage is None:
            cursor.execute(
                "DELETE FROM meta.layer_vintage WHERE layer_schema = %s AND region = %s",
                (SCHEMA, region),
            )
        else:
            cursor.execute(
                "INSERT INTO meta.layer_vintage "
                "(layer_schema, source, region, vintage, source_url) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (layer_schema, source, region) DO UPDATE SET "
                "vintage = EXCLUDED.vintage, source_url = EXCLUDED.source_url, loaded_at = now()",
                (SCHEMA, "speed_survey", region, report.vintage, None),
            )
    connection.commit()
    log(f"  {report.summary()}")
    return report


__all__ = [
    "MATCH_BEARING_DEG",
    "MATCH_DISTANCE_M",
    "MIN_OVERLAP",
    "QUALIFIED",
    "Candidate",
    "SurveyMatch",
    "SurveyReport",
    "compute",
    "create_statements",
    "match_survey",
    "normalise_name",
    "resolve_conflicts",
    "usable",
    "vintage_of",
]
