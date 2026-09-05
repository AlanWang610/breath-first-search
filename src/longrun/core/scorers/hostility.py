"""Per-segment traffic stress (scope 7.2, 8.3).

The measurement is an LTS level from `core.routing.lts`; this module does not decide
whether that level is acceptable. Two thresholds sit on top of it and they come from
different places on purpose:

* the **soft** flag is `ctx.profile.traffic_tolerance` — the user's own line, and a
  comfort-tier concern (scope 8.4 calls an LTS 2 to 3 trade against shade a same-tier
  trade, which it could not be if soft hostility were a safety flag);
* the **hard** flag is LTS 4, a safety floor. `traffic_tolerance` is capped at 3 by
  `core.preferences.floors`, but this module does not lean on that cap: the hard test is
  written against the floor constant directly, so a hand-built profile cannot silence it.

Missing tags lower `confidence` and never manufacture a level (scope 12). `lts_from_tags`
already returns a weaker claim for a road with no `maxspeed`, and that weakened confidence
is carried straight through to the measurement rather than being rounded away.

**Shared plumbing.** The way-tag join and the metric route frame below are used by every
scorer in this package. They live here, rather than in a private module, only because this
milestone owns six scorer files and no seventh; they are the first thing that should move
when a `core/scorers/_common.py` is allowed to exist.

**Coverage is returned, not published.** Scope 4.1 makes every scorer a pure function of
its inputs, so none of these mutate `ctx.coverage`; the scoring loop copies the returned
entries into the plan-wide manifest (`base.publish` is there for exactly that). A scorer
that published its own would double every line of the sheet's coverage section when the
loop copies them too.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from shapely.geometry import LineString, Point

from longrun.core.data.file_store import LayerNotFound
from longrun.core.geo.projections import local_crs, transformer_to
from longrun.core.geo.segments import DEFAULT_CORRIDOR_BUFFER_M, corridor
from longrun.core.models.geometry import Route, Segment
from longrun.core.models.measurement import (
    Flag,
    FlagKind,
    ScorerResult,
    SegmentMeasurement,
    Tier,
)
from longrun.core.preferences.floors import LTS_HARD
from longrun.core.routing.lts import lts_from_tags
from longrun.core.scorers.base import record_coverage, unavailable

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext

name = "segment_hostility"

#: The layer every tag-reading scorer opens.
WAYS_LAYER = "ways"

#: Column carrying the OSM way id in a `ways` frame; the join key to `Segment.way_id`.
WAY_ID_COLUMN = "way_id"

#: Columns an HPMS conflation may have written onto the ways frame (scope 5, 7.2). AADT
#: is a confidence-raiser for `lts_from_tags`, never a requirement.
AADT_COLUMNS = ("aadt", "AADT", "aadt_veh_day")

#: `SegmentMeasurement.segment_id` for the one row describing the whole route. Route
#: totals ("fraction of length at LTS >= 3", scope 7.1) have no segment to live on, and
#: real segment ids are `s00000`-shaped, so this cannot collide with one.
ROUTE_SUMMARY_ID = "route"

#: Confidence for a segment whose way is not in the layer at all. Not zero: we still know
#: where the segment is, only nothing about what it is made of.
UNKNOWN_WAY_CONFIDENCE = 0.3


# --- shared plumbing --------------------------------------------------------


def row_tags(row: Any) -> dict[str, Any]:
    """One GeoDataFrame row as an OSM tag dict, with geometry and nulls dropped.

    Nulls are dropped rather than carried as None so that `tags.get("sidewalk")` means
    "this way has no sidewalk tag" — which `has_sidewalk` reads as *unknown* — instead of
    a NaN that a later truthiness test would read as a value (scope 12).
    """
    tags: dict[str, Any] = {}
    for key, value in row.items():
        if key == "geometry" or value is None:
            continue
        try:
            if bool(value != value):  # NaN and pandas NA are not equal to themselves
                continue
        except (TypeError, ValueError):  # pragma: no cover - exotic array-valued cell
            pass
        tags[key] = value
    return tags


def frame_tags_by_way(frame: Any) -> dict[int, dict[str, Any]]:
    """Index an already-fetched ways frame by way id."""
    by_way: dict[int, dict[str, Any]] = {}
    if len(frame) == 0 or WAY_ID_COLUMN not in frame.columns:
        return by_way
    for _, row in frame.iterrows():
        try:
            way_id = int(row[WAY_ID_COLUMN])
        except (TypeError, ValueError):
            continue
        by_way[way_id] = row_tags(row)
    return by_way


def way_tags_in_corridor(
    route: Route, ctx: ScorerContext, buffer_m: float = DEFAULT_CORRIDOR_BUFFER_M
) -> dict[int, dict[str, Any]]:
    """Tags of every way in the route corridor, keyed by way id.

    Raises `LayerNotFound` when the layer is absent; callers turn that into an
    `unavailable()` result rather than letting it escape (scope 3.6).
    """
    return frame_tags_by_way(ctx.layers.ways_in_corridor(corridor(route, buffer_m=buffer_m)))


def segment_tags(segment: Segment, by_way: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    """The tags of the way a segment lies on, or None when they are not known.

    None is a third answer, distinct from an empty tag dict: an unmatched segment has told
    us nothing, and every caller lowers confidence rather than assuming defaults.
    """
    if segment.way_id is None:
        return None
    return by_way.get(int(segment.way_id))


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


class RouteFrame:
    """The route in its own metric CRS: distance *along* it, and distance *to* it.

    Both queries are needed by half the scorers here — a crossing has to be placed at a
    kilometre mark, an amenity has to be shown to be beside the route rather than merely
    inside its bounding box — and neither may be computed in degrees (scope 4.1).

    Distances along are rescaled onto the route's own `cum_dist_m`, which is haversine, so
    a located point lands in the same coordinate the segments are indexed by.
    """

    def __init__(self, route: Route) -> None:
        to_local = transformer_to(local_crs(route))
        xs, ys = to_local.transform([p.lon for p in route.points], [p.lat for p in route.points])
        self._to_local = to_local
        self.line = LineString(list(zip(xs, ys, strict=True)))
        self.length_m = route.length_m
        self._scale = self.length_m / self.line.length if self.line.length > 0 else 1.0

    def locate(self, lon: float, lat: float) -> tuple[float, float]:
        """`(cum_dist_m of the nearest point on the route, perpendicular distance in m)`."""
        x, y = self._to_local.transform(lon, lat)
        point = Point(x, y)
        return self.line.project(point) * self._scale, self.line.distance(point)


def segment_at(segments: list[Segment], cum_m: float) -> Segment | None:
    """The segment containing a distance-along-route, or None when it falls outside."""
    if not segments:
        return None
    for segment in segments:
        if cum_m < segment.cum_end_m:
            return segment
    last = segments[-1]
    return last if cum_m <= last.cum_end_m + 1.0 else None


# --- the scorer -------------------------------------------------------------


def severity_for_level(level: int) -> float:
    """Severity rising with the level: 0 at LTS 1, 1.0 at the hard level (scope 8.3)."""
    return min(max((level - 1) / (LTS_HARD - 1), 0.0), 1.0)


def segment_hostility(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
) -> ScorerResult:
    """LTS 1-4 per segment, with severity and soft/hard flags (scope 7.2).

    `etas` is accepted and ignored by design: traffic stress is a property of the road,
    not of the hour, so this scorer is time-independent and its golden output stable.
    """
    try:
        by_way = way_tags_in_corridor(route, ctx)
    except LayerNotFound as exc:
        return unavailable(name, f"ways layer unavailable: {exc}")

    tolerance = int(ctx.profile.traffic_tolerance.value)
    result = ScorerResult(name=name)

    scored_m = 0.0
    high_stress_m = 0.0
    hard_count = 0

    for segment in segments:
        tags = segment_tags(segment, by_way)
        if tags is None:
            result.measurements.append(
                SegmentMeasurement(
                    segment_id=segment.id,
                    values={"lts": None, "lts_confidence": None, "aadt": None},
                    confidence=UNKNOWN_WAY_CONFIDENCE,
                )
            )
            continue

        aadt = aadt_of(tags)
        lts = lts_from_tags(tags, aadt=aadt)
        severity = severity_for_level(lts.level)

        result.measurements.append(
            SegmentMeasurement(
                segment_id=segment.id,
                values={
                    "lts": lts.level,
                    "lts_confidence": lts.confidence,
                    "lts_reasons": ",".join(lts.reasons),
                    "aadt": aadt,
                    "severity": severity,
                },
                confidence=lts.confidence,
            )
        )

        scored_m += segment.length_m
        if lts.level >= 3:
            high_stress_m += segment.length_m

        # Tested against the floor, not the profile, so no profile value — however it was
        # constructed — can suppress it (scope 6.3).
        if lts.level >= LTS_HARD:
            hard_count += 1
            result.flags.append(
                Flag(
                    scorer=name,
                    segment_id=segment.id,
                    kind=FlagKind.HARD,
                    tier=Tier.SAFETY,
                    severity=severity,
                    reason_code=f"lts_{lts.level}",
                    detail=f"LTS {lts.level} ({'; '.join(lts.reasons)})",
                )
            )
        elif lts.level > tolerance:
            result.flags.append(
                Flag(
                    scorer=name,
                    segment_id=segment.id,
                    kind=FlagKind.SOFT,
                    tier=Tier.COMFORT,
                    severity=severity,
                    reason_code=f"lts_{lts.level}",
                    detail=f"LTS {lts.level} above a tolerance of {tolerance}",
                )
            )

    result.measurements.append(
        SegmentMeasurement(
            segment_id=ROUTE_SUMMARY_ID,
            values={
                "length_m": route.length_m,
                "scored_m": scored_m,
                "fraction_lts3_plus": (high_stress_m / scored_m) if scored_m else None,
                "lts4_count": hard_count,
                "traffic_tolerance": tolerance,
            },
            confidence=1.0 if scored_m else 0.0,
        )
    )

    record_coverage(
        result, source=WAYS_LAYER, kind="osm_tags", vintage=ctx.layers.vintage(WAYS_LAYER)
    )
    return result


#: The name the scorer registry loads. Aliased so a caller can dispatch on
#: `module.score` without knowing which scope tool this module implements.
score = segment_hostility
