"""Pacing tests (scope 3.3, 6.2, 7.3, 12).

The caveat tests are as important as the arithmetic. Scope 12 requires an extrapolated
pace to be labelled a guess, and the labelling only survives if it is produced alongside
the ETAs rather than left to whoever renders them.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from longrun.core.geo.segments import segment_route
from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.pacing.curves import PacingCurves
from longrun.core.pacing.model import ETAVector, pacing_model
from longrun.core.pacing.population import (
    FLAT_COST,
    MAX_DOWNHILL_SPEED_FACTOR,
    MAX_GRADIENT,
    fatigue_factor,
    grade_factor,
    minetti_cost,
)

START = datetime(2026, 3, 15, 7, 30)


def _route(n: int = 101, spacing_m: float = 100.0) -> Route:
    return Route(
        id="r",
        points=[
            RoutePoint(lat=37.77 + i * 0.0009, lon=-122.4, cum_dist_m=i * spacing_m)
            for i in range(n)
        ],
    )


def _ramp(n: int, rise_per_point_m: float) -> list[float | None]:
    return [i * rise_per_point_m for i in range(n)]


# --- Minetti ----------------------------------------------------------------


def test_flat_cost_matches_the_published_constant() -> None:
    """Minetti et al. 2002: 3.6 J/(kg*m) on the level."""
    assert minetti_cost(0.0) == pytest.approx(FLAT_COST)
    assert minetti_cost(0.0) == pytest.approx(3.6)


def test_climbing_costs_more_than_the_flat() -> None:
    assert minetti_cost(0.10) > minetti_cost(0.0)
    assert minetti_cost(0.20) > minetti_cost(0.10)


def test_gentle_descent_costs_less_than_the_flat() -> None:
    assert minetti_cost(-0.10) < minetti_cost(0.0)


def test_steep_descent_costs_more_again() -> None:
    """The shape that makes the polynomial worth using: braking is work."""
    assert minetti_cost(-0.40) > minetti_cost(-0.20)


def test_gradient_is_clamped_rather_than_extrapolated() -> None:
    """Past Minetti's measured range the polynomial diverges; a 60% wall is not 8x a 45%."""
    assert minetti_cost(0.9) == pytest.approx(minetti_cost(MAX_GRADIENT))
    assert minetti_cost(-0.9) == pytest.approx(minetti_cost(-MAX_GRADIENT))


def test_grade_factor_is_one_on_the_flat() -> None:
    assert grade_factor(0.0) == pytest.approx(1.0)


def test_climbing_is_slower() -> None:
    assert grade_factor(0.10) < 1.0
    assert grade_factor(0.15) < grade_factor(0.10)


def test_downhill_bonus_is_capped() -> None:
    """Uncapped Minetti gives 2x flat speed at -20%: nobody runs 3:00/km downhill."""
    assert grade_factor(-0.10) == pytest.approx(MAX_DOWNHILL_SPEED_FACTOR)
    assert grade_factor(-0.20) == pytest.approx(MAX_DOWNHILL_SPEED_FACTOR)
    assert max(grade_factor(g / 100) for g in range(-45, 46)) <= MAX_DOWNHILL_SPEED_FACTOR


def test_very_steep_descent_is_slower_than_the_cap() -> None:
    assert grade_factor(-0.40) < MAX_DOWNHILL_SPEED_FACTOR


# --- fatigue ----------------------------------------------------------------


def test_no_fatigue_before_the_onset() -> None:
    assert fatigue_factor(5_000.0) == 1.0
    assert fatigue_factor(10_000.0) == 1.0


def test_fatigue_accumulates_with_distance() -> None:
    assert fatigue_factor(20_000.0) < 1.0
    assert fatigue_factor(50_000.0) < fatigue_factor(20_000.0)


def test_fatigue_never_stops_the_runner_entirely() -> None:
    """A 200 km input must not produce a non-positive speed multiplier."""
    assert fatigue_factor(200_000.0) > 0.0


# --- curves -----------------------------------------------------------------


def test_measured_curve_overrides_the_population_one() -> None:
    """A user's own grade-adjusted pace beats Minetti (scope 6.2)."""
    curves = PacingCurves(speed_by_grade_bin={"+4": 9.9}, provenance="history")
    assert curves.speed_ms(gradient=0.05, distance_m=0.0) == pytest.approx(9.9)


def test_unmeasured_bin_falls_back_to_minetti() -> None:
    curves = PacingCurves(speed_by_grade_bin={"+4": 9.9}, provenance="history")
    assert curves.speed_ms(gradient=-0.05, distance_m=0.0) != pytest.approx(9.9)


def test_unpaved_costs_speed() -> None:
    curves = PacingCurves()
    paved = curves.speed_ms(gradient=0.0, distance_m=0.0, unpaved=False)
    dirt = curves.speed_ms(gradient=0.0, distance_m=0.0, unpaved=True)
    assert dirt < paved


