"""Things on the route that can hurt you and are not traffic (scope 7.6).

The scope's list is deliberately miscellaneous — at-grade rail crossings, tunnels, bridges
with no walkway, water crossings, cattle guards, seasonal snow, NWS flood and red-flag
warnings — and what unifies it is not the hazard but the reporting: **six independent
sources, each of which can be absent, and a plan that must say which ones answered.** A
route scored with no railway layer and no alerts is not a route with no rail crossings.

So the shape here is one measurement pass per source, each guarded, each writing its own
coverage entry, and a per-segment confidence that falls as sources drop out. That is scope
3.6 and 12 applied to a scorer with more inputs than any other.

**Everything is a soft flag in the COMFORT tier**, and ADR 0011 records why: the tiers are
lexicographic, so a single SAFETY flag beats any amount of
evidence below it, and the genuinely disqualifying cases here are already owned by
`legality` (a tunnel you may not enter is `foot=no`) and `crossings` (a road you cannot
safely cross). Promoting hazards would double-count those and would let one cattle grid
outrank dangerous heat.

**Water crossings are the noisy one and are marked as such.** NHD and OSM are separately
digitised, so a flowline meeting a way in plan view is often a culvert nobody tagged. The
flag is still worth raising — an unbridged creek on a 100 km route is exactly the surprise
this project exists to remove — but it goes out at a reduced confidence with the reason
saying why, rather than as a claim the ground has been checked.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

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
from longrun.core.scorers._common import (
    ROUTE_SUMMARY_ID,
    WAYS_LAYER,
    RouteFrame,
    segment_at,
    segment_tags,
    way_tags_in_corridor,
)
from longrun.core.scorers.base import record_coverage
from longrun.core.scorers.crossings import NODES_LAYER, wgs84_line

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext

name = "hazards"

RAILWAYS_LAYER = "railways"
FLOWLINES_LAYER = "flowlines"

#: How close a point feature has to be to the route to count as on it. Tighter than the
#: crossing-signal radius: a cattle grid twenty metres off the line is on a different path.
POINT_RADIUS_M = 15.0

#: `barrier` values scope 7.6 names as a hazard underfoot.
GRID_KINDS = frozenset({"cattle_grid"})

#: Elevation above which lying snow is plausible in the months below.
#:
#: A blunt instrument, and the scorer says so in its confidence rather than pretending
#: otherwise: real snow lines vary by latitude, aspect and year, and the honest version of
#: this needs SNODAS or a snow-depth raster. What this catches is the case worth catching —
#: a route planned over a pass in February by someone who has not looked.
SNOW_ELEVATION_M = 2000.0

#: Northern-hemisphere months in which `SNOW_ELEVATION_M` means anything.
SNOW_MONTHS = frozenset({11, 12, 1, 2, 3, 4})

#: Road classes where a bridge without a footway is worth flagging. Below these a bridge is
#: a residential street over a creek and a runner shares it with nobody.
BRIDGE_ROAD_CLASSES = frozenset({"motorway", "trunk", "primary", "secondary", "tertiary"})

SEVERITY_BY_CODE: dict[str, float] = {
    "rail_at_grade": 0.5,
    "confirmed_ford": 0.8,
    "possible_water_crossing": 0.4,
    "tunnel": 0.4,
    "bridge_no_walkway": 0.6,
    "cattle_grid": 0.2,
    "seasonal_snow": 0.5,
    "flood_warning": 0.8,
    "fire_weather_warning": 0.7,
}

#: Confidence for a water crossing inferred from two independently digitised datasets.
CONFLATION_CONFIDENCE = 0.5

#: Confidence for a segment whose elevation the DEM could not supply.
NO_ELEVATION_CONFIDENCE = 0.7

#: Tag values that mean "present but no".
FALSY = frozenset({"no", "false", "0", ""})


def _set(tags: dict[str, Any], key: str) -> bool:
    value = tags.get(key)
    return value is not None and str(value).strip().lower() not in FALSY


def _is_separated(tags: dict[str, Any] | None) -> bool:
    """Whether the route is carried over or under whatever it meets here."""
    if tags is None:
        return False
    return _set(tags, "bridge") or _set(tags, "tunnel") or _set(tags, "culvert")


def snow_plausible(elevation_m: float | None, when: date) -> bool | None:
    """Three states, and the third is the point.

    None means the DEM had no elevation here — which is not "no snow". A route that loses
    its terrain over a pass is exactly the route this question matters for.
    """
    if elevation_m is None:
        return None
    if when.month not in SNOW_MONTHS:
        return False
    return elevation_m >= SNOW_ELEVATION_M


def has_walkway(tags: dict[str, Any]) -> bool | None:
    """Whether a bridge carries something to run on, or None when the tags do not say."""
    for key in ("sidewalk", "sidewalk:both", "sidewalk:left", "sidewalk:right", "footway"):
        if key in tags:
            return _set(tags, key)
    if str(tags.get("highway", "")).strip().lower() in ("footway", "path", "pedestrian", "steps"):
        return True
    return None


def _flag(segment_id: str, code: str, detail: str) -> Flag:
    return Flag(
        scorer=name,
        segment_id=segment_id,
        kind=FlagKind.SOFT,
        # ADR 0011. Lexicographic tiers mean SAFETY dominates everything below it, and the
        # disqualifying cases here already belong to `legality` and `crossings`.
        tier=Tier.COMFORT,
        severity=SEVERITY_BY_CODE[code],
        reason_code=code,
        detail=detail,
    )


def _crossing_points(route_line: Any, geometry: Any) -> list[Any]:
    """Point components of an intersection; a shared line is running along, not across."""
    if geometry is None or geometry.is_empty:
        return []
    shape = route_line.intersection(geometry)
    if shape.is_empty:
        return []
    return [p for p in getattr(shape, "geoms", [shape]) if p.geom_type == "Point"]


def hazards(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
) -> ScorerResult:
    """Rail, water, tunnels, bridges, cattle grids, snow and NWS alerts (scope 7.6)."""
    result = ScorerResult(name=name)
    frame = RouteFrame(route)
    line = wgs84_line(route)
    counts: dict[str, dict[str, int]] = {s.id: {} for s in segments}
    lowered: set[str] = set()

    def record(segment_id: str, code: str, detail: str) -> None:
        counts.setdefault(segment_id, {})
        counts[segment_id][code] = counts[segment_id].get(code, 0) + 1
        result.flags.append(_flag(segment_id, code, detail))

    # --- way tags: tunnels and bridges --------------------------------------
    try:
        by_way = way_tags_in_corridor(route, ctx)
    except (LayerNotFound, FileNotFoundError):
        by_way = None

    if by_way is None:
        result.coverage.append(
            CoverageEntry(
                source=WAYS_LAYER,
                kind="osm_hazard_tags",
                checked=False,
                reason="no ways layer: tunnels and bridges not established",
            )
        )
    else:
        for segment in segments:
            tags = segment_tags(segment, by_way)
            if tags is None:
                continue
            where = f"{segment.cum_start_m / 1000:.2f} km"
            if _set(tags, "tunnel"):
                record(segment.id, "tunnel", f"tunnel at {where}")
            if _set(tags, "bridge"):
                walkway = has_walkway(tags)
                highway = str(tags.get("highway", "")).strip().lower()
                if walkway is False and highway in BRIDGE_ROAD_CLASSES:
                    record(
                        segment.id,
                        "bridge_no_walkway",
                        f"{highway} bridge with no footway at {where}",
                    )
        record_coverage(
            result,
            source=WAYS_LAYER,
            kind="osm_hazard_tags",
            vintage=ctx.layers.vintage(WAYS_LAYER),
        )

    # --- railways -----------------------------------------------------------
    rail_crossings = _rail(route, segments, ctx, frame, line, by_way, record)
    if rail_crossings is None:
        result.coverage.append(
            CoverageEntry(
                source=RAILWAYS_LAYER,
                kind="rail_crossings",
                checked=False,
                reason="no railways layer: at-grade rail crossings not established",
            )
        )
    else:
        record_coverage(
            result,
            source=RAILWAYS_LAYER,
            kind="rail_crossings",
            vintage=ctx.layers.vintage(RAILWAYS_LAYER),
        )

    # --- water --------------------------------------------------------------
    water = _water(route, segments, ctx, frame, line, by_way, record)
    if water is None:
        result.coverage.append(
            CoverageEntry(
                source=FLOWLINES_LAYER,
                kind="water_crossings",
                checked=False,
                reason="no flowlines layer: water crossings not established",
            )
        )
    else:
        for segment_id in water:
            lowered.add(segment_id)
        record_coverage(
            result,
            source=FLOWLINES_LAYER,
            kind="water_crossings",
            vintage=ctx.layers.vintage(FLOWLINES_LAYER),
            confidence=CONFLATION_CONFIDENCE,
        )

    # --- cattle grids -------------------------------------------------------
    grids = _grids(route, segments, ctx, frame, record)
    if grids is None:
        result.coverage.append(
            CoverageEntry(
                source=NODES_LAYER,
                kind="cattle_grids",
                checked=False,
                reason="no nodes layer: cattle grids not established",
            )
        )
    else:
        record_coverage(
            result, source=NODES_LAYER, kind="cattle_grids", vintage=ctx.layers.vintage(NODES_LAYER)
        )

    # --- seasonal snow ------------------------------------------------------
    day = etas[0].date() if etas else ctx.clock.now().date()
    unknown_elevation = _snow(route, segments, day, record)
    lowered |= unknown_elevation
    result.coverage.append(
        CoverageEntry(
            source="dem",
            kind="seasonal_snow",
            checked=not unknown_elevation,
            reason=(
                None
                if not unknown_elevation
                else f"{len(unknown_elevation)} segment(s) have no elevation; snow not established"
            ),
        )
    )

    # --- NWS alerts ---------------------------------------------------------
    _alerts(route, ctx, day, result, record)

    total = 0
    for segment in segments:
        found = counts.get(segment.id, {})
        total += sum(found.values())
        result.measurements.append(
            SegmentMeasurement(
                segment_id=segment.id,
                values={"hazards": sum(found.values()), **{k: v for k, v in found.items()}},
                confidence=CONFLATION_CONFIDENCE if segment.id in lowered else 1.0,
            )
        )

    by_code: dict[str, int] = {}
    for found in counts.values():
        for code, n in found.items():
            by_code[code] = by_code.get(code, 0) + n
    result.measurements.append(
        SegmentMeasurement(
            segment_id=ROUTE_SUMMARY_ID,
            values={
                "hazards": total + sum(1 for f in result.flags if f.segment_id == ROUTE_SUMMARY_ID),
                "sources_checked": sum(1 for c in result.coverage if c.checked),
                "sources_missing": sum(1 for c in result.coverage if not c.checked),
                **by_code,
            },
            confidence=1.0,
        )
    )
    return result


# --- one function per source ------------------------------------------------


def _rail(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    frame: RouteFrame,
    line: Any,
    by_way: dict[int, dict[str, Any]] | None,
    record: Any,
) -> int | None:
    """At-grade crossings of a live railway line, or None when the layer is absent."""
    try:
        rails = ctx.layers.lines_crossing(route, RAILWAYS_LAYER)
    except (LayerNotFound, FileNotFoundError):
        return None

    found = 0
    for _, row in rails.iterrows():
        for point in _crossing_points(line, row.geometry):
            cum_m, _ = frame.locate(point.x, point.y)
            segment = segment_at(segments, cum_m)
            if segment is None:
                continue
            tags = segment_tags(segment, by_way) if by_way is not None else None
            if _is_separated(tags):
                continue
            found += 1
            record(
                segment.id,
                "rail_at_grade",
                f"crosses a railway at grade at {cum_m / 1000:.2f} km",
            )
    return found


def _water(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    frame: RouteFrame,
    line: Any,
    by_way: dict[int, dict[str, Any]] | None,
    record: Any,
) -> set[str] | None:
    """Segments where a watercourse meets the route unbridged, or None with no layer.

    Returns the segment ids whose confidence should drop: an inferred crossing is evidence
    about the map, not about the ground.
    """
    try:
        flowlines = ctx.layers.lines_crossing(route, FLOWLINES_LAYER)
    except (LayerNotFound, FileNotFoundError):
        return None

    touched: set[str] = set()
    for _, row in flowlines.iterrows():
        for point in _crossing_points(line, row.geometry):
            cum_m, _ = frame.locate(point.x, point.y)
            segment = segment_at(segments, cum_m)
            if segment is None:
                continue
            tags = segment_tags(segment, by_way) if by_way is not None else None
            if _is_separated(tags):
                continue
            named = str(row.get("gnis_name") or "an unnamed watercourse")
            where = f"{cum_m / 1000:.2f} km"
            if tags is not None and _set(tags, "ford"):
                record(segment.id, "confirmed_ford", f"tagged ford of {named} at {where}")
            else:
                touched.add(segment.id)
                record(
                    segment.id,
                    "possible_water_crossing",
                    f"{named} meets the route at {where} with no bridge tagged; "
                    f"NHD and OSM are separately digitised, so this may be culverted",
                )
    return touched


def _grids(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    frame: RouteFrame,
    record: Any,
) -> int | None:
    """Cattle grids within `POINT_RADIUS_M` of the line, or None when nodes are absent."""
    try:
        points = ctx.layers.points_in_corridor(
            corridor(route, buffer_m=POINT_RADIUS_M * 4), sorted(GRID_KINDS), layer=NODES_LAYER
        )
    except (LayerNotFound, FileNotFoundError):
        return None

    found = 0
    for _, row in points.iterrows():
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        cum_m, offset_m = frame.locate(geometry.x, geometry.y)
        if offset_m > POINT_RADIUS_M:
            continue
        segment = segment_at(segments, cum_m)
        if segment is None:
            continue
        found += 1
        record(segment.id, "cattle_grid", f"cattle grid at {cum_m / 1000:.2f} km")
    return found


def _snow(route: Route, segments: list[Segment], day: date, record: Any) -> set[str]:
    """Flag segments plausibly under snow; return those whose elevation is unknown."""
    unknown: set[str] = set()
    for segment in segments:
        elevations = [
            p.ele_m
            for p in route.points[segment.start_idx : segment.end_idx + 1]
            if p.ele_m is not None
        ]
        highest = max(elevations) if elevations else None
        verdict = snow_plausible(highest, day)
        if verdict is None:
            unknown.add(segment.id)
        elif verdict:
            record(
                segment.id,
                "seasonal_snow",
                f"{highest:.0f} m in {day.strftime('%B')}: lying snow is plausible",
            )
    return unknown


def _alerts(route: Route, ctx: ScorerContext, day: date, result: ScorerResult, record: Any) -> None:
    """NWS flood and red-flag warnings, recorded on the route rather than a segment.

    An alert is issued over a county or a fire-weather zone, so it belongs to the whole
    route. `arbitrate` gives a flag whose segment id it does not recognise the base
    position weight of 1.0, which is exactly right for a condition with no position.
    """
    from longrun.core.data.alerts import covers_day, route_alerts

    try:
        found = route_alerts(route, ctx, day)
    except Exception as exc:  # noqa: BLE001 - alerts must not take the scorer down
        result.coverage.append(
            CoverageEntry(
                source="nws_alerts",
                kind="warnings",
                checked=False,
                reason=f"alerts could not be read: {type(exc).__name__}",
            )
        )
        return

    if found.beyond_horizon:
        result.coverage.append(
            CoverageEntry(
                source="nws_alerts",
                kind="warnings",
                checked=False,
                reason=(
                    f"the run is more than {found.horizon_days} days out; NWS publishes "
                    f"active alerts, not forecasts of them"
                ),
            )
        )
        return

    if not found.answered:
        result.coverage.append(
            CoverageEntry(
                source="nws_alerts",
                kind="warnings",
                checked=False,
                reason="; ".join(found.reasons) or "no site answered",
            )
        )
        return

    for alert in found.all_alerts:
        # An alert whose window cannot be read is not placed. Raising it anyway would put a
        # flood warning on a day it may not touch.
        if covers_day(alert, day) is not True:
            continue
        code = "flood_warning" if alert.family == "flood" else "fire_weather_warning"
        record(ROUTE_SUMMARY_ID, code, f"{alert.event}: {alert.headline}")

    record_coverage(result, source="nws_alerts", kind="warnings")


score = hazards

__all__ = [
    "BRIDGE_ROAD_CLASSES",
    "CONFLATION_CONFIDENCE",
    "SEVERITY_BY_CODE",
    "SNOW_ELEVATION_M",
    "SNOW_MONTHS",
    "has_walkway",
    "hazards",
    "name",
    "score",
    "snow_plausible",
]
