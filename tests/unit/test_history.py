"""Run history becomes a pacing curve (scope 6.2).

Hand-built activities whose answer is arithmetic: a runner holding exactly 3 m/s on the
flat for an hour produces a flat speed of 3 m/s, and anything else is a bug in the
derivation rather than a judgement call about somebody's training.

No FIT files here. The reader is a thin loop over `fitdecode` frames and a test of it
against a file this suite also *wrote* would mostly be testing the encoder; what can
actually be wrong is the mapping - semicircles, missing positions, the race flag - and that
is tested against hand-built frames. The reader meets a real device file when one is
dropped in `data/`, which is where it belongs and where this suite will never look.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from longrun.core.pacing.curves import PacingCurves
from longrun.core.pacing.history import (
    MIN_RUNS_FOR_ACCEPTED,
    PRIVACY_TRIM_M,
    Activity,
    TrackPoint,
    accepted_ways,
    derive,
    grade_bin,
    ingest,
)

START = datetime(2026, 3, 15, 7, 0)


def _run(
    activity_id: str = "a",
    *,
    metres: float = 6000.0,
    speed_ms: float = 3.0,
    gradient: float = 0.0,
    is_race: bool = False,
    step_m: float = 10.0,
) -> Activity:
    """A run held at one speed on one gradient, so the median is the speed."""
    points: list[TrackPoint] = []
    distance = 0.0
    seconds = 0.0
    while distance <= metres:
        points.append(
            TrackPoint(
                lat=37.77 + distance * 1e-5,
                lon=-122.41,
                cum_dist_m=distance,
                at=START + timedelta(seconds=seconds),
                ele_m=distance * gradient,
            )
        )
        distance += step_m
        seconds += step_m / speed_ms
    return Activity(activity_id=activity_id, points=points, is_race=is_race)


# --- the bin key, which must match the consumer exactly ----------------------


def test_the_bin_key_is_the_one_the_curve_looks_up() -> None:
    """A mismatch here is invisible: a missing bin is legal and falls back to Minetti, so
    binning differently would silently discard every measurement."""
    for gradient in (0.0, 0.019, 0.02, 0.05, -0.01, -0.03, -0.2):
        key = grade_bin(gradient)
        curves = PacingCurves(provenance="history", speed_by_grade_bin={key: 2.5})
        assert curves.speed_ms(gradient, 0.0) == pytest.approx(2.5, rel=1e-6), gradient


def test_the_key_floors_toward_minus_infinity() -> None:
    """-3% belongs in the -4 bin, not the -2 one. Truncation toward zero would put every
    shallow descent in the wrong place."""
    assert grade_bin(-0.03) == "-4"
    assert grade_bin(0.03) == "+2"
    assert grade_bin(0.0) == "+0"


# --- the derivation ----------------------------------------------------------


def test_a_runner_holding_one_speed_measures_that_speed() -> None:
    history = derive([_run(metres=6000.0, speed_ms=3.0)])

    assert history.curves.provenance == "history"
    assert history.curves.flat_speed_ms == pytest.approx(3.0, rel=1e-3)
    assert history.curves.longest_effort_m == pytest.approx(6000.0, abs=20.0)


def test_a_measured_curve_turns_off_the_population_caveat() -> None:
    """Which is the whole point of ingesting: `_caveats` emits the Minetti disclosure on
    `provenance == "population"`, and the extrapolation warning on `longest_effort_m`."""
    from longrun.core.pacing.model import _caveats

    measured = derive([_run(metres=30_000.0)]).curves
    text = " ".join(_caveats(measured, 20_000.0, [1.0, 2.0]))

    assert "population" not in text
    assert not measured.extrapolates_beyond_history(20_000.0)


def test_a_race_is_excluded_and_said_to_be(tmp_path: Any) -> None:
    """Scope 6.2: "race efforts excluded or flagged". Both - excluded from the numbers and
    flagged in the reasons, because a curve quietly built from four runs when six were
    supplied is a number nobody can check."""
    history = derive([_run("a"), _run("b"), _run("race", is_race=True)])

    assert history.races_excluded == 1
    assert history.activities_read == 3
    assert any("race" in reason for reason in history.reasons)


def test_a_thin_bin_is_left_to_the_population_curve_and_named() -> None:
    """Twenty samples is the floor. A bin with three is noise, and a curve that reported it
    as a measurement would be confidently wrong about exactly the gradients a runner
    encounters least."""
    history = derive([_run(metres=2000.0, step_m=100.0)], min_samples=20)

    assert history.curves.speed_by_grade_bin == {}
    assert any("fewer than 20 samples" in reason for reason in history.reasons)


def test_stopped_time_is_not_pace(tmp_path: Any) -> None:
    """Scope 6.2 excludes it. A traffic light held for two minutes would otherwise drag a
    median down by more than any hill."""
    moving = _run(metres=4000.0, speed_ms=3.0)
    paused = list(moving.points)
    # Ninety seconds standing still in the middle of the run.
    paused = [
        TrackPoint(
            lat=p.lat,
            lon=p.lon,
            cum_dist_m=p.cum_dist_m,
            at=p.at + timedelta(seconds=90),
            ele_m=p.ele_m,
        )
        if p.cum_dist_m > 2000.0
        else p
        for p in paused
    ]

    with_pause = derive([Activity(activity_id="p", points=paused)])

    assert with_pause.curves.flat_speed_ms == pytest.approx(3.0, rel=1e-2)


# --- privacy ------------------------------------------------------------------


def test_the_ends_of_every_activity_are_dropped_before_anything_reads_them() -> None:
    """Scope 6.2 and 3.7: a run starts where somebody lives, and the trim happens before
    binning and before map matching rather than as a filter on the output."""
    from longrun.core.pacing.history import _trimmed

    run = _run(metres=4000.0)
    kept = _trimmed(run.points)

    assert kept[0].cum_dist_m >= PRIVACY_TRIM_M
    assert kept[-1].cum_dist_m <= 4000.0 - PRIVACY_TRIM_M + 10.0


def test_a_run_shorter_than_the_trim_is_all_doorstep() -> None:
    """Dropped entirely rather than partly kept: there is nothing left of a 600 m run that
    is not somebody's address."""
    from longrun.core.pacing.history import _trimmed

    assert _trimmed(_run(metres=600.0).points) == []


