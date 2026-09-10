"""What is actually open when you get there, in minutes (scope 7.5, 8.3).

`services_along` measures distance to the next water. This measures the thing a runner can
act on: **minutes**, at the hour they will arrive, counting only what is open then. A
fountain 400 m ahead and a café 400 m ahead are the same distance and not the same fact at
06:30.

Three deliberate choices.

**Unparseable opening hours are unknown, never closed.** OSM `opening_hours` is a small
language with a long tail — `Mo-Fr 07:00-19:00; Sa 08:00-17:00; PH off`, seasonal rules,
sunset-relative times. This parses the common forms and returns `None` for the rest, and a
`None` counts as *available* while lowering confidence. Treating an unrecognised string as
closed would manufacture dry gaps out of syntax this module simply does not speak.

**A tap with no hours at all is open.** A drinking fountain in a park usually carries no
`opening_hours` tag because it has none. Reading absence as closure is the same scope 12
mistake in a different coat.

**Thresholds scale down with heat.** Scope §8.3 says the dry-gap limits "both scale down
with WBGT" and does not say by how much. The choice here: no scaling at or below the 26 °C
soft threshold, halved at the 30 °C hard floor, linear between. Stated rather than tuned,
so that when it is wrong it is wrong somewhere legible.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from longrun.core.data.file_store import LayerNotFound
from longrun.core.models.coverage import CoverageEntry
from longrun.core.models.measurement import (
    Flag,
    FlagKind,
    ScorerResult,
    SegmentMeasurement,
    Tier,
)
from longrun.core.preferences.floors import DRY_GAP_HARD_MIN, WBGT_HARD_C
from longrun.core.scorers._common import ROUTE_SUMMARY_ID, RouteFrame, row_tags
from longrun.core.scorers.base import record_coverage, unavailable
from longrun.core.scorers.crossings import NODES_LAYER
from longrun.core.scorers.heat import WBGT_SOFT_C
from longrun.core.scorers.services import DEFAULT_BUFFER_M, all_kinds, category_of

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext
    from longrun.core.models.geometry import Route, Segment

name = "resupply_schedule"

#: Thresholds are halved by the time WBGT reaches the hard floor. See the module docstring:
#: scope 8.3 requires the scaling and does not specify it.
HEAT_SCALE_AT_FLOOR = 0.5

#: Millilitres per minute of running, for the carry suggestion. A middle-of-the-road
#: sweat rate; the profile's `carry_capacity_ml` is what decides whether it is enough.
ML_PER_MINUTE = 10.0

#: Confidence for a gap computed with at least one unparseable `opening_hours`.
UNKNOWN_HOURS_CONFIDENCE = 0.6

_DAYS = ("mo", "tu", "we", "th", "fr", "sa", "su")
_RULE = re.compile(
    r"^(?P<days>(?:[A-Za-z]{2}(?:-[A-Za-z]{2})?)(?:,[A-Za-z]{2}(?:-[A-Za-z]{2})?)*)?\s*"
    r"(?P<from>\d{1,2}:\d{2})\s*-\s*(?P<to>\d{1,2}:\d{2})$"
)


def _day_indices(spec: str) -> set[int]:
    days: set[int] = set()
    for part in spec.lower().split(","):
        if "-" in part:
            start, _, end = part.partition("-")
            if start in _DAYS and end in _DAYS:
                lo, hi = _DAYS.index(start), _DAYS.index(end)
                days.update(range(lo, hi + 1) if lo <= hi else [*range(lo, 7), *range(0, hi + 1)])
        elif part in _DAYS:
            days.add(_DAYS.index(part))
    return days


def _to_time(text: str) -> time | None:
    try:
        hour, _, minute = text.partition(":")
        return time(int(hour) % 24, int(minute))
    except ValueError:  # pragma: no cover - guarded by the regex
        return None


def is_open(spec: str | None, when: datetime) -> bool | None:
    """`True` open, `False` closed, `None` not understood — three answers.

    `None` is the load-bearing one: it means this module does not speak the dialect, not
    that the door is locked.
    """
    if spec is None:
        return True  # no tag at all: a public fountain has no hours
    text = spec.strip().lower()
    if not text:
        return True
    if text in {"24/7", "24x7", "open"}:
        return True
    if text in {"off", "closed"}:
        return False

    understood = False
    for rule in (r.strip() for r in text.split(";") if r.strip()):
        match = _RULE.match(rule)
        if match is None:
            continue
        understood = True
        days = _day_indices(match.group("days") or "mo-su")
        if days and when.weekday() not in days:
            continue
        start, end = _to_time(match.group("from")), _to_time(match.group("to"))
        if start is None or end is None:
            continue
        now = when.time()
        inside = start <= now <= end if start <= end else (now >= start or now <= end)
        if inside:
            return True
    return False if understood else None


def heat_scaled(threshold_min: float, wbgt: float | None) -> float:
    """Shrink a dry-gap threshold as WBGT rises (scope 8.3)."""
    if wbgt is None or wbgt <= WBGT_SOFT_C:
        return threshold_min
    span = WBGT_HARD_C - WBGT_SOFT_C
    over = min(max((wbgt - WBGT_SOFT_C) / span, 0.0), 1.0) if span > 0 else 1.0
    return threshold_min * (1.0 - over * (1.0 - HEAT_SCALE_AT_FLOOR))


def _worst_wbgt(prior: list[ScorerResult] | None) -> float | None:
    if not prior:
        return None
    for result in prior:
        if result.name != "heat_stress":
            continue
        for measurement in result.measurements:
            if measurement.segment_id == ROUTE_SUMMARY_ID:
                value = measurement.values.get("max_wbgt_c")
                return float(value) if isinstance(value, int | float) else None
    return None


def _minutes_at(route: Route, etas: list[datetime], cum_m: float) -> float:
    """Elapsed minutes from the start at a distance along the route."""
    for index, point in enumerate(route.points):
        if point.cum_dist_m >= cum_m:
            return (etas[index] - etas[0]).total_seconds() / 60.0
    return (etas[-1] - etas[0]).total_seconds() / 60.0


def resupply_schedule(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
    prior: list[ScorerResult] | None = None,
) -> ScorerResult:
    """Open services at arrival, the longest dry and toilet gaps, and a carry suggestion."""
    if not etas:
        return unavailable(name, "no ETA vector: opening hours are only defined at a time")

    from longrun.core.geo.segments import corridor

    try:
        points = ctx.layers.points_in_corridor(
            corridor(route, buffer_m=DEFAULT_BUFFER_M), all_kinds()
        )
    except (LayerNotFound, FileNotFoundError) as exc:
        return unavailable(name, f"amenity layer unavailable: {exc}")

    frame = RouteFrame(route)
    open_at: dict[str, list[float]] = {"water": [], "toilet": [], "food": []}
    unknown_hours = 0

    for _, row in points.iterrows():
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        tags = row_tags(row)
        category = category_of(tags)
        if category is None:
            continue
        cum_m, offset_m = frame.locate(geometry.x, geometry.y)
        if offset_m > DEFAULT_BUFFER_M:
            continue

        minutes = _minutes_at(route, etas, cum_m)
        state = is_open(tags.get("opening_hours"), etas[0] + timedelta(minutes=minutes))
        if state is None:
            unknown_hours += 1
        if state is not False:
            open_at[category].append(minutes)

    total_min = (etas[-1] - etas[0]).total_seconds() / 60.0
    wbgt = _worst_wbgt(prior)
    water_soft = heat_scaled(float(ctx.profile.water_gap_max_min.value), wbgt)
    water_hard = heat_scaled(DRY_GAP_HARD_MIN, wbgt)
    toilet_soft = heat_scaled(float(ctx.profile.toilet_gap_max_min.value), wbgt)

    gaps = {category: _max_gap_min(sorted(marks), total_min) for category, marks in open_at.items()}
    confidence = UNKNOWN_HOURS_CONFIDENCE if unknown_hours else 1.0

    result = ScorerResult(name=name)
    for segment in segments:
        start_min = _minutes_at(route, etas, segment.cum_start_m)
        result.measurements.append(
            SegmentMeasurement(
                segment_id=segment.id,
                values={
                    "minutes_to_open_water": _next_ahead(open_at["water"], start_min),
                    "minutes_to_open_toilet": _next_ahead(open_at["toilet"], start_min),
                    "minutes_to_open_food": _next_ahead(open_at["food"], start_min),
                },
                confidence=confidence,
            )
        )

    carry_ml = gaps["water"] * ML_PER_MINUTE
    result.measurements.append(
        SegmentMeasurement(
            segment_id=ROUTE_SUMMARY_ID,
            values={
                "max_dry_gap_min": gaps["water"],
                "max_toilet_gap_min": gaps["toilet"],
                "max_food_gap_min": gaps["food"],
                "suggested_carry_ml": carry_ml,
                "carry_capacity_ml": float(ctx.profile.carry_capacity_ml.value),
                "water_soft_threshold_min": water_soft,
                "water_hard_threshold_min": water_hard,
                "wbgt_used_c": wbgt,
                "services_with_unreadable_hours": unknown_hours,
            },
            confidence=confidence,
        )
    )

    _flag_gaps(result, segments, gaps, water_soft, water_hard, toilet_soft, wbgt)

    record_coverage(
        result, source=NODES_LAYER, kind="opening_hours", vintage=ctx.layers.vintage(NODES_LAYER)
    )
    if unknown_hours:
        result.coverage.append(
            CoverageEntry(
                source=NODES_LAYER,
                kind="opening_hours",
                checked=False,
                reason=(
                    f"{unknown_hours} service(s) carry opening_hours this parser does not "
                    "read; counted as available rather than closed"
                ),
            )
        )
    return result


def _max_gap_min(marks: list[float], total_min: float) -> float:
    """Longest stretch with nothing open ahead, start and finish included."""
    bounds = [0.0, *marks, total_min]
    return max(b - a for a, b in zip(bounds[:-1], bounds[1:], strict=True))


def _next_ahead(marks: list[float], from_min: float) -> float | None:
    ahead = [m for m in marks if m >= from_min]
    return min(ahead) - from_min if ahead else None


def _flag_gaps(
    result: ScorerResult,
    segments: list[Segment],
    gaps: dict[str, float],
    water_soft: float,
    water_hard: float,
    toilet_soft: float,
    wbgt: float | None,
) -> None:
    """One flag per exceeded gap, on the segment the gap starts in.

    Scope 8.4 puts the dry gap in the physiological tier — the same tier as heat, and for
    the same reason.
    """
    if not segments:
        return
    heat_note = f" (thresholds scaled for WBGT {wbgt:.1f} C)" if wbgt else ""

    if gaps["water"] > water_soft:
        hard = gaps["water"] > water_hard
        result.flags.append(
            Flag(
                scorer=name,
                segment_id=segments[0].id,
                kind=FlagKind.HARD if hard else FlagKind.SOFT,
                tier=Tier.PHYSIOLOGICAL,
                severity=min(gaps["water"] / max(water_hard, 1.0), 1.0),
                reason_code="dry_gap_above_hard_floor" if hard else "dry_gap_above_tolerance",
                detail=(
                    f"{gaps['water']:.0f} min without open water against "
                    f"{water_soft:.0f} min{heat_note}"
                ),
            )
        )
    if gaps["toilet"] > toilet_soft:
        result.flags.append(
            Flag(
                scorer=name,
                segment_id=segments[0].id,
                kind=FlagKind.SOFT,
                tier=Tier.COMFORT,
                severity=min(gaps["toilet"] / max(toilet_soft * 2.0, 1.0), 1.0),
                reason_code="toilet_gap_above_tolerance",
                detail=f"{gaps['toilet']:.0f} min without an open toilet",
            )
        )


score = resupply_schedule

__all__ = [
    "HEAT_SCALE_AT_FLOOR",
    "ML_PER_MINUTE",
    "heat_scaled",
    "is_open",
    "name",
    "resupply_schedule",
    "score",
]
