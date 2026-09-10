"""Where the route crosses a road, and whether anything stops the traffic (scope 7.2, 8.3).

Scope 8.3 sets the thresholds: an unsignalized crossing of a secondary road is a soft
flag, and an unsignalized crossing of a primary or trunk road posted above
`CROSSING_HARD_SPEED_KPH` is a hard one — the same condition `gpx_verify` check 8 fails on.

Three things this scorer refuses to do:

* **Confuse a bridge with a crossing.** Two lines meeting in plan view have not met on the
  ground. A `bridge`, a `tunnel`, or differing `layer` tags on either side means the route
  passes over or under, and the intersection is discarded rather than reported as an
  at-grade crossing of a motorway.
* **Infer signalization from silence.** With no node layer to consult, a crossing's
  signalization is *unknown* (scope 12), which means a lowered confidence and an unchecked
  coverage entry — not a hard flag asserting there is no signal. The distinction is
  exactly the one in `core.data.file_store.LayerNotFound`: "checked, found nothing" is
  evidence, "never looked" is not.
* **Attach a sign.** A signalized crossing is counted and reported. Whether stopping at it
  is a cost is `stop_density` plus the profile's business, not this scorer's.

Tier: hard crossings are SAFETY (scope 8.4 names them). Soft ones are COMFORT — 8.4's
safety tier lists "hard crossings" specifically, and its comfort tier is where the
stop-like concerns live, so an unsignalized secondary is a same-tier trade against shade
rather than something that outranks every comfort consideration on the route.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from shapely.geometry import LineString

from longrun.core.data.file_store import LayerNotFound
from longrun.core.geo.segments import corridor
from longrun.core.models.coverage import CoverageEntry
from longrun.core.models.geometry import Route, Segment
from longrun.core.models.measurement import (
    Flag,
    FlagKind,
    ScorerResult,
    SegmentMeasurement,
    Tier,
)
from longrun.core.preferences.floors import CROSSING_HARD_SPEED_KPH
from longrun.core.routing.lts import parse_maxspeed
from longrun.core.scorers._common import (
    ROUTE_SUMMARY_ID,
    WAYS_LAYER,
    RouteFrame,
    frame_tags_by_way,
    row_tags,
    segment_at,
)
from longrun.core.scorers.base import record_coverage, unavailable

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext

name = "crossings"

#: Road classes worth reporting a crossing of, ranked by how much stops for whom.
CLASS_RANK: dict[str, int] = {
    "tertiary": 1,
    "tertiary_link": 1,
    "secondary": 2,
    "secondary_link": 2,
    "primary": 3,
    "primary_link": 3,
    "trunk": 4,
    "trunk_link": 4,
    "motorway": 5,
    "motorway_link": 5,
}

#: Secondary and above (scope 7.2: "roads above a class threshold").
MIN_REPORTED_RANK = CLASS_RANK["secondary"]

#: Primary and above: the class half of scope 8.3's hard threshold.
HARD_RANK = CLASS_RANK["primary"]

#: Amenity kinds that might mark a crossing as controlled.
SIGNAL_KINDS: tuple[str, ...] = ("traffic_signals", "crossing", "level_crossing", "stop")

#: How close a signal node must be — along the route and away from it — to be this
#: crossing's signal. Wider than an intersection is deep, narrower than a city block.
SIGNAL_RADIUS_M = 30.0

#: Layer the signal-node query goes to.
#:
#: Deliberately NOT "amenities". That layer holds drinking water, toilets and food, so
#: pointing signal detection at it means a region with fountains but no signal data reads
#: as "checked, and no signals exist" - which silently marks every crossing unsignalized.
#: With a dedicated layer, a region that never extracted signal nodes reports signalization
#: as unknown, and gpx_verify check 8 skips instead of ruling either way.
NODES_LAYER = "nodes"

#: Tag values that mean "this tag is present but says no".
FALSY_TAG_VALUES = frozenset({"no", "false", "0", ""})

SEVERITY_BY_CODE: dict[str, float] = {
    "unsignalized_primary_crossing": 1.0,
    "unsignalized_primary_crossing_unknown_speed": 0.7,
    "unsignalized_primary_crossing_low_speed": 0.6,
    "unsignalized_secondary_crossing": 0.5,
}

#: Confidence for a crossing whose signalization could not be established.
UNKNOWN_SIGNAL_CONFIDENCE = 0.5


def _tag_is_set(tags: dict[str, Any], key: str) -> bool:
    value = tags.get(key)
    if value is None:
        return False
    return str(value).strip().lower() not in FALSY_TAG_VALUES


def _layer_of(tags: dict[str, Any]) -> float | None:
    try:
        return float(str(tags.get("layer")))
    except (TypeError, ValueError):
        return None


def grade_separated(route_tags: dict[str, Any] | None, crossed_tags: dict[str, Any]) -> bool:
    """Whether the two ways pass at different heights rather than meeting.

    Checked from both sides, because either the route or the road it appears to cross may
    be the one on the structure.
    """
    if _tag_is_set(crossed_tags, "bridge") or _tag_is_set(crossed_tags, "tunnel"):
        return True
    if route_tags is None:
        return False
    if _tag_is_set(route_tags, "bridge") or _tag_is_set(route_tags, "tunnel"):
        return True
    route_layer, crossed_layer = _layer_of(route_tags), _layer_of(crossed_tags)
    return route_layer is not None and crossed_layer is not None and route_layer != crossed_layer


def is_signalized(tags: dict[str, Any]) -> bool:
    """Whether a node stops the traffic, from whichever tag the extract carries it in."""
    if str(tags.get("kind", "")).strip().lower() == "traffic_signals":
        return True
    if str(tags.get("highway", "")).strip().lower() == "traffic_signals":
        return True
    return "traffic_signals" in str(tags.get("crossing", "")).strip().lower()


def signal_positions(route: Route, ctx: ScorerContext, frame: RouteFrame) -> list[float] | None:
    """Distances along the route of signalized nodes, or None when nobody was asked.

    None is the load-bearing return value: it is what stops an absent node layer from
    being read as an absence of signals (scope 12).
    """
    try:
        points = ctx.layers.points_in_corridor(
            corridor(route), list(SIGNAL_KINDS), layer=NODES_LAYER
        )
    except LayerNotFound:
        return None
    positions: list[float] = []
    for _, row in points.iterrows():
        tags = row_tags(row)
        if not is_signalized(tags):
            continue
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        cum_m, offset_m = frame.locate(geometry.x, geometry.y)
        if offset_m <= SIGNAL_RADIUS_M:
            positions.append(cum_m)
    return positions


def _intersection_points(route_line: Any, geometry: Any) -> list[Any]:
    """Point components of an intersection.

    A LineString component means the route runs *along* the other way rather than across
    it, and is dropped: that is a shared segment, which `segment_hostility` already scores.
    """
    if geometry is None or geometry.is_empty:
        return []
    shape = route_line.intersection(geometry)
    if shape.is_empty:
        return []
    parts = list(getattr(shape, "geoms", [shape]))
    return [p for p in parts if p.geom_type == "Point"]


def classify(rank: int, speed_kph: float | None, floor_kph: float = CROSSING_HARD_SPEED_KPH) -> str:
    """The reason code for an unsignalized crossing of a road of this class and speed."""
    if rank < HARD_RANK:
        return "unsignalized_secondary_crossing"
    if speed_kph is None:
        return "unsignalized_primary_crossing_unknown_speed"
    if speed_kph > floor_kph:
        return "unsignalized_primary_crossing"
    return "unsignalized_primary_crossing_low_speed"


def crossings(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
) -> ScorerResult:
    """At-grade crossings of roads at or above secondary, with count per km (scope 7.2)."""
    try:
        frame = ctx.layers.lines_crossing(route, WAYS_LAYER)
    except LayerNotFound as exc:
        return unavailable(name, f"ways layer unavailable: {exc}")

    route_frame = RouteFrame(route)
    route_line = wgs84_line(route)
    by_way = frame_tags_by_way(frame)
    own_way_ids = {int(s.way_id) for s in segments if s.way_id is not None}
    signals = signal_positions(route, ctx, route_frame)

    counts: dict[str, int] = {s.id: 0 for s in segments}
    unsignalized: dict[str, int] = {s.id: 0 for s in segments}
    unknown_signal: set[str] = set()
    result = ScorerResult(name=name)
    total = 0
    separated = 0

    for _, row in frame.iterrows():
        tags = row_tags(row)
        way_id = tags.get("way_id")
        if way_id is not None and int(way_id) in own_way_ids:
            continue
        rank = CLASS_RANK.get(str(tags.get("highway", "")).strip().lower())
        if rank is None or rank < MIN_REPORTED_RANK:
            continue

        speed_kph = parse_maxspeed(tags.get("maxspeed"))
        for point in _intersection_points(route_line, row.geometry):
            cum_m, _ = route_frame.locate(point.x, point.y)
            segment = segment_at(segments, cum_m)
            if segment is None:
                continue
            route_tags = by_way.get(int(segment.way_id)) if segment.way_id is not None else None
            if grade_separated(route_tags, tags):
                separated += 1
                continue

            total += 1
            counts[segment.id] += 1

            if signals is None:
                unknown_signal.add(segment.id)
                continue
            if any(abs(position - cum_m) <= SIGNAL_RADIUS_M for position in signals):
                continue

            unsignalized[segment.id] += 1
            code = classify(rank, speed_kph)
            hard = code == "unsignalized_primary_crossing"
            result.flags.append(
                Flag(
                    scorer=name,
                    segment_id=segment.id,
                    kind=FlagKind.HARD if hard else FlagKind.SOFT,
                    tier=Tier.SAFETY if hard else Tier.COMFORT,
                    severity=SEVERITY_BY_CODE[code],
                    reason_code=code,
                    detail=(
                        f"unsignalized crossing of {tags.get('highway')} at "
                        f"{cum_m / 1000:.2f} km"
                        + (f", posted {speed_kph:.0f} kph" if speed_kph is not None else "")
                    ),
                )
            )

    for segment in segments:
        unknown = segment.id in unknown_signal
        result.measurements.append(
            SegmentMeasurement(
                segment_id=segment.id,
                values={
                    "crossings": counts[segment.id],
                    "unsignalized": None if unknown else unsignalized[segment.id],
                },
                confidence=UNKNOWN_SIGNAL_CONFIDENCE if unknown else 1.0,
            )
        )

    km = route.length_m / 1000.0
    result.measurements.append(
        SegmentMeasurement(
            segment_id=ROUTE_SUMMARY_ID,
            values={
                "crossings": total,
                "crossings_per_km": (total / km) if km > 0 else None,
                "unsignalized": None if signals is None else sum(unsignalized.values()),
                "grade_separated": separated,
                "signals_checked": signals is not None,
            },
            confidence=1.0 if signals is not None else UNKNOWN_SIGNAL_CONFIDENCE,
        )
    )

    record_coverage(
        result, source=WAYS_LAYER, kind="osm_crossings", vintage=ctx.layers.vintage(WAYS_LAYER)
    )
    if signals is None:
        result.coverage.append(
            CoverageEntry(
                source=NODES_LAYER,
                kind="traffic_signals",
                checked=False,
                reason="no node layer: crossings reported without signalization",
            )
        )
    else:
        record_coverage(
            result,
            source=NODES_LAYER,
            kind="traffic_signals",
            vintage=ctx.layers.vintage(NODES_LAYER),
        )
    return result


def wgs84_line(route: Route) -> Any:
    """The route as a WGS84 LineString, for intersecting against layer geometry.

    Intersections are computed in degrees deliberately: only the *location* of the meeting
    is wanted, and `RouteFrame.locate` converts it to metres immediately after. No
    distance is ever taken from this geometry (scope 4.1).
    """
    return LineString([(p.lon, p.lat) for p in route.points])


#: The name the scorer registry loads. Aliased so a caller can dispatch on
#: `module.score` without knowing which scope tool this module implements.
score = crossings
