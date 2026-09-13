"""Where the sun is, and how bright it would be with no clouds (scope 7.4).

Pure geometry and a clear-sky model. Nothing here reads the network, nothing here has a
cassette, and every number is reproducible from a timestamp and a coordinate — which is
why it lives in `core.geo` beside the ray-cast rather than inside a scorer. `sun_exposure`
and `lighting` both need it.

**The timezone problem M2 has to solve.** A plan carries a naive local start time
(`FrozenClock` takes one, `request.yaml` pins one), and solar position depends on the
actual UTC instant. An hour of error is fifteen degrees of azimuth, which moves a shadow
across the street — so this cannot be waved through.

The offset is **stated** by the caller, **looked up** from the coordinate through a
timezone database, or — only if both fail — **derived from longitude**, and which of the
three happened is reported rather than hidden (ADR 0008). Scope 3.6 applied to the clock.

The longitude fallback is kept and is a poor answer: it gives mean solar time, so it is
wrong by an hour wherever summer time is in force, which for a US route is most of the
running season. San Francisco in September is -7 and longitude says -8; Boston is -4 and
longitude says -5. It survives only because a lookup can fail on a coordinate no zone
polygon covers, and a wrong hour reported as a guess still beats a crash.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    from numpy.typing import NDArray

#: Degrees of longitude per hour of solar time.
DEGREES_PER_HOUR = 15.0

#: Sun below this is night for the purposes of scope 7.4's daylight status. Civil twilight
#: rather than geometric sunset: a runner still has usable light at -4 degrees, and calling
#: that darkness would flag half the segments on an evening run.
CIVIL_TWILIGHT_DEG = -6.0

#: Above this the sun is high enough that a horizon obstruction has to be close or tall to
#: matter. Used only to keep the shade summary honest about near-noon geometry.
HIGH_SUN_DEG = 60.0


@dataclass(frozen=True)
class SolarPosition:
    """Apparent position of the sun at a set of instants, in radians."""

    elevation: NDArray[np.float64]
    azimuth: NDArray[np.float64]

    @property
    def is_daylight(self) -> NDArray[np.bool_]:
        return self.elevation > math.radians(CIVIL_TWILIGHT_DEG)

    @property
    def above_horizon(self) -> NDArray[np.bool_]:
        return self.elevation > 0.0


@dataclass(frozen=True)
class ClearSky:
    """Clear-sky irradiance in W/m^2 — the ceiling that cloud cover scales down."""

    ghi: NDArray[np.float64]
    dni: NDArray[np.float64]
    dhi: NDArray[np.float64]


def utc_offset_from_longitude(lon: float) -> float:
    """Mean-solar-time offset for a longitude, in hours.

    The last resort. Right to within a zone's meridian offset and wrong by a further hour
    wherever summer time applies. Callers report having used it.
    """
    return round(lon / DEGREES_PER_HOUR)


def utc_offset_for(
    lat: float, lon: float, when: datetime, stated: float | None = None
) -> tuple[float, str]:
    """`(hours from UTC, how it was arrived at)` — stated, looked up, or guessed.

    The second element is the point. A shade figure computed from a guessed offset can be
    an hour out, which is more than fifteen degrees of solar azimuth, and a plan has to be
    able to say which of the three it used.

    The lookup resolves the zone from the coordinate and then asks that zone what its
    offset was **on the plan's own date**, so summer time is handled rather than averaged
    over. Ambiguous instants inside a DST transition resolve to the standard-time reading,
    which is `zoneinfo`'s default and is off by an hour for at most one hour a year.
    """
    if stated is not None:
        return float(stated), "stated"
    try:
        from zoneinfo import ZoneInfo

        import tzfpy

        zone = tzfpy.get_tz(lon, lat)
        if zone:
            offset = when.replace(tzinfo=ZoneInfo(zone)).utcoffset()
            if offset is not None:
                return offset.total_seconds() / 3600.0, f"zone {zone}"
    except Exception:  # pragma: no cover - no zone covers this coordinate, or no data
        pass
    return utc_offset_from_longitude(lon), "derived from longitude"


def _index(times: Sequence[datetime], utc_offset_hours: float) -> object:
    """Naive local times to a UTC pandas index, which is what pvlib wants."""
    import pandas as pd

    shifted = [t - timedelta(hours=utc_offset_hours) if t.tzinfo is None else t for t in times]
    return pd.DatetimeIndex(shifted, tz="UTC")


def solar_positions(
    times: Sequence[datetime], lat: float, lon: float, utc_offset_hours: float
) -> SolarPosition:
    """Apparent elevation and azimuth, in radians, north-clockwise.

    Radians and north-clockwise because that is what `raycast.is_sunlit` compares against;
    converting once here beats converting at every comparison, and pvlib's azimuth is
    already measured clockwise from north.
    """
    import pvlib

    if not times:
        empty = np.zeros(0, dtype=np.float64)
        return SolarPosition(elevation=empty, azimuth=empty)

    frame = pvlib.solarposition.get_solarposition(_index(times, utc_offset_hours), lat, lon)
    return SolarPosition(
        elevation=np.radians(frame["apparent_elevation"].to_numpy(dtype=float)),
        azimuth=np.radians(frame["azimuth"].to_numpy(dtype=float)),
    )


def clear_sky(
    times: Sequence[datetime],
    lat: float,
    lon: float,
    utc_offset_hours: float,
    altitude_m: float = 0.0,
) -> ClearSky:
    """Ineichen-Perez clear-sky irradiance, as scope 7.4 names.

    The ceiling, not the answer: `sun_exposure` scales this by cloud cover from the
    forecast, and zeroes the direct component wherever the ray-cast says the skyline is in
    the way. Diffuse survives shade — a shaded runner is still under the sky — which is the
    whole reason the sky view factor is measured separately.
    """
    import pvlib

    if not times:
        empty = np.zeros(0, dtype=np.float64)
        return ClearSky(ghi=empty, dni=empty, dhi=empty)

    location = pvlib.location.Location(lat, lon, tz="UTC", altitude=max(0.0, altitude_m))
    frame = location.get_clearsky(_index(times, utc_offset_hours), model="ineichen")
    return ClearSky(
        ghi=frame["ghi"].to_numpy(dtype=float),
        dni=frame["dni"].to_numpy(dtype=float),
        dhi=frame["dhi"].to_numpy(dtype=float),
    )


__all__ = [
    "CIVIL_TWILIGHT_DEG",
    "DEGREES_PER_HOUR",
    "HIGH_SUN_DEG",
    "ClearSky",
    "SolarPosition",
    "clear_sky",
    "solar_positions",
    "utc_offset_for",
    "utc_offset_from_longitude",
]
