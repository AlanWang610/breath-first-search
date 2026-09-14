"""The ray-cast against geometry whose answer is known in closed form.

A horizon algorithm is easy to write so that it produces plausible numbers and wrong ones:
an off-by-one in the azimuth convention puts every shadow on the wrong side of the street,
and nothing about the output looks odd. So every case here has a hand-computable answer —
a wall of height H at distance D subtends `arctan(H/D)`, and that is what is asserted.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from longrun.core.geo.raycast import (
    LADDER,
    RaycastSettings,
    azimuth_angles,
    horizon_at,
    horizon_is_truncated,
    horizon_profile,
    is_sunlit,
    sky_view_factor,
)

CELL_M = 2.0
SIZE = 401
CENTRE = 200

#: Azimuth indices in a 72-sample profile.
NORTH, EAST, SOUTH, WEST = 0, 18, 36, 54


def _flat() -> np.ndarray:
    return np.zeros((SIZE, SIZE), dtype=np.float32)


def _with_wall(distance_m: float, height_m: float, thickness_cells: int = 6) -> np.ndarray:
    """A north-south wall `distance_m` due east of the centre."""
    surface = _flat()
    near = CENTRE + int(distance_m / CELL_M)
    surface[:, near : near + thickness_cells] = height_m
    return surface


def _at_centre() -> tuple[np.ndarray, np.ndarray]:
    return np.array([float(CENTRE)]), np.array([float(CENTRE)])


# --- the profile ------------------------------------------------------------


def test_flat_ground_has_no_horizon() -> None:
    rows, cols = _at_centre()
    horizon = horizon_profile(_flat(), CELL_M, rows, cols)
    assert horizon.shape == (1, 72)
    assert np.all(horizon == 0.0)


def test_a_wall_subtends_the_angle_it_should() -> None:
    """40 m tall, 200 m away: arctan(40/200) = 11.31 degrees, and not a degree more."""
    rows, cols = _at_centre()
    horizon = horizon_profile(_with_wall(200.0, 40.0), CELL_M, rows, cols)
    assert horizon[0, EAST] == pytest.approx(math.atan2(40.0, 200.0), abs=1e-3)


def test_the_wall_is_on_the_side_it_was_built_on() -> None:
    """The azimuth convention, asserted rather than assumed.

    North-clockwise is what a solar azimuth is. Getting it backwards would shade the east
    side of every street at sunrise and look entirely reasonable in a plan sheet.
    """
    rows, cols = _at_centre()
    horizon = horizon_profile(_with_wall(200.0, 40.0), CELL_M, rows, cols)
    assert horizon[0, EAST] > 0.1
    for azimuth in (NORTH, SOUTH, WEST):
        assert horizon[0, azimuth] == 0.0


def test_a_closer_wall_subtends_more() -> None:
    rows, cols = _at_centre()
    near = horizon_profile(_with_wall(100.0, 40.0), CELL_M, rows, cols)
    far = horizon_profile(_with_wall(300.0, 40.0), CELL_M, rows, cols)
    assert near[0, EAST] > far[0, EAST] > 0.0


def test_an_obstruction_beyond_the_search_radius_is_not_seen() -> None:
    """The radius is a real cut-off, which is what makes it a budget knob."""
    rows, cols = _at_centre()
    settings = RaycastSettings(rung="full", azimuths=72, max_radius_m=150.0, step_m=2.0)
    horizon = horizon_profile(_with_wall(200.0, 40.0), CELL_M, rows, cols, settings)
    assert horizon[0, EAST] == 0.0


def test_kerb_height_noise_is_not_an_obstruction() -> None:
    """A 0.2 m step is DSM noise, not a skyline (`MIN_OBSTRUCTION_M`)."""
    rows, cols = _at_centre()
    horizon = horizon_profile(_with_wall(50.0, 0.2), CELL_M, rows, cols)
    assert np.all(horizon == 0.0)


def test_many_points_are_profiled_independently() -> None:
    surface = _with_wall(200.0, 40.0)
    rows = np.full(4, float(CENTRE))
    cols = np.array([float(CENTRE), CENTRE + 40.0, CENTRE + 80.0, 10.0])
    horizon = horizon_profile(surface, CELL_M, rows, cols)

    assert horizon.shape == (4, 72)
    # Walking east towards the wall raises it; the far-west point sees it furthest away.
    assert horizon[0, EAST] < horizon[1, EAST] < horizon[2, EAST]
    assert horizon[3, EAST] < horizon[0, EAST]


# --- sky view factor --------------------------------------------------------


def test_open_sky_is_one() -> None:
    assert sky_view_factor(np.zeros((1, 72))) == pytest.approx(1.0)


def test_a_uniform_forty_five_degree_skyline_is_a_half() -> None:
    """SVF averages cos^2 of the horizon angle, so 45 degrees all round is exactly 0.5."""
    horizon = np.full((1, 72), math.pi / 4)
    assert sky_view_factor(horizon) == pytest.approx(0.5)


def test_a_closed_sky_is_zero() -> None:
    assert sky_view_factor(np.full((1, 72), math.pi / 2)) == pytest.approx(0.0)


def test_the_wall_reduces_the_sky_view_by_its_own_sector_only() -> None:
    """One blocked azimuth out of 72 removes one seventy-second of the weighted sky."""
    horizon = np.zeros((1, 72))
    horizon[0, EAST] = math.pi / 2
    assert sky_view_factor(horizon) == pytest.approx(1.0 - 1.0 / 72.0)


# --- sun position against the profile ---------------------------------------


def test_the_sun_clears_a_low_wall_and_not_a_high_one() -> None:
    horizon = np.zeros((1, 72))
    horizon[0, EAST] = math.radians(20.0)
    east = math.pi / 2

    assert is_sunlit(horizon, east, math.radians(30.0))[0]
    assert not is_sunlit(horizon, east, math.radians(10.0))[0]


def test_night_is_not_sunlit() -> None:
    """Below the horizontal is night, not shade; a sheet must not call 2 a.m. shaded."""
    assert not is_sunlit(np.zeros((1, 72)), 0.0, math.radians(-5.0))[0]


def test_the_horizon_is_interpolated_between_azimuths() -> None:
    """The sun is never exactly on a sample azimuth.

    Snapping would make the sunlit boundary jump by up to half a sector as the sun moves,
    which in a 5-degree profile is twenty minutes of spurious shade.
    """
    horizon = np.zeros((1, 72))
    horizon[0, EAST] = math.radians(20.0)
    horizon[0, EAST + 1] = math.radians(40.0)

    step = 2.0 * math.pi / 72.0
    midpoint = azimuth_angles(72)[EAST] + step / 2.0
    assert horizon_at(horizon, midpoint)[0] == pytest.approx(math.radians(30.0))


def test_interpolation_wraps_around_north() -> None:
    horizon = np.zeros((1, 72))
    horizon[0, 71] = math.radians(10.0)
    horizon[0, 0] = math.radians(30.0)

    step = 2.0 * math.pi / 72.0
    assert horizon_at(horizon, 2.0 * math.pi - step / 2.0)[0] == pytest.approx(math.radians(20.0))


# --- the degradation ladder -------------------------------------------------


def test_the_ladder_is_ordered_coarsest_last() -> None:
    """Scope 6.4 descends it under time pressure, so the order has to mean something."""
    work = [rung.azimuths * rung.steps for rung in LADDER]
    assert work == sorted(work, reverse=True)
    assert [rung.rung for rung in LADDER] == ["full", "reduced", "coarse", "minimal"]


def test_a_coarser_rung_still_sees_a_large_obstruction() -> None:
    """Degrading may cost precision. It must not turn a building into open sky."""
    rows, cols = _at_centre()
    surface = _with_wall(150.0, 40.0, thickness_cells=12)
    for settings in LADDER:
        horizon = horizon_profile(surface, CELL_M, rows, cols, settings)
        east = int(round(settings.azimuths / 4))
        assert horizon[0, east] > math.radians(10.0), f"{settings.rung} lost the wall"


def test_a_corridor_narrower_than_the_search_radius_is_reported_as_truncated() -> None:
    """Rays running off the raster report open sky, which is a different claim (scope 3.6)."""
    assert horizon_is_truncated((200, 200), CELL_M)
    assert not horizon_is_truncated((1000, 1000), CELL_M)
