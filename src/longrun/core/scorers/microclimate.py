"""Hourly weather where the runner will actually be (scope 7.4).

Scope §7.4 asks for "hourly forecast at multiple points along the route, **not a single
station**", and the emphasis is the whole point. A 50 km route out of San Francisco starts
in the marine layer and finishes somewhere twelve degrees warmer; a single airport reading
would describe neither end.

Measurement only, and it produces no flags. Scope §8.3's thresholds table has no
microclimate row — being warm is not a defect, and what the temperature *means* is
`heat_stress`'s question (WBGT, against a fixed floor) or the profile's (`sun.pivot_c`,
which decides whether shade is worth seeking). This module answers "what will it be like
here, then" and stops.

It is also the first scorer whose data comes from outside, so it is the first that can be
partly answered: eighteen sites of twenty-one is a real result, and the segments nearest
the three that failed carry lower confidence rather than the whole route being discarded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from longrun.core.data.forecast import DEFAULT_SPACING_M, route_forecast
from longrun.core.models.measurement import ScorerResult, SegmentMeasurement
from longrun.core.scorers._common import ROUTE_SUMMARY_ID
from longrun.core.scorers.base import unavailable

if TYPE_CHECKING:  # pragma: no cover
    from datetime import datetime

    from longrun.core.models.context import ScorerContext
    from longrun.core.models.geometry import Route, Segment

name = "microclimate"

#: Beyond this from the nearest site that answered, the reading is a neighbouring
#: microclimate rather than this one. Half the sampling interval, so a segment is never
#: more than one gap from a site that spoke.
FAR_FROM_A_SITE_M = DEFAULT_SPACING_M / 2.0

#: Confidence for a segment whose nearest answering site is further than that.
DISTANT_SITE_CONFIDENCE = 0.6


def microclimate(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
) -> ScorerResult:
    """Temperature, humidity, wind and cloud at each segment's arrival time."""
    if not etas:
        return unavailable(name, "no ETA vector: the forecast is only defined at a time")

    day = etas[0].date()
    forecast = route_forecast(route, ctx, day)

    result = ScorerResult(name=name)
    readings = forecast.for_segments(segments, etas)

    for segment, reading in zip(segments, readings, strict=True):
        centre = segment.cum_start_m + segment.length_m / 2.0
        gap = forecast.gap_to_nearest_m(centre)
        confidence = 1.0
        if reading is None:
            confidence = 0.0
        elif gap is not None and gap > FAR_FROM_A_SITE_M:
            confidence = DISTANT_SITE_CONFIDENCE

        result.measurements.append(
            SegmentMeasurement(
                segment_id=segment.id,
                values={
                    "temp_c": None if reading is None else reading.temp_c,
                    "relative_humidity_pct": (
                        None if reading is None else reading.relative_humidity_pct
                    ),
                    "wind_speed_ms": None if reading is None else reading.wind_speed_ms,
                    "cloud_cover_pct": None if reading is None else reading.cloud_cover_pct,
                    "precip_probability_pct": (
                        None if reading is None else reading.precip_probability_pct
                    ),
                    # Distance to the site that answered, so a reader can see when a
                    # reading is being borrowed from further away than it should be.
                    "nearest_site_m": gap,
                },
                confidence=confidence,
            )
        )

    temps = [r.temp_c for r in readings if r is not None and r.temp_c is not None]
    result.measurements.append(
        SegmentMeasurement(
            segment_id=ROUTE_SUMMARY_ID,
            values={
                "sites": len(forecast.sites),
                "sites_answered": sum(1 for s in forecast.sites if s.hours),
                "spacing_m": forecast.spacing_m,
                "min_temp_c": min(temps) if temps else None,
                "max_temp_c": max(temps) if temps else None,
                "providers": ", ".join(f"{k}:{v}" for k, v in sorted(forecast.providers.items())),
            },
            confidence=1.0 if forecast.answered else 0.0,
        )
    )

    result.coverage.extend(forecast.coverage())
    return result


score = microclimate

__all__ = ["microclimate", "name", "score"]
