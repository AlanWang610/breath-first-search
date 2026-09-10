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

**Coverage is returned, not published.** Scope 4.1 makes every scorer a pure function of
its inputs, so none of these mutate `ctx.coverage`; the scoring loop copies the returned
entries into the plan-wide manifest (`base.publish` is there for exactly that). A scorer
that published its own would double every line of the sheet's coverage section when the
loop copies them too.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from longrun.core.data.file_store import LayerNotFound
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
from longrun.core.scorers._common import (
    ROUTE_SUMMARY_ID,
    UNKNOWN_WAY_CONFIDENCE,
    WAYS_LAYER,
    aadt_of,
    segment_tags,
    way_tags_in_corridor,
)
from longrun.core.scorers.base import record_coverage, unavailable

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import ScorerContext

name = "segment_hostility"

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
