"""Heat stress as WBGT, against a floor the user cannot lower (scope 7.4, 8.3).

Scope §7.4 says "WBGT **or** UTCI" and picks neither, and the document contains no formula
for either. ADR 0004 records the choice; the short version is that WBGT is what §8.3's
thresholds are already stated in — soft above 26 °C, hard above 30 °C — and switching index
would orphan two numbers the scope commits to.

**The formula is the ACSM / Australian Bureau of Meteorology approximation:**

    WBGT = 0.567 Ta + 0.393 e + 3.94        e = RH/100 x 6.105 exp(17.27 Ta / (237.7 + Ta))

and its limitation is the important part: **it takes temperature and humidity only.** It
has no term for solar load and none for wind, so on an exposed road in full sun it is a
*lower bound* on what a runner meets. This module therefore does two things rather than
pretend otherwise — it reports the sunlit fraction beside the number, and it lowers
confidence on segments that `sun_exposure` found exposed. A reader is told the figure is
conservative; a reader is never handed a number that quietly is not.

**The threshold is a floor, not a preference.** Scope §6.3 puts WBGT among the safety
floors "outside the profile and not lowerable", and `floors.WBGT_HARD_C` has existed since
M1.3 with nothing reading it. This is what reads it. The *tier* is physiological rather
than safety (§8.4) — which is not a contradiction: the tier says what kind of harm it is,
the floor says which threshold was crossed.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from longrun.core.data.forecast import route_forecast
from longrun.core.models.measurement import (
    Flag,
    FlagKind,
    ScorerResult,
    SegmentMeasurement,
    Tier,
)
from longrun.core.preferences.floors import WBGT_HARD_C
from longrun.core.scorers._common import ROUTE_SUMMARY_ID
from longrun.core.scorers.base import unavailable

if TYPE_CHECKING:  # pragma: no cover
    from datetime import datetime

    from longrun.core.models.context import ScorerContext
    from longrun.core.models.geometry import Route, Segment

name = "heat_stress"

#: Scope 8.3's soft threshold. Not marked "(profile)" there, so it is fixed like the hard
#: one: heat is physiology, and scope 6.3 keeps it out of the preference model entirely.
WBGT_SOFT_C = 26.0

#: Magnus coefficients for saturation vapour pressure over water, hPa.
MAGNUS_A = 6.105
MAGNUS_B = 17.27
MAGNUS_C = 237.7

#: Confidence for a segment the sun is on, where a humidity-only WBGT under-reports.
SUNLIT_CONFIDENCE = 0.6

#: Above this sunlit fraction a segment is treated as exposed for that purpose.
EXPOSED_FRACTION = 0.5


def vapour_pressure_hpa(temp_c: float, relative_humidity_pct: float) -> float:
    """Partial pressure of water vapour, by the Magnus formula."""
    saturation = MAGNUS_A * math.exp(MAGNUS_B * temp_c / (MAGNUS_C + temp_c))
    return saturation * min(max(relative_humidity_pct, 0.0), 100.0) / 100.0


def wbgt_c(temp_c: float, relative_humidity_pct: float) -> float:
    """Wet bulb globe temperature, ACSM / BoM approximation.

    Temperature and humidity only — see the module docstring. Callers report the sunlit
    fraction alongside rather than folding an unverified solar term into this number.
    """
    return 0.567 * temp_c + 0.393 * vapour_pressure_hpa(temp_c, relative_humidity_pct) + 3.94


def severity_for(wbgt: float) -> float:
    """0 at the soft threshold, 1.0 at the hard one, saturating above.

    Linear between the two numbers scope 8.3 states, so a segment at 28 C reads as half
    way to a hard flag rather than as an arbitrary curve.
    """
    span = WBGT_HARD_C - WBGT_SOFT_C
    return min(max((wbgt - WBGT_SOFT_C) / span, 0.0), 1.0) if span > 0 else 0.0


def _sunlit_by_segment(results: list[ScorerResult] | None) -> dict[str, float]:
    """Sunlit fraction per segment, if `sun_exposure` ran. Empty when it did not."""
    if not results:
        return {}
    for result in results:
        if result.name != "sun_exposure":
            continue
        return {
            m.segment_id: 1.0 - float(m.values.get("shaded_fraction") or 0.0)
            for m in result.measurements
            if m.values.get("shaded_fraction") is not None
        }
    return {}


def heat_stress(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
    prior: list[ScorerResult] | None = None,
) -> ScorerResult:
    """WBGT per segment at its arrival time, flagged against the fixed floors."""
    if not etas:
        return unavailable(name, "no ETA vector: heat is only defined at a time")

    forecast = route_forecast(route, ctx, etas[0].date())
    if not forecast.answered:
        return unavailable(
            name,
            "no forecast: WBGT needs temperature and humidity, and neither may be assumed",
            kind="forecast",
        )

    sunlit = _sunlit_by_segment(prior)
    readings = forecast.for_segments(segments, etas)
    result = ScorerResult(name=name)
    values: list[float] = []

    for segment, reading in zip(segments, readings, strict=True):
        temp = None if reading is None else reading.temp_c
        humidity = None if reading is None else reading.relative_humidity_pct
        exposure = sunlit.get(segment.id)

        if temp is None or humidity is None:
            result.measurements.append(
                SegmentMeasurement(
                    segment_id=segment.id,
                    values={"wbgt_c": None, "temp_c": temp, "sunlit_fraction": exposure},
                    confidence=0.0,
                )
            )
            continue

        wbgt = wbgt_c(temp, humidity)
        values.append(wbgt)
        confidence = 1.0
        if exposure is not None and exposure >= EXPOSED_FRACTION:
            confidence = SUNLIT_CONFIDENCE

        result.measurements.append(
            SegmentMeasurement(
                segment_id=segment.id,
                values={
                    "wbgt_c": wbgt,
                    "temp_c": temp,
                    "relative_humidity_pct": humidity,
                    "wind_speed_ms": reading.wind_speed_ms if reading else None,
                    "sunlit_fraction": exposure,
                },
                confidence=confidence,
            )
        )

        if wbgt <= WBGT_SOFT_C:
            continue
        hard = wbgt > WBGT_HARD_C
        result.flags.append(
            Flag(
                scorer=name,
                segment_id=segment.id,
                kind=FlagKind.HARD if hard else FlagKind.SOFT,
                # Physiological, not safety: scope 8.4 puts heat in the second tier. The
                # floor decides which threshold was crossed, the tier what kind of harm.
                tier=Tier.PHYSIOLOGICAL,
                severity=severity_for(wbgt),
                reason_code="wbgt_above_hard_floor" if hard else "wbgt_above_soft_threshold",
                detail=(
                    f"WBGT {wbgt:.1f} C at {temp:.0f} C / {humidity:.0f}% RH"
                    + (
                        f", {exposure:.0%} sunlit - a humidity-only WBGT understates this"
                        if exposure is not None and exposure >= EXPOSED_FRACTION
                        else ""
                    )
                ),
            )
        )

    result.measurements.append(
        SegmentMeasurement(
            segment_id=ROUTE_SUMMARY_ID,
            values={
                "max_wbgt_c": max(values) if values else None,
                "mean_wbgt_c": (sum(values) / len(values)) if values else None,
                "soft_threshold_c": WBGT_SOFT_C,
                "hard_floor_c": WBGT_HARD_C,
                "formula": "ACSM/BoM approximation: temperature and humidity only",
                "segments_above_soft": sum(1 for v in values if v > WBGT_SOFT_C),
            },
            confidence=1.0,
        )
    )
    result.coverage.extend(forecast.coverage())
    return result


score = heat_stress

__all__ = [
    "EXPOSED_FRACTION",
    "SUNLIT_CONFIDENCE",
    "WBGT_SOFT_C",
    "heat_stress",
    "name",
    "score",
    "severity_for",
    "vapour_pressure_hpa",
    "wbgt_c",
]
