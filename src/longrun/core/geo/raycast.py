"""Direct-sun ray-casting over a surface model (scope 5, 6.4, 7.4).

The measurement scope 7.4 asks for has two halves — direct irradiance, which needs to know
whether the sun reaches a point, and diffuse, which needs to know how much sky the point
can see. Both fall out of one quantity: the **horizon profile**, the elevation angle of the
skyline in each of N azimuths around a point.

That is why this computes horizons rather than casting one ray per (point, time), which is
the cheaper thing to do if direct sun is all you want — measured at 33x cheaper, in fact,
and risk R3 expected the opposite. See ADR 0003. The profile still wins, for two reasons
that survive the measurement:

* **Diffuse needs it anyway.** The sky view factor is an integral over the horizon profile
  (`sky_view_factor` below), and scope 7.4 asks for direct *and* diffuse. Casting a single
  ray per point answers half the measurement and leaves the other half needing exactly this
  computation. This is the reason; everything else is a bonus.
* **It is the cacheable, time-independent unit.** Scope 5 caches SVF as COG tiles keyed by
  tile id, and scope 7.7 sweeps start times over one geometry. A profile is reusable across
  both; a ray answers one point at one moment.

The plan's stated rationale — that `start_time_optimizer` makes the profile "dramatically
better" — is not what the numbers say: break-even is at 33 start times and a sweep is
nearer 8. It is the right call for the wrong reason, which is worth writing down.

**The degradation ladder is parameters, not branches.** Scope 6.4 lets resolution fall
before the time budget is exceeded, so every knob that trades accuracy for time is a field
on `RaycastSettings` with a name that goes into the manifest. A plan that degraded must be
able to say so; a plan that says nothing must not have degraded. In practice it should
never be needed: the finest configuration scope 7.4 asks for does a 100 km route in under
half a second, so the ladder is a capability held in reserve rather than a normal path.

Grid coordinates, not geographic. The caller resamples the corridor into a metric grid
(`core.geo.dsm`) before calling in, because a ray-cast in degrees is wrong by the cosine of
the latitude and wrong differently along each axis.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import numpy as np
from pydantic import BaseModel, Field

if TYPE_CHECKING:  # pragma: no cover
    from numpy.typing import NDArray

#: Rungs of the ladder, coarsest last. Named so the manifest can record which one ran
#: rather than a bag of numbers a reader has to interpret.
Rung = Literal["full", "reduced", "coarse", "minimal"]

#: Height difference below which a neighbour is not an obstruction. Guards against a DSM
#: whose canopy layer is noisy at the metre scale reading as a wall.
MIN_OBSTRUCTION_M = 0.5


class RaycastSettings(BaseModel):
    """One rung of the scope 6.4 degradation ladder.

    Everything here trades accuracy for time. `rung` names the combination so the plan
    manifest can report it in one word; the fields are what actually took effect.
    """

    model_config = {"frozen": True}

    rung: Rung = "full"
    #: Azimuths in the profile. 72 is one sample per 5 degrees — finer than the sun moves
    #: in 20 minutes, and finer than a 2 m DSM resolves a building edge at 200 m.
    azimuths: int = Field(default=72, ge=8, le=720)
    #: How far to look for an obstruction. Beyond a few hundred metres a building has to be
    #: implausibly tall to shade you, and terrain that far away is the DEM's business.
    max_radius_m: float = Field(default=500.0, gt=0)
    #: Distance between samples along a ray. Below the cell size it only re-reads cells.
    step_m: float = Field(default=2.0, gt=0)

    @property
    def steps(self) -> int:
        return max(1, int(self.max_radius_m / self.step_m))


#: The ladder, in the order it is descended. Each rung is roughly half the work of the one
#: before it, so the choice is coarse on purpose: fine-grained tuning would imply a
#: precision the budget accounting does not have.
LADDER: tuple[RaycastSettings, ...] = (
    RaycastSettings(rung="full", azimuths=72, max_radius_m=500.0, step_m=2.0),
    RaycastSettings(rung="reduced", azimuths=48, max_radius_m=400.0, step_m=3.0),
    RaycastSettings(rung="coarse", azimuths=32, max_radius_m=300.0, step_m=5.0),
    RaycastSettings(rung="minimal", azimuths=16, max_radius_m=200.0, step_m=10.0),
)


def azimuth_angles(count: int) -> NDArray[np.float64]:
    """Azimuths of the profile, clockwise from north, in radians.

    North-clockwise rather than mathematical convention because that is what a solar
    azimuth is, and converting in one place beats converting at every comparison.
    """
    return np.linspace(0.0, 2.0 * np.pi, count, endpoint=False)


def horizon_profile(
    surface: NDArray[np.floating],
    cell_m: float,
    rows: NDArray[np.floating],
    cols: NDArray[np.floating],
    settings: RaycastSettings | None = None,
) -> NDArray[np.float64]:
    """Skyline elevation angle in each azimuth, for each sample point.

    `surface` is a metric grid of heights, north-up, with square `cell_m` cells. `rows` and
    `cols` are fractional grid coordinates of the sample points. Returns radians above the
    horizontal, shape `(len(rows), settings.azimuths)`, zero where nothing rises above the
    point.

    Vectorised over points and azimuths, looping over range steps: the loop is a few
    hundred iterations over an array of a few tens of thousands, which is the shape numpy
    is good at. A per-ray Python loop would be the same arithmetic four orders of magnitude
    slower, and is what makes this look like it needs a JIT.
    """
    config = settings or LADDER[0]
    azimuths = azimuth_angles(config.azimuths)
    height, width = surface.shape

    origin_r = np.asarray(rows, dtype=np.float64)[:, None]
    origin_c = np.asarray(cols, dtype=np.float64)[:, None]
    base = _sample(surface, origin_r, origin_c, height, width)

    # Grid steps per metre travelled, one per azimuth. North is -row, east is +col.
    d_row = -np.cos(azimuths)[None, :] / cell_m
    d_col = np.sin(azimuths)[None, :] / cell_m

    tallest = np.zeros((origin_r.shape[0], config.azimuths), dtype=np.float64)
    for step in range(1, config.steps + 1):
        distance = step * config.step_m
        sample_r = origin_r + distance * d_row
        sample_c = origin_c + distance * d_col

        inside = (sample_r >= 0) & (sample_r < height) & (sample_c >= 0) & (sample_c < width)
        if not inside.any():
            # Every ray has left the raster; nothing further out can be sampled. This is a
            # real limit, not an absence of obstructions - `horizon_is_truncated` reports it.
            break

        heights = _sample(surface, sample_r, sample_c, height, width)
        rise = heights - base
        angle = np.where(
            inside & (rise > MIN_OBSTRUCTION_M) & np.isfinite(rise),
            np.arctan2(rise, distance),
            0.0,
        )
        np.maximum(tallest, angle, out=tallest)

    return tallest


def _sample(
    surface: NDArray[np.floating],
    rows: NDArray[np.float64],
    cols: NDArray[np.float64],
    height: int,
    width: int,
) -> NDArray[np.float64]:
    """Nearest-neighbour height lookup, clamped to the grid.

    Nearest rather than bilinear on purpose: a horizon is defined by the tallest thing on
    the ray, and interpolating heights across a building edge invents a ramp where there is
    a wall. Out-of-bounds reads are clamped and then discarded by the caller's `inside`
    mask, which is cheaper than fancy-indexing a ragged selection.
    """
    r = np.clip(rows.astype(np.int64), 0, height - 1)
    c = np.clip(cols.astype(np.int64), 0, width - 1)
    return surface[r, c].astype(np.float64)


def sky_view_factor(horizon: NDArray[np.float64]) -> NDArray[np.float64]:
    """Fraction of the sky hemisphere visible, weighted for a horizontal surface.

    For a horizon at elevation angle θ in one azimuth, the projected solid angle of visible
    sky in that sector is `cos²θ / 2`, so averaging `cos²θ` over equally spaced azimuths
    gives the factor directly. Open sky is 1.0; a uniform 45° skyline is 0.5.

    This is the diffuse half of scope 7.4, and it is why the profile is computed rather
    than a single ray per point.
    """
    return np.mean(np.cos(horizon) ** 2, axis=-1)


def horizon_at(
    horizon: NDArray[np.float64], azimuth_rad: float | NDArray[np.float64]
) -> NDArray[np.float64]:
    """The profile's horizon angle at an arbitrary azimuth, linearly interpolated.

    The sun is not on a profile azimuth. Interpolating between the two neighbouring samples
    is closer than snapping, and snapping would make the sunlit/shaded boundary jump by up
    to half a sector as the sun moves.
    """
    count = horizon.shape[-1]
    position = np.asarray(azimuth_rad, dtype=np.float64) / (2.0 * np.pi) * count
    low = np.floor(position).astype(np.int64) % count
    high = (low + 1) % count
    weight = position - np.floor(position)
    rows = np.arange(horizon.shape[0])
    return horizon[rows, low] * (1.0 - weight) + horizon[rows, high] * weight


def is_sunlit(
    horizon: NDArray[np.float64],
    solar_azimuth_rad: float | NDArray[np.float64],
    solar_elevation_rad: float | NDArray[np.float64],
) -> NDArray[np.bool_]:
    """Whether the sun clears the skyline at each point.

    A sun below the horizontal is night, not shade, and the caller is expected to treat the
    two differently — `sun_exposure` reports zero direct irradiance either way, but a plan
    that says "shaded" about 2 a.m. is wrong.
    """
    elevation = np.asarray(solar_elevation_rad, dtype=np.float64)
    return (elevation > 0.0) & (elevation > horizon_at(horizon, solar_azimuth_rad))


def horizon_is_truncated(
    surface_shape: tuple[int, int], cell_m: float, settings: RaycastSettings | None = None
) -> bool:
    """Whether the grid is too small for the configured search radius.

    A ray that runs off the raster reports no obstruction, which is indistinguishable from
    open sky. Scope 3.6 says those must not be confused, so a corridor extract narrower
    than the search radius is a coverage caveat and the caller has to say so.
    """
    config = settings or LADDER[0]
    return min(surface_shape) * cell_m < 2.0 * config.max_radius_m


__all__ = [
    "LADDER",
    "MIN_OBSTRUCTION_M",
    "RaycastSettings",
    "Rung",
    "azimuth_angles",
    "horizon_at",
    "horizon_is_truncated",
    "horizon_profile",
    "is_sunlit",
    "sky_view_factor",
]
