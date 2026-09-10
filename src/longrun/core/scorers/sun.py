"""Direct and diffuse irradiance at arrival time, and shaded fraction (scope 7.4).

Scope §7.4 is unusually specific about this one: *"per-km direct + diffuse irradiance
integrated at arrival times; direct via DSM ray-cast, diffuse via sky view factor;
cloud-scaled. Output J/m² and shaded fraction."* Four inputs, and this module is where they
meet:

* `core.geo.svf.corridor_horizons` — the skyline at every route point, from the DSM.
* `core.geo.solar` — where the sun is, and what a cloudless sky would deliver.
* `core.data.forecast` — how much cloud is actually in the way.
* `raycast.is_sunlit` — whether that skyline blocks that sun at that moment.

**Measurement only.** Scope §3 is explicit that sun is not assumed bad: with both `sun`
weights at zero — the shipped default — shade is *reported and not costed*, and this
produces no flags at all. A flag appears only when the user has said they care, and then
which way it points depends on the temperature at the ETA against their own `pivot_c`.
Heat is a separate question with a fixed floor, and it lives in `heat_stress`.

**Diffuse survives shade.** A runner in a building's shadow is still under an open sky
above them, so `direct` is zeroed by the skyline while `diffuse` is scaled by the sky view
factor instead. Treating shade as "no sun at all" would understate exposure on a
half-open street by most of what is actually falling on the runner — which is exactly the
error a route planned for a hot afternoon cannot afford.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from longrun.core.data.forecast import route_forecast
from longrun.core.geo.raycast import is_sunlit
from longrun.core.geo.solar import (
    ClearSky,
    SolarPosition,
    clear_sky,
    solar_positions,
    utc_offset_from_longitude,
)
from longrun.core.geo.svf import corridor_horizons
from longrun.core.models.coverage import CoverageEntry
from longrun.core.models.measurement import (
    Flag,
    FlagKind,
    ScorerResult,
    SegmentMeasurement,
    Tier,
)
from longrun.core.scorers._common import ROUTE_SUMMARY_ID
from longrun.core.scorers.base import unavailable

if TYPE_CHECKING:  # pragma: no cover
    from datetime import datetime

    from longrun.core.data.forecast import RouteForecast
    from longrun.core.geo.svf import CorridorHorizons
    from longrun.core.models.context import ScorerContext
    from longrun.core.models.geometry import Route, Segment

name = "sun_exposure"

#: Kasten & Czeplak (1980): global irradiance under cloud fraction `c` is roughly
#: `1 - 0.75 c^3.4` of its clear-sky value. Thin high cloud barely dims the sky; the last
#: tenth of cover takes most of what is left.
CLOUD_GLOBAL_EXPONENT = 3.4
CLOUD_GLOBAL_COEFFICIENT = 0.75

#: A segment this exposed, against a stated preference, is worth a flag at full severity.
FULL_EXPOSURE = 1.0

#: Confidence when the surface model was incomplete — `DsmCoverage` decides the number.
#: Named here so the scorer's own degradation and the DSM's are not conflated.
NO_FORECAST_CONFIDENCE = 0.5


def _cloud_factors(cloud_pct: float | None) -> tuple[float, float]:
    """`(beam factor, global factor)` for a cloud cover percentage.

    Overcast removes the beam almost entirely while the sky stays bright, so the two
    scale differently — using one factor for both would either keep a beam that is not
    there or throw away diffuse that is.
    """
    if cloud_pct is None:
        return 1.0, 1.0
    fraction = min(max(cloud_pct / 100.0, 0.0), 1.0)
    beam = 1.0 - fraction
    glob = 1.0 - CLOUD_GLOBAL_COEFFICIENT * fraction**CLOUD_GLOBAL_EXPONENT
    return beam, glob


def sun_exposure(
    route: Route,
    segments: list[Segment],
    ctx: ScorerContext,
    etas: list[datetime] | None = None,
) -> ScorerResult:
    """Irradiance and shade per segment, at the time the runner gets there."""
    if not etas or len(etas) != len(route.points):
        return unavailable(name, "no ETA vector: irradiance is only defined at a time")

    horizons = corridor_horizons(route, ctx)
    if not horizons.coverage.answered:
        return unavailable(
            name, f"no surface model: {horizons.coverage.describe()}", kind="surface_model"
        )

    offset = ctx.utc_offset_hours
    guessed = offset is None
    if offset is None:
        offset = utc_offset_from_longitude(route.points[0].lon)

    lat = route.points[0].lat
    lon = route.points[0].lon
    position = solar_positions(etas, lat, lon, offset)
    sky = clear_sky(etas, lat, lon, offset)

    forecast = route_forecast(route, ctx, etas[0].date())
    cloud = np.array(
        [
            _point_cloud(forecast, route.points[i].cum_dist_m, etas[i])
            for i in range(len(route.points))
        ],
        dtype=object,
    )

    lit = is_sunlit(horizons.horizons, position.azimuth, position.elevation)
    direct, diffuse = _irradiance(position, sky, horizons.svf, cloud, lit)

    result = ScorerResult(name=name)
    _measure_segments(result, segments, etas, direct, diffuse, lit, horizons, forecast)
    _summarise(result, route, segments, direct, diffuse, lit, horizons, offset, guessed)
    _flag_against_preference(result, segments, ctx, lit, forecast, etas)

    for entry in horizons.coverage.entries():
        result.coverage.append(entry)
    result.coverage.append(
        CoverageEntry(
            source="pvlib",
            kind="solar_geometry",
            checked=True,
            reason=(
                f"UTC offset derived from longitude ({offset:+.0f} h); pass --utc-offset "
                "to state it, or shade is placed up to an hour out"
                if guessed
                else f"UTC offset {offset:+.0f} h as given"
            ),
            confidence=0.7 if guessed else 1.0,
        )
    )
    if not forecast.answered:
        result.coverage.append(
            CoverageEntry(
                source="forecast",
                kind="cloud_cover",
                checked=False,
                reason="no forecast: irradiance reported as clear-sky, an upper bound",
            )
        )
    return result


def _point_cloud(forecast: RouteForecast, cum_m: float, when: datetime) -> float | None:
    reading = forecast.at_distance(cum_m, when)
    return None if reading is None else reading.cloud_cover_pct


def _irradiance(
    position: SolarPosition,
    sky: ClearSky,
    svf: np.ndarray,
    cloud: np.ndarray,
    lit: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Direct and diffuse irradiance on a horizontal surface, W/m^2."""
    factors = np.array([_cloud_factors(c) for c in cloud], dtype=float)
    beam_factor, global_factor = factors[:, 0], factors[:, 1]

    sin_elev = np.clip(np.sin(position.elevation), 0.0, 1.0)
    dni = np.asarray(sky.dni, dtype=float)
    ghi = np.asarray(sky.ghi, dtype=float)

    direct_open = dni * sin_elev * beam_factor
    # What is left of the global once the beam is accounted for is the sky's own light,
    # and that is the part the sky view factor scales.
    diffuse_open = np.maximum(ghi * global_factor - direct_open, 0.0)

    return direct_open * lit.astype(float), diffuse_open * svf


