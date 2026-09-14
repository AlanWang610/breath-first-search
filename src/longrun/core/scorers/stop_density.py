"""Signalized stops and gates per kilometre (scope 7.2, 8.3).

What is being measured is interruption: things that take the runner from moving to
standing. Signals and gates count; a marked crossing with no control does not, because it
does not reliably stop anyone.

Density is **local, not route-average**. A flag needs a segment to live on (`Flag` has a
`segment_id`), and a route-wide average of 3 stops/km cannot say *where*. So each segment
gets the density of a window centred on it: a 4 km run with every signal packed into the
first kilometre is flagged in that kilometre and clean afterwards, which is both true and
actionable — it is where the loop would try an alternative.

The soft threshold is `ctx.profile.stops_tolerance` and there is no hard one: scope 8.3
gives stop density no safety floor, and standing at a light is a comfort cost (scope 8.4
lists "stops" in the comfort tier), never a danger.

Nodes off the route are excluded by perpendicular distance, not by the corridor query. A
400 m corridor around an urban route contains every signal for two blocks either side, and
counting those would report a downtown grid's stop density instead of the route's.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from longrun.core.data.file_store import LayerNotFound
from longrun.core.geo.segments import corridor
from longrun.core.models.geometry import Route, Segment
from longrun.core.models.measurement import (
    Flag,
    FlagKind,
    ScorerResult,
    SegmentMeasurement,
    Tier,
)
from longrun.core.scorers._common import ROUTE_SUMMARY_ID, RouteFrame, row_tags, segment_at
from longrun.core.scorers.base import record_coverage, unavailable
from longrun.core.scorers.crossings import NODES_LAYER, SIGNAL_KINDS, is_signalized

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext

name = "stop_density"

#: Barrier and railway values that make a runner stop and deal with something.
GATE_KINDS = frozenset({"gate", "lift_gate", "swing_gate", "kissing_gate", "level_crossing"})

#: Amenity kinds queried: signals, plus everything that might turn out to be a gate.
STOP_KINDS: tuple[str, ...] = (*SIGNAL_KINDS, "gate", "lift_gate", "barrier")

#: How far off the line a node may be and still be on the runner's path. Matches the 15 m
#: tolerance `gpx_verify` check 2 uses for "on a routable way".
ON_ROUTE_M = 15.0

#: Window over which local density is measured. One kilometre, so that the number carries
#: the units the threshold is stated in without any rescaling to argue about.
WINDOW_M = 1000.0


def is_gate(tags: dict[str, Any]) -> bool:
    """Whether a node is a gate or a level crossing, from kind or from OSM tags."""
    if str(tags.get("kind", "")).strip().lower() in GATE_KINDS:
        return True
    if str(tags.get("barrier", "")).strip().lower() in GATE_KINDS:
        return True
    return str(tags.get("railway", "")).strip().lower() == "level_crossing"


def _stops_on_route(route: Route, ctx: ScorerContext, frame: RouteFrame) -> list[tuple[float, str]]:
    """`(cum_dist_m, "signal" | "gate")` for every stop actually on the line."""
    points = ctx.layers.points_in_corridor(corridor(route), list(STOP_KINDS), layer=NODES_LAYER)
    stops: list[tuple[float, str]] = []
    for _, row in points.iterrows():
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        tags = row_tags(row)
        if is_signalized(tags):
            kind = "signal"
        elif is_gate(tags):
            kind = "gate"
        else:
            continue
        cum_m, offset_m = frame.locate(geometry.x, geometry.y)
        if offset_m <= ON_ROUTE_M:
            stops.append((cum_m, kind))
    return stops


def window_density(positions: list[float], centre_m: float, length_m: float) -> tuple[float, float]:
    """`(stops per km, window length in m)` for a window centred on `centre_m`.

    The window keeps its full length and slides inward at the ends rather than being
    truncated, so a stop 200 m into a 300 m route is not divided by 800 m of pavement that
    does not exist — and neither is the first segment of a long route measured over half a
    window and reported at double density.
    """
    span = min(WINDOW_M, length_m)
    if span <= 0:
        return 0.0, 0.0
    low = min(max(centre_m - span / 2.0, 0.0), length_m - span)
    high = low + span
    count = sum(1 for position in positions if low <= position <= high)
    return count / (span / 1000.0), span


def stop_density(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
) -> ScorerResult:
    """Signalized stops and gates per km, flagged against `stops_tolerance` (scope 7.2)."""
    frame = RouteFrame(route)
    try:
        stops = _stops_on_route(route, ctx, frame)
    except LayerNotFound:
        return unavailable(name, "no nodes layer: stops not established")

    tolerance = float(ctx.profile.stops_tolerance.value)
    positions = [cum for cum, _ in stops]
    signals = sum(1 for _, kind in stops if kind == "signal")
    gates = len(stops) - signals

    per_segment: dict[str, dict[str, int]] = {s.id: {"signal": 0, "gate": 0} for s in segments}
    for cum, kind in stops:
        segment = segment_at(segments, cum)
        if segment is not None:
            per_segment[segment.id][kind] += 1

    result = ScorerResult(name=name)
    for segment in segments:
        centre = segment.cum_start_m + segment.length_m / 2.0
        density, span = window_density(positions, centre, route.length_m)
        counts = per_segment[segment.id]
        result.measurements.append(
            SegmentMeasurement(
                segment_id=segment.id,
                values={
                    "signals": counts["signal"],
                    "gates": counts["gate"],
                    "stops_per_km": density,
                    "window_m": span,
                },
            )
        )
        if density > tolerance:
            result.flags.append(
                Flag(
                    scorer=name,
                    segment_id=segment.id,
                    kind=FlagKind.SOFT,
                    tier=Tier.COMFORT,
                    severity=min(1.0, (density - tolerance) / max(tolerance, 1.0)),
                    reason_code="stops_per_km_above_tolerance",
                    detail=(
                        f"{density:.1f} stops/km over {span:.0f} m against a tolerance of "
                        f"{tolerance:.1f}"
                    ),
                )
            )

    km = route.length_m / 1000.0
    result.measurements.append(
        SegmentMeasurement(
            segment_id=ROUTE_SUMMARY_ID,
            values={
                "signals": signals,
                "gates": gates,
                "stops": len(stops),
                "stops_per_km": (len(stops) / km) if km > 0 else None,
                "stops_tolerance": tolerance,
            },
        )
    )

    record_coverage(
        result, source=NODES_LAYER, kind="osm_stops", vintage=ctx.layers.vintage(NODES_LAYER)
    )
    return result


#: The name the scorer registry loads. Aliased so a caller can dispatch on
#: `module.score` without knowing which scope tool this module implements.
score = stop_density
