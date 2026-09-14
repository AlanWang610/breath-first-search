"""AQI and PM2.5 along the corridor at arrival time (scope 7.4).

Measurement only. Scope §8.3's threshold table has no air-quality row, so nothing here
flags: what an AQI of 130 *means* for a given runner is not something this project has been
told, and inventing a threshold would be attaching a sign to a measurement — the one thing
scope §3.2 forbids a scorer to do.

The source is Open-Meteo rather than the AirNow the scope names, per ADR 0006, and the
coverage entry says so on every plan. That is not a footnote: AirNow reports what monitors
measured and Open-Meteo publishes a model, and a reader deciding whether to run on a smoky
day should know which they are looking at.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from longrun.core.data.air import route_air_quality
from longrun.core.models.measurement import ScorerResult, SegmentMeasurement
from longrun.core.scorers._common import ROUTE_SUMMARY_ID
from longrun.core.scorers.base import unavailable

if TYPE_CHECKING:  # pragma: no cover
    from datetime import datetime

    from longrun.core.models.context import ScorerContext
    from longrun.core.models.geometry import Route, Segment

name = "air_quality"


def air_quality(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
) -> ScorerResult:
    """US AQI and particulate concentrations per segment, at its arrival time."""
    if not etas:
        return unavailable(name, "no ETA vector: air quality is only defined at a time")

    air = route_air_quality(route, ctx, etas[0].date())
    result = ScorerResult(name=name)
    aqi_values: list[float] = []

    for segment in segments:
        when = etas[segment.start_idx] if segment.start_idx < len(etas) else etas[0]
        centre = segment.cum_start_m + segment.length_m / 2.0
        reading = air.at_distance(centre, when)
        if reading is not None and reading.us_aqi is not None:
            aqi_values.append(reading.us_aqi)

        result.measurements.append(
            SegmentMeasurement(
                segment_id=segment.id,
                values={
                    "us_aqi": None if reading is None else reading.us_aqi,
                    "pm2_5_ug_m3": None if reading is None else reading.pm2_5,
                    "pm10_ug_m3": None if reading is None else reading.pm10,
                },
                confidence=1.0 if reading is not None else 0.0,
            )
        )

    result.measurements.append(
        SegmentMeasurement(
            segment_id=ROUTE_SUMMARY_ID,
            values={
                "max_us_aqi": max(aqi_values) if aqi_values else None,
                "mean_us_aqi": (sum(aqi_values) / len(aqi_values)) if aqi_values else None,
                "sites": len(air.sites),
                "sites_answered": sum(1 for s in air.sites if s.hours),
                "source": "open-meteo (CAMS model)",
            },
            confidence=1.0 if air.answered else 0.0,
        )
    )
    result.coverage.extend(air.coverage())
    return result


score = air_quality

__all__ = ["air_quality", "name", "score"]