def _measure_segments(
    result: ScorerResult,
    segments: list[Segment],
    etas: list[datetime],
    direct: np.ndarray,
    diffuse: np.ndarray,
    lit: np.ndarray,
    horizons: CorridorHorizons,
    forecast: RouteForecast,
) -> None:
    for segment in segments:
        lo, hi = segment.start_idx, segment.end_idx
        span = slice(lo, hi + 1)
        seconds = max((etas[hi] - etas[lo]).total_seconds(), 0.0)

        mean_direct = float(np.mean(direct[span]))
        mean_diffuse = float(np.mean(diffuse[span]))
        exposed = float(np.mean(lit[span].astype(float)))
        support = float(np.mean(horizons.support[span]))

        result.measurements.append(
            SegmentMeasurement(
                segment_id=segment.id,
                values={
                    "shaded_fraction": 1.0 - exposed,
                    "direct_w_m2": mean_direct,
                    "diffuse_w_m2": mean_diffuse,
                    "energy_j_m2": (mean_direct + mean_diffuse) * seconds,
                    "sky_view_factor": float(np.mean(horizons.svf[span])),
                },
                confidence=_confidence(horizons, support, forecast),
            )
        )


def _confidence(horizons: CorridorHorizons, support: float, forecast: RouteForecast) -> float:
    """How much of this measurement rests on data that was actually there.

    Three independent degradations multiply: an incomplete surface model, a tile whose
    ground was partly nodata, and a missing forecast leaving irradiance at its clear-sky
    ceiling. Reporting the product rather than the worst keeps a doubly-degraded segment
    from looking as good as a singly-degraded one.
    """
    value = float(horizons.coverage.confidence) * max(support, 0.0)
    if not forecast.answered:
        value *= NO_FORECAST_CONFIDENCE
    return min(max(value, 0.0), 1.0)


