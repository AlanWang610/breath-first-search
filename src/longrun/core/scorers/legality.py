"""Pedestrian-prohibited segments (scope 7.6, 7.9 check 5).

This is the substance of `gpx_verify` check 5 and of the router's hard-exclude list
(scope 7.1): `access=private`, `foot=no`, `highway=motorway`, railway right-of-way. Every
flag here is HARD and SAFETY — there is no soft legality, no profile axis that trades it
away, and no position weight (`core.plan.weighting` excludes this scorer by name, because
a segment is no more legal at kilometre 2 than at kilometre 80).

Two rules make this more than a tag lookup:

* **A mode tag beats a general one.** `access=private` with `foot=yes` is a driveway you
  may walk; flagging it would send the loop rerouting around legal paths, and after a few
  such flags a user stops believing the ones that are real.
* **A missing tag proves nothing** (scope 12). An untagged way is not an open one and not
  a closed one; it produces a measurement with lowered confidence and no flag. Absence of
  evidence reaching the plan sheet as a hard fail is the failure mode that would make
  every rural route unroutable.

At most one flag per segment, by the precedence in `CHECKS`. A motorway ramp that is also
`foot=no` is one problem, not two, and counting it twice would distort the per-tier
weighted sum in `core.plan.arbitrate`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from longrun.core.data.file_store import LayerNotFound
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
    UNKNOWN_WAY_CONFIDENCE,
    WAYS_LAYER,
    segment_tags,
    way_tags_in_corridor,
)
from longrun.core.scorers.base import record_coverage, unavailable

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext

name = "legality"

#: Values of `foot` that grant passage whatever a general `access` tag says.
FOOT_ALLOWED = frozenset({"yes", "designated", "permissive", "destination", "official"})

#: Values of `foot` that withdraw it.
FOOT_DENIED = frozenset({"no", "private"})

#: Motorways and their ramps: pedestrian-prohibited in every US state.
MOTORWAY_CLASSES = frozenset({"motorway", "motorway_link"})

#: Live railway right-of-way. Deliberately excludes `disused`, `abandoned` and
#: `razed`, which are overwhelmingly rail-trails and are exactly where a long run wants
#: to be.
RAILWAY_ROW = frozenset(
    {"rail", "light_rail", "subway", "tram", "narrow_gauge", "funicular", "monorail", "preserved"}
)

#: `access` values that prohibit entry absent a mode tag saying otherwise.
ACCESS_DENIED: dict[str, str] = {"private": "access_private", "no": "access_no"}

#: A hard flag is a hard flag; severity carries no extra information here, and scope 8.3
#: lists legality as "any" rather than as a graded threshold.
LEGALITY_SEVERITY = 1.0


def _foot(tags: dict[str, Any]) -> str | None:
    value = tags.get("foot")
    return str(value).strip().lower() if value is not None else None


def _highway(tags: dict[str, Any]) -> str | None:
    value = tags.get("highway")
    return str(value).strip().lower() if value is not None else None


def _check_motorway(tags: dict[str, Any]) -> str | None:
    return "motorway" if _highway(tags) in MOTORWAY_CLASSES else None


def _check_railway(tags: dict[str, Any]) -> str | None:
    """A live railway is only walkable where the way is also a road or path.

    Street trackage (`railway=tram` on a `highway=*` way) and level crossings are shared
    surfaces; a bare `railway=rail` linestring is trespass.
    """
    railway = tags.get("railway")
    if railway is None or str(railway).strip().lower() not in RAILWAY_ROW:
        return None
    if _highway(tags) is not None or _foot(tags) in FOOT_ALLOWED:
        return None
    return "railway_row"


def _check_foot(tags: dict[str, Any]) -> str | None:
    foot = _foot(tags)
    if foot in FOOT_DENIED:
        return "foot_private" if foot == "private" else "foot_no"
    return None


def _check_access(tags: dict[str, Any]) -> str | None:
    """`access` restrictions, unless a `foot` tag grants passage back."""
    if _foot(tags) in FOOT_ALLOWED:
        return None
    value = tags.get("access")
    if value is None:
        return None
    return ACCESS_DENIED.get(str(value).strip().lower())


#: Checked in order; the first hit is the segment's one flag.
CHECKS: tuple[Callable[[dict[str, Any]], str | None], ...] = (
    _check_motorway,
    _check_railway,
    _check_foot,
    _check_access,
)

#: Prose for the plan sheet. Tests assert on the keys, never on these strings.
DETAIL: dict[str, str] = {
    "motorway": "motorway or motorway ramp: pedestrians prohibited",
    "railway_row": "live railway right-of-way with no road or path on it",
    "foot_no": "tagged foot=no",
    "foot_private": "tagged foot=private",
    "access_private": "tagged access=private with no foot access granted",
    "access_no": "tagged access=no with no foot access granted",
}


def violation_of(tags: dict[str, Any]) -> str | None:
    """The reason code for a way's tags, or None when nothing prohibits walking."""
    for check in CHECKS:
        code = check(tags)
        if code is not None:
            return code
    return None


def legality(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
) -> ScorerResult:
    """Hard flags for every segment a pedestrian may not legally be on (scope 7.6)."""
    try:
        by_way = way_tags_in_corridor(route, ctx)
    except LayerNotFound:
        return unavailable(name, "no ways layer: pedestrian access not established")

    result = ScorerResult(name=name)
    prohibited_m = 0.0
    unknown_m = 0.0

    for segment in segments:
        tags = segment_tags(segment, by_way)
        if tags is None:
            unknown_m += segment.length_m
            result.measurements.append(
                SegmentMeasurement(
                    segment_id=segment.id,
                    values={"prohibited": None, "highway": None, "access": None, "foot": None},
                    confidence=UNKNOWN_WAY_CONFIDENCE,
                )
            )
            continue

        code = violation_of(tags)
        result.measurements.append(
            SegmentMeasurement(
                segment_id=segment.id,
                values={
                    "prohibited": code is not None,
                    "reason_code": code,
                    "highway": _highway(tags),
                    "access": tags.get("access"),
                    "foot": _foot(tags),
                    "railway": tags.get("railway"),
                },
                confidence=1.0,
            )
        )
        if code is None:
            continue

        prohibited_m += segment.length_m
        result.flags.append(
            Flag(
                scorer=name,
                segment_id=segment.id,
                kind=FlagKind.HARD,
                tier=Tier.SAFETY,
                severity=LEGALITY_SEVERITY,
                reason_code=code,
                detail=DETAIL.get(code),
            )
        )

    result.measurements.append(
        SegmentMeasurement(
            segment_id=ROUTE_SUMMARY_ID,
            values={
                "prohibited_m": prohibited_m,
                "unknown_m": unknown_m,
                "prohibited_segments": len(result.flags),
                "length_m": route.length_m,
            },
        )
    )

    record_coverage(
        result, source=WAYS_LAYER, kind="osm_access_tags", vintage=ctx.layers.vintage(WAYS_LAYER)
    )
    return result


#: The name the scorer registry loads. Aliased so a caller can dispatch on
#: `module.score` without knowing which scope tool this module implements.
score = legality
