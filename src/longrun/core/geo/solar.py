"""Where the sun is, and how bright it would be with no clouds (scope 7.4).

Pure geometry and a clear-sky model. Nothing here reads the network, nothing here has a
cassette, and every number is reproducible from a timestamp and a coordinate — which is
why it lives in `core.geo` beside the ray-cast rather than inside a scorer. `sun_exposure`
and `lighting` both need it.

**The timezone problem M2 has to solve.** A plan carries a naive local start time
(`FrozenClock` takes one, `request.yaml` pins one), and solar position depends on the
actual UTC instant. An hour of error is fifteen degrees of azimuth, which moves a shadow
across the street — so this cannot be waved through.

There is no timezone database in this project's dependencies, so the offset is either
**stated** by the caller or **derived from longitude**, and the difference is reported
rather than hidden. Longitude gives mean solar time, which is right to within the
difference between a zone's meridian and the actual place, plus an hour wherever summer
time is in force — so the derived value is a fallback that says so, not a silent default.
Scope 3.6 applied to the clock: an assumed offset is a known unknown, not a fact.
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

    A fallback, never a fact. It is right to within a zone's meridian offset and wrong by
    a further hour wherever summer time applies — which for a US route is most of the
    running season. Callers report having used it.
    """
    return round(lon / DEGREES_PER_HOUR)


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
    "utc_offset_from_longitude",
]