def test_population_curves_always_count_as_extrapolating() -> None:
    assert PacingCurves().extrapolates_beyond_history(5_000.0) is True


def test_history_shorter_than_half_the_target_is_extrapolation() -> None:
    """Scope 6.2 sets the threshold at 50% of target distance."""
    curves = PacingCurves(provenance="history", longest_effort_m=20_000.0)
    assert curves.extrapolates_beyond_history(30_000.0) is False
    assert curves.extrapolates_beyond_history(50_000.0) is True


# --- ETA vector -------------------------------------------------------------


def test_ten_flat_kilometres_at_six_minute_pace_takes_an_hour() -> None:
    vector = pacing_model(_route(), START)
    assert vector.total_time == timedelta(hours=1)
    assert vector.moving_time_s == pytest.approx(3600.0)


def test_etas_are_one_per_route_point() -> None:
    """Scope 3.3: every point has an arrival time, so scorers can index by point."""
    route = _route(n=51)
    assert len(pacing_model(route, START).etas) == len(route.points)


def test_etas_never_go_backwards() -> None:
    route = _route()
    etas = pacing_model(route, START, elevations=_ramp(101, 3.0)).etas
    assert all(a <= b for a, b in zip(etas[:-1], etas[1:], strict=True))


def test_climbing_takes_longer_than_flat() -> None:
    route = _route()
    flat = pacing_model(route, START)
    uphill = pacing_model(route, START, elevations=_ramp(101, 5.0))
    assert uphill.total_time > flat.total_time


def test_stop_allowance_is_added_to_the_finish() -> None:
    route = _route()
    without = pacing_model(route, START)
    with_stops = pacing_model(route, START, stop_allowance_s=600.0)
    assert with_stops.stopped_time_s == pytest.approx(600.0)
    assert (with_stops.finish - without.finish).total_seconds() == pytest.approx(600.0)


def test_mismatched_input_lengths_are_rejected() -> None:
    with pytest.raises(ValueError, match="elevations for"):
        pacing_model(_route(n=5), START, elevations=[1.0, 2.0])
    with pytest.raises(ValueError, match="surface flags"):
        pacing_model(_route(n=5), START, unpaved=[True])


def test_at_distance_interpolates_between_points() -> None:
    route = _route()
    vector = pacing_model(route, START)
    assert vector.at_distance(5000.0, route) == START + timedelta(minutes=30)


def test_at_distance_clamps_outside_the_route() -> None:
    route = _route()
    vector = pacing_model(route, START)
    assert vector.at_distance(-100.0, route) == vector.start
    assert vector.at_distance(99_999.0, route) == vector.finish


def test_segment_etas_use_the_midpoint() -> None:
    """A long segment's start can be many minutes early for a sun or heat question."""
    route = _route()
    segments = segment_route(route, max_len_m=1000.0)
    vector = pacing_model(route, START)
    etas = vector.for_segments(segments, route)
    assert len(etas) == len(segments)
    assert etas[0] > vector.start
    assert etas[-1] < vector.finish


def test_eta_vector_serializes() -> None:
    vector = pacing_model(_route(n=11), START)
    assert len(ETAVector.model_validate_json(vector.model_dump_json()).etas) == 11


# --- caveats (scope 12) -----------------------------------------------------


def test_population_curve_is_disclosed() -> None:
    caveats = " ".join(pacing_model(_route(), START).caveats)
    assert "population" in caveats.lower()
    assert "minetti" in caveats.lower()


def test_missing_elevation_is_disclosed() -> None:
    caveats = " ".join(pacing_model(_route(), START).caveats)
    assert "flat terrain" in caveats.lower()


def test_partial_elevation_gaps_are_counted() -> None:
    elevations: list[float | None] = [*_ramp(50, 2.0), *([None] * 51)]
    caveats = " ".join(pacing_model(_route(), START, elevations=elevations).caveats)
    assert "51 of 101" in caveats


def test_extrapolation_beyond_history_is_labelled_a_guess() -> None:
    curves = PacingCurves(provenance="history", longest_effort_m=20_000.0)
    route = _route(n=1001)  # 100 km
    caveats = " ".join(pacing_model(route, START, curves=curves).caveats)
    assert "20 km" in caveats
    assert "unreliable" in caveats


def test_a_well_supported_plan_carries_no_extrapolation_caveat() -> None:
    curves = PacingCurves(provenance="history", longest_effort_m=40_000.0)
    caveats = " ".join(
        pacing_model(_route(), START, curves=curves, elevations=_ramp(101, 1.0)).caveats
    )
    assert "unreliable" not in caveats
    assert "guess" not in caveats
