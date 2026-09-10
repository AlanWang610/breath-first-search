"""Daylight at arrival, and street lighting where there is none (scope 7.4, 8.3).

Two halves with different characters, and keeping them apart is the point. Whether the sun
is up is **arithmetic** — solar geometry, reproducible to arcseconds from a timestamp and a
coordinate, no forecast and no cassette. Whether a dark street is *lit* is an **OSM tag**,
and therefore frequently unknown.

Scope §8.3's flag is precise about that: "segment in darkness with `lit=no` (profile)". Not
"without `lit=yes`". A way with no `lit` tag has told us nothing, and treating silence as
absence would flag most of the rural US for having unlit roads that may well be lit — the
scope 12 rule, in the one place it is easiest to get wrong. So an untagged dark segment
lowers confidence and appears in coverage; it does not raise a flag.

Darkness itself is measured to **civil twilight**, not to geometric sunset. A runner has
usable light at four degrees below the horizon, and calling that darkness would flag half
of every evening run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from longrun.core.data.file_store import LayerNotFound
from longrun.core.geo.solar import (
    CIVIL_TWILIGHT_DEG,
    solar_positions,
    utc_offset_from_longitude,
)
from longrun.core.models.coverage import CoverageEntry
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
    from datetime import datetime

    from longrun.core.models.context import ScorerContext
    from longrun.core.models.geometry import Route, Segment

name = "lighting"

#: `lit` values that positively assert there is no street lighting. Anything else — and
#: especially the absence of the tag — is unknown, not "no".
LIT_NO = frozenset({"no", "none", "0", "disused"})

#: `lit` values that assert there is.
LIT_YES = frozenset({"yes", "24/7", "automatic", "limited", "sunset-sunrise"})

#: Confidence for a dark segment whose way carries no `lit` tag at all.
UNKNOWN_LIT_CONFIDENCE = 0.4

#: A fully dark, positively unlit segment is the worst case this scorer reports.
FULL_SEVERITY = 1.0


def lit_state(tags: dict[str, Any] | None) -> bool | None:
    """`True` lit, `False` unlit, `None` unknown — three answers, not two."""
    if not tags:
        return None
    value = str(tags.get("lit", "")).strip().lower()
    if not value:
        return None
    if value in LIT_NO:
        return False
    if value in LIT_YES:
        return True
    return True  # an interval like "22:00-05:00" is still a claim that lighting exists


def lighting(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
) -> ScorerResult:
    """Daylight status per segment, and street lighting where it is dark."""
    if not etas or len(etas) != len(route.points):
        return unavailable(name, "no ETA vector: daylight is only defined at a time")

    offset = ctx.utc_offset_hours
    guessed = offset is None
    if offset is None:
        offset = utc_offset_from_longitude(route.points[0].lon)

    position = solar_positions(etas, route.points[0].lat, route.points[0].lon, offset)
    daylight = position.is_daylight

    try:
        by_way = way_tags_in_corridor(route, ctx)
        tags_available = True
    except (LayerNotFound, FileNotFoundError):
        by_way, tags_available = {}, False

    result = ScorerResult(name=name)
    tolerated = bool(ctx.profile.darkness_tolerance.value)
    dark_m = 0.0
    unlit_m = 0.0
    unknown_m = 0.0

    for segment in segments:
        lo, hi = segment.start_idx, segment.end_idx
        dark_fraction = float(1.0 - np.mean(daylight[lo : hi + 1].astype(float)))
        elevation_deg = float(np.degrees(np.mean(position.elevation[lo : hi + 1])))
        tags = segment_tags(segment, by_way) if tags_available else None
        lit = lit_state(tags)

        if dark_fraction > 0:
            dark_m += segment.length_m * dark_fraction
            if lit is False:
                unlit_m += segment.length_m * dark_fraction
            elif lit is None:
                unknown_m += segment.length_m * dark_fraction

        confidence = 1.0
        if dark_fraction > 0 and lit is None:
            confidence = UNKNOWN_LIT_CONFIDENCE if tags_available else UNKNOWN_WAY_CONFIDENCE

        result.measurements.append(
            SegmentMeasurement(
                segment_id=segment.id,
                values={
                    "dark_fraction": dark_fraction,
                    "sun_elevation_deg": elevation_deg,
                    "lit": lit,
                },
                confidence=confidence,
            )
        )

        # Scope 8.3: darkness *with `lit=no`*. An untagged way has said nothing, and the
        # profile's `darkness_tolerance` decides whether even a known-unlit stretch counts.
        if dark_fraction > 0 and lit is False and not tolerated:
            result.flags.append(
                Flag(
                    scorer=name,
                    segment_id=segment.id,
                    kind=FlagKind.SOFT,
                    tier=Tier.COMFORT,
                    severity=min(dark_fraction, FULL_SEVERITY),
                    reason_code="unlit_in_darkness",
                    detail=(
                        f"{dark_fraction:.0%} of this segment is run after civil twilight "
                        f"on a way tagged lit=no (sun {elevation_deg:.0f} deg)"
                    ),
                )
            )

    result.measurements.append(
        SegmentMeasurement(
            segment_id=ROUTE_SUMMARY_ID,
            values={
                "dark_m": dark_m,
                "unlit_dark_m": unlit_m,
                "unknown_lit_dark_m": unknown_m,
                "twilight_threshold_deg": CIVIL_TWILIGHT_DEG,
                "utc_offset_hours": offset,
                "utc_offset_guessed": guessed,
                "starts_in_daylight": bool(daylight[0]),
                "finishes_in_daylight": bool(daylight[-1]),
            },
            confidence=1.0,
        )
    )

    if tags_available:
        record_coverage(
            result, source=WAYS_LAYER, kind="lit_tags", vintage=ctx.layers.vintage(WAYS_LAYER)
        )
        if unknown_m > 0:
            result.coverage.append(
                CoverageEntry(
                    source=WAYS_LAYER,
                    kind="lit_tags",
                    checked=False,
                    reason=(
                        f"{unknown_m / 1000:.1f} km run in darkness on ways with no lit tag: "
                        "unknown, not unlit"
                    ),
                )
            )
    else:
        result.coverage.append(
            CoverageEntry(
                source=WAYS_LAYER,
                kind="lit_tags",
                checked=False,
                reason="no ways layer: street lighting not established",
            )
        )
    result.coverage.append(
        CoverageEntry(
            source="pvlib",
            kind="solar_geometry",
            checked=True,
            reason=(
                f"UTC offset derived from longitude ({offset:+.0f} h)"
                if guessed
                else f"UTC offset {offset:+.0f} h as given"
            ),
            confidence=0.7 if guessed else 1.0,
        )
    )
    return result


score = lighting

__all__ = [
    "LIT_NO",
    "LIT_YES",
    "UNKNOWN_LIT_CONFIDENCE",
    "lighting",
    "lit_state",
    "name",
    "score",
]
