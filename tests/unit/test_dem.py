"""Elevation tests (scope 7.1, 7.9, 9).

Synthetic terrain with hand-computed answers: a constant ramp, a symmetric hill, and a
deliberate single-sample artifact of the kind scope 7.9 check 4 exists to catch.
"""

from __future__ import annotations

import pytest

from longrun.core.geo.dem import (
    DEFAULT_GAIN_THRESHOLD_M,
    ElevationProfile,
    elevation_profile,
    grades,
    strip_spikes,
)
from longrun.core.models.geometry import Route, RoutePoint


def _route(n: int, spacing_m: float = 100.0) -> Route:
    return Route(
        id="r",
        points=[
            RoutePoint(lat=37.77 + i * 0.0009, lon=-122.4, cum_dist_m=i * spacing_m)
            for i in range(n)
        ],
    )


def _ramp(n: int, rise_per_point_m: float) -> list[float | None]:
    return [i * rise_per_point_m for i in range(n)]


# --- grades -----------------------------------------------------------------


def test_constant_ramp_has_constant_grade() -> None:
    """5 m of rise per 100 m of travel is 5%, everywhere along the ramp."""
    route = _route(21)
    result = grades(route, _ramp(21, 5.0), smooth_m=200.0)
    interior = [g for g in result[2:-2] if g is not None]
    assert interior
    assert all(g == pytest.approx(5.0) for g in interior)


def test_descent_is_negative() -> None:
    route = _route(21)
    result = grades(route, _ramp(21, -4.0), smooth_m=200.0)
    assert result[10] == pytest.approx(-4.0)


def test_flat_terrain_is_zero_grade() -> None:
    route = _route(11)
    assert all(g == pytest.approx(0.0) for g in grades(route, [100.0] * 11) if g is not None)


def test_grade_is_none_where_elevation_is_missing() -> None:
    """A DEM hole is unknown grade, not zero grade (scope 12)."""
    route = _route(5)
    result = grades(route, [None] * 5, smooth_m=100.0)
    assert result == [None] * 5


def test_grades_reject_mismatched_elevation_count() -> None:
    with pytest.raises(ValueError, match="for 5 route points"):
        grades(_route(5), [1.0, 2.0])


def test_smoothing_window_is_measured_in_distance_not_points() -> None:
    """A dense track and a sparse one must report comparable grades."""
    sparse = grades(_route(11, spacing_m=100.0), _ramp(11, 5.0), smooth_m=200.0)
    dense = grades(_route(101, spacing_m=10.0), _ramp(101, 0.5), smooth_m=200.0)
    assert sparse[5] == pytest.approx(5.0)
    assert dense[50] == pytest.approx(5.0)


# --- spike removal ----------------------------------------------------------


def test_single_sample_artifact_is_removed() -> None:
    """Scope 7.9 check 4: a spike that goes up and straight back is not terrain."""
    elevations: list[float | None] = [100.0, 100.0, 900.0, 100.0, 100.0]
    assert strip_spikes(elevations) == [100.0, 100.0, 100.0, 100.0, 100.0]


def test_a_genuine_step_is_preserved() -> None:
    """A cliff that does not come back is real, however large."""
    elevations: list[float | None] = [100.0, 100.0, 900.0, 900.0, 900.0]
    assert strip_spikes(elevations) == elevations


def test_spike_removal_tolerates_gaps_and_short_inputs() -> None:
    assert strip_spikes([100.0, None, 100.0]) == [100.0, None, 100.0]
    assert strip_spikes([100.0]) == [100.0]
    assert strip_spikes([]) == []


def test_artifact_does_not_inflate_gain() -> None:
    """The point of stripping spikes: 800 m of phantom climb on a flat route."""
    route = _route(5)
    spiked = elevation_profile(route, [100.0, 100.0, 900.0, 100.0, 100.0])
    assert spiked.gain_m == pytest.approx(0.0)


# --- profile ----------------------------------------------------------------


def test_ramp_gain_matches_the_total_rise() -> None:
    route = _route(21)
    profile = elevation_profile(route, _ramp(21, 5.0), smooth_m=200.0)
    assert profile.gain_m == pytest.approx(100.0, abs=DEFAULT_GAIN_THRESHOLD_M)
    assert profile.loss_m == pytest.approx(0.0)
    assert profile.min_ele_m == 0.0
    assert profile.max_ele_m == 100.0


def test_symmetric_hill_gains_and_loses_equally() -> None:
    route = _route(21)
    up = _ramp(11, 10.0)
    down = [100.0 - (i + 1) * 10.0 for i in range(10)]
    profile = elevation_profile(route, [*up, *down], smooth_m=200.0)
    assert profile.gain_m == pytest.approx(profile.loss_m, abs=1.0)