def _summarise(
    result: ScorerResult,
    route: Route,
    segments: list[Segment],
    direct: np.ndarray,
    diffuse: np.ndarray,
    lit: np.ndarray,
    horizons: CorridorHorizons,
    offset: float,
    guessed: bool,
) -> None:
    total = direct + diffuse
    result.measurements.append(
        SegmentMeasurement(
            segment_id=ROUTE_SUMMARY_ID,
            values={
                "shaded_fraction": float(1.0 - np.mean(lit.astype(float))),
                "mean_sky_view_factor": float(np.mean(horizons.svf)),
                "peak_irradiance_w_m2": float(np.max(total)) if total.size else 0.0,
                "surface": horizons.coverage.describe(),
                "utc_offset_hours": offset,
                "utc_offset_guessed": guessed,
                "raycast_rung": horizons.settings.rung,
            },
            confidence=float(horizons.coverage.confidence),
        )
    )


def _flag_against_preference(
    result: ScorerResult,
    segments: list[Segment],
    ctx: ScorerContext,
    lit: np.ndarray,
    forecast: RouteForecast,
    etas: list[datetime],
) -> None:
    """Flag only what the user has said they care about (scope 6.3).

    `sun` is `{cool, hot, pivot_c}`: a weight applied when the temperature at the ETA is
    below or above the pivot. Both default to zero, so the shipped behaviour is to report
    shade and cost nothing — which is why this is the last thing the scorer does and the
    first thing it skips.
    """
    if not ctx.profile.scores_sun:
        return

    preference = ctx.profile.sun.value
    for segment in segments:
        lo, hi = segment.start_idx, segment.end_idx
        exposed = float(np.mean(lit[lo : hi + 1].astype(float)))
        centre = segment.cum_start_m + segment.length_m / 2.0
        reading = forecast.at_distance(centre, etas[lo])
        temp = None if reading is None else reading.temp_c
        if temp is None:
            continue

        weight = preference.hot if temp > preference.pivot_c else preference.cool
        if weight == 0.0:
            continue

        # A negative weight seeks shade, so exposure is the offence; a positive weight
        # seeks sun, and shade is.
        offence = exposed if weight < 0 else 1.0 - exposed
        severity = min(abs(weight) * offence, FULL_EXPOSURE)
        if severity <= 0.0:
            continue

        result.flags.append(
            Flag(
                scorer=name,
                segment_id=segment.id,
                kind=FlagKind.SOFT,
                tier=Tier.COMFORT,
                severity=severity,
                reason_code="exposed_against_preference"
                if weight < 0
                else "shaded_against_preference",
                detail=(
                    f"{exposed:.0%} sunlit at {temp:.0f} C, against a stated "
                    f"{'shade' if weight < 0 else 'sun'} preference"
                ),
            )
        )


score = sun_exposure

__all__ = ["CLOUD_GLOBAL_COEFFICIENT", "CLOUD_GLOBAL_EXPONENT", "name", "score", "sun_exposure"]