# --- the accepted-road set ----------------------------------------------------


class _Matcher:
    """A router that reports the same ways for every activity it is given."""

    def __init__(self, ways: list[list[int | None]]) -> None:
        self.ways = ways
        self.calls = 0

    def map_match(self, track: Any) -> tuple[Any, list[int | None]]:
        answer = self.ways[min(self.calls, len(self.ways) - 1)]
        self.calls += 1
        return track, answer


def test_a_way_run_twice_is_accepted_and_one_run_once_is_not() -> None:
    """Scope 6.2: ways run at least twice, as a prior rather than a constraint."""
    router = _Matcher([[1, 1, 2], [1, 3, 3]])

    ways, reasons = accepted_ways([_run("a"), _run("b")], router, min_runs=MIN_RUNS_FOR_ACCEPTED)

    assert ways == frozenset({1})
    assert not reasons


def test_one_activity_that_will_not_match_does_not_lose_the_others() -> None:
    class _Flaky(_Matcher):
        def map_match(self, track: Any) -> tuple[Any, list[int | None]]:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("the matcher fell over")
            return track, [9, 9]

    router = _Flaky([[9]])
    ways, reasons = accepted_ways([_run("a"), _run("b"), _run("c")], router, min_runs=2)

    assert ways == frozenset({9})
    assert reasons and "RuntimeError" in reasons[0]


def test_without_a_router_the_set_is_empty_and_says_why() -> None:
    """ADR 0001 keyed this on real `osm_way_id` values and struck the geometry-hashing
    fallback, so there is nothing honest to produce without a matcher."""
    history = ingest([_run("a"), _run("b")], router=None)

    assert history.accepted_ways == frozenset()
    assert any("no router" in reason for reason in history.reasons)


# --- the profile --------------------------------------------------------------


def test_a_measured_curve_is_inferred_and_a_stated_one_still_wins() -> None:
    """Two provenance vocabularies, answering different questions. The curve says
    "history" - what measured it. The entry says INFERRED - who said so. Scope 6.3 keeps
    `stated` above `inferred`, so a runner who sets their own pace is not overwritten by
    their own watch.
    """
    from longrun.core.models.profile import PreferenceEntry, Provenance
    from longrun.core.preferences.store import load_defaults, merge

    stated = load_defaults().model_copy(
        update={
            "pacing": PreferenceEntry(
                value=PacingCurves(flat_speed_ms=2.5), provenance=Provenance.STATED
            )
        }
    )
    measured = derive([_run(metres=6000.0, speed_ms=3.0)]).curves
    incoming = stated.model_copy(
        update={"pacing": PreferenceEntry(value=measured, provenance=Provenance.INFERRED)}
    )

    merged, refused = merge(stated, incoming)

    assert merged.pacing.value.flat_speed_ms == pytest.approx(2.5)
    assert "pacing" in refused


def test_the_profile_default_is_the_population_curve() -> None:
    """So a profile with no history behind it produces exactly the ETAs this project has
    always produced - and `_caveats` still emits the Minetti disclosure."""
    from longrun.core.preferences.store import load_defaults

    assert load_defaults().pacing.value.provenance == "population"