def test_noise_below_the_threshold_is_not_counted_as_climbing() -> None:
    """Accumulating every sample turns DEM noise into phantom gain over 50 km."""
    route = _route(41)
    jitter: list[float | None] = [100.0 + (1.0 if i % 2 else -1.0) for i in range(41)]
    assert elevation_profile(route, jitter).gain_m == pytest.approx(0.0)


def test_profile_with_no_elevation_at_all_reports_it() -> None:
    profile = elevation_profile(_route(5), [None] * 5)
    assert profile.has_elevation is False
    assert profile.samples_missing == 5
    assert profile.gain_m == 0.0


def test_missing_samples_are_counted_not_zero_filled() -> None:
    profile = elevation_profile(_route(5), [100.0, None, 100.0, None, 100.0])
    assert profile.samples_missing == 2
    assert profile.min_ele_m == 100.0


def test_longest_climb_is_measured_from_dem_not_gpx_elevation() -> None:
    """The GPX carries drifting barometric values; the profile must ignore them."""
    route = Route(
        id="r",
        points=[
            RoutePoint(lat=37.77 + i * 0.0009, lon=-122.4, ele_m=9999.0, cum_dist_m=i * 100.0)
            for i in range(11)
        ],
    )
    profile = elevation_profile(route, _ramp(11, 5.0), smooth_m=200.0)
    assert profile.longest_climb is not None
    assert profile.longest_climb.gain_m == pytest.approx(50.0, abs=10.0)
    assert profile.longest_climb.mean_grade_pct == pytest.approx(5.0, abs=1.0)


def test_longest_descent_found_on_a_falling_route() -> None:
    route = _route(21)
    profile = elevation_profile(route, _ramp(21, -5.0), smooth_m=200.0)
    assert profile.longest_descent is not None
    assert profile.longest_descent.length_m > 1000.0
    assert profile.longest_descent.mean_grade_pct < 0


def test_grade_histogram_sums_to_route_length() -> None:
    """Scope 9 reports a grade summary; every metre must land in exactly one bin."""
    route = _route(21)
    profile = elevation_profile(route, _ramp(21, 5.0), smooth_m=200.0)
    assert sum(profile.grade_histogram.values()) == pytest.approx(route.length_m, rel=0.01)


def test_histogram_bins_are_sorted_numerically() -> None:
    route = _route(21)
    up = _ramp(11, 10.0)
    down = [100.0 - (i + 1) * 10.0 for i in range(10)]
    profile = elevation_profile(route, [*up, *down], smooth_m=200.0)
    keys = [float(k) for k in profile.grade_histogram]
    assert keys == sorted(keys)


def test_profile_serializes() -> None:
    profile = elevation_profile(_route(11), _ramp(11, 5.0), smooth_m=200.0)
    assert ElevationProfile.model_validate_json(profile.model_dump_json()).gain_m == pytest.approx(
        profile.gain_m
    )


def test_a_point_outside_the_raster_is_unknown_and_not_a_nodata_reading() -> None:
    """The bug the M5.5 loop found by routing off the edge of a fixture DEM.

    `read_window` reads `boundless=True, fill_value=src.nodata`, so a point outside
    coverage comes back as the fill value rather than as nothing. With a nodata of
    -999999 that is not a small error: the observed sheet reported **1,000,012 m of
    descent** over 8 km, and `sample_elevation`'s own docstring already forbade it -
    "a hole in the DEM is unknown elevation, and zero would read as sea level and invent
    thousands of metres of gain".

    Every golden route sits inside the DEM frozen for it, which is why four milestones of
    green goldens never touched this. The first route not cut to fit its raster found it
    immediately.
    """
    import numpy as np
    from affine import Affine

    from longrun.core.geo.dem import _sample_window

    nodata = -999999.0
    band = np.array([[10.0, nodata], [nodata, 12.0]], dtype="float32")
    transform = Affine.translation(-122.0, 37.5) * Affine.scale(0.01, -0.01)
    route = Route(
        id="edge",
        points=[
            RoutePoint(lat=37.495, lon=-121.995, cum_dist_m=0.0),
            RoutePoint(lat=37.495, lon=-121.985, cum_dist_m=100.0),
            RoutePoint(lat=37.485, lon=-121.985, cum_dist_m=200.0),
        ],
    )

    assert _sample_window(route, band, transform, nodata) == [10.0, None, 12.0]
    # And without being told the nodata value, the fill reads as a measurement - which is
    # exactly what `read_window` did by discarding it.
    assert _sample_window(route, band, transform, None) == [10.0, nodata, 12.0]
