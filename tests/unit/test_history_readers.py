"""Reading activity files, and what a real archive taught the derivation (scope 6.2).

`test_history.py` promised the FIT mapping was "tested against hand-built frames", and no
test ever touched `read_fit`. This module is those tests, plus the ones a real Strava
archive made necessary: a `fitdecode` crash on a third-party developer field, device
elevation noisy enough to hide hills, and ride speeds that a pace curve must never see.

Still no committed device file. Frames and GPX are built here, so the arithmetic is known.
"""

from __future__ import annotations

import gzip
import io
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.unit.test_strava_export import gpx_track

from longrun.core.pacing.history import (
    PRIVACY_TRIM_M,
    STRIDE_M,
    Activity,
    TrackPoint,
    UnsupportedActivityFile,
    _strides,
    derive,
    grade_bin,
    read_activity,
    with_terrain,
)

START = datetime(2026, 3, 15, 13, 0, tzinfo=UTC)


def _track(
    *,
    metres: float = 4000.0,
    step_m: float = 5.0,
    speed_ms: float = 3.0,
    elevation: Any = lambda d: 100.0,
    sport: str | None = None,
) -> Activity:
    points = []
    distance = 0.0
    while distance <= metres:
        points.append(
            TrackPoint(
                lat=39.0 + distance * 9e-6,
                lon=-98.0,
                cum_dist_m=distance,
                at=START + timedelta(seconds=distance / speed_ms),
                ele_m=elevation(distance),
            )
        )
        distance += step_m
    return Activity(activity_id="t", points=points, sport=sport)


# --- GPX ------------------------------------------------------------------------


def test_a_gpx_track_measures_its_own_distance_and_names_its_sport() -> None:
    activity = read_activity("activities/42.gpx", gpx_track(metres=2000.0))

    assert activity is not None
    assert activity.activity_id == "42"
    assert activity.sport == "running"
    # Steps of 0.0001 degrees of latitude, 11.12 m each on a great circle.
    assert activity.distance_m == pytest.approx((len(activity.points) - 1) * 11.1195, rel=1e-4)


def test_a_gzipped_file_reads_the_same_as_the_plain_one() -> None:
    plain = read_activity("1.gpx", gpx_track())
    zipped = read_activity("1.gpx.gz", gzip.compress(gpx_track()))

    assert plain == zipped


def test_a_gpx_point_without_a_time_is_shape_not_pace() -> None:
    body = gpx_track(metres=500.0).replace(b"<time>", b"<desc>").replace(b"</time>", b"</desc>")

    assert read_activity("1.gpx", body) is None


def test_a_format_with_no_reader_is_refused_by_name() -> None:
    with pytest.raises(UnsupportedActivityFile, match="1.tcx"):
        read_activity("activities/1.tcx.gz", gzip.compress(b"<TrainingCenterDatabase/>"))


# --- FIT, frame by frame ---------------------------------------------------------


class _Frame:
    """A `fitdecode` data frame: a name and fields, nothing else."""

    def __init__(self, name: str, **fields: Any) -> None:
        import fitdecode

        self.frame_type = fitdecode.FIT_FRAME_DATA
        self.name = name
        self.fields = fields

    def get_value(self, field: str) -> Any:
        if field not in self.fields:
            raise KeyError(field)
        return self.fields[field]


def _fit(monkeypatch: pytest.MonkeyPatch, frames: list[_Frame]) -> Activity | None:
    from longrun.core.pacing import history

    class _Reader:
        def __init__(self, *_: Any, **__: Any) -> None: ...
        def __enter__(self) -> list[_Frame]:
            return frames

        def __exit__(self, *_: Any) -> None: ...

    monkeypatch.setattr(history, "_fit_reader", lambda: _Reader)
    return read_activity("activities/7.fit.gz", gzip.compress(b"frames are faked"))


def _record(distance: float, **extra: Any) -> _Frame:
    fields = {
        "position_lat": round(39.0 * 2**31 / 180),  # semicircles, as FIT stores them
        "position_long": round(-98.0 * 2**31 / 180),
        "timestamp": START + timedelta(seconds=distance / 3.0),
        "distance": distance,
        **extra,
    }
    return _Frame("record", **fields)


def test_semicircles_become_degrees(monkeypatch: pytest.MonkeyPatch) -> None:
    activity = _fit(monkeypatch, [_record(0.0), _record(10.0)])

    assert activity is not None
    assert activity.points[0].lat == pytest.approx(39.0, abs=1e-4)
    assert activity.points[0].lon == pytest.approx(-98.0, abs=1e-4)


def test_enhanced_altitude_is_preferred_over_the_16_bit_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = _fit(
        monkeypatch,
        [
            _record(0.0, enhanced_altitude=312.4, altitude=300.0),
            _record(10.0, altitude=301.0),
        ],
    )

    assert activity is not None
    assert [p.ele_m for p in activity.points] == [312.4, 301.0]


def test_a_record_without_a_position_is_not_a_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """A heart-rate sample. Interpolating a position for it would invent an accepted road."""
    bare = _Frame("record", timestamp=START, distance=5.0, heart_rate=150)
    activity = _fit(monkeypatch, [_record(0.0), bare, _record(10.0)])

    assert activity is not None
    assert len(activity.points) == 2


def test_sport_comes_from_the_session_when_there_is_no_sport_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity = _fit(
        monkeypatch,
        [_record(0.0), _record(10.0), _Frame("session", sport="cycling", sub_sport="road")],
    )

    assert activity is not None
    assert activity.sport == "cycling"


def test_a_treadmill_run_has_no_track(monkeypatch: pytest.MonkeyPatch) -> None:
    moving = _Frame("record", timestamp=START, distance=5.0)

    assert _fit(monkeypatch, [_Frame("sport", sport="running"), moving]) is None


class _DevFieldDescription:
    """A `field_description` naming developer data index 1, which nothing declared."""

    local_mesg_num = 0

    def get_raw_value(self, name: str) -> Any:
        values = {"developer_data_index": 1, "field_definition_number": 0}
        if name not in values:
            raise KeyError(name)
        return values[name]


def test_fitdecode_itself_crashes_on_an_undeclared_developer_index() -> None:
    """The control. If this stops raising, `_fit_reader`'s override is dead weight and the
    pinned `fitdecode` has fixed it upstream."""
    import fitdecode

    reader = fitdecode.FitReader(io.BytesIO(b""), error_handling=fitdecode.ErrorHandling.IGNORE)

    with pytest.raises(fitdecode.FitParseError, match="developer_data_index 1 not defined"):
        reader._add_dev_field_description(_DevFieldDescription())


def test_the_reader_registers_the_undeclared_index_instead() -> None:
    import fitdecode

    from longrun.core.pacing.history import _fit_reader

    reader = _fit_reader()(io.BytesIO(b""), error_handling=fitdecode.ErrorHandling.IGNORE)
    reader._add_dev_field_description(_DevFieldDescription())

    assert 0 in reader._local_dev_types[1]["fields"]


# --- what gets binned -------------------------------------------------------------


def test_a_ride_never_reaches_the_pace_curve() -> None:
    """A cycling median near 8 m/s would otherwise become the runner's flat speed."""
    history = derive([_track(speed_ms=3.0, sport="running"), _track(speed_ms=8.0, sport="cycling")])

    assert history.curves.flat_speed_ms == pytest.approx(3.0, rel=1e-3)
    assert any("not running (cycling 1)" in reason for reason in history.reasons)


def test_an_activity_that_names_no_sport_is_binned_and_counted() -> None:
    history = derive([_track(sport="running"), _track(sport=None)])

    assert any("name no sport" in reason for reason in history.reasons)


def test_elevation_noise_is_not_a_hill() -> None:
    """A steady 5% climb whose altimeter alternates +/-0.6 m every 5 m.

    Point to point, each step reads +29% or -19% and not one lands in the +4 bin. Over
    the stride a plan grades on, the noise cancels and every stride says 5%.
    """

    def noisy(d: float) -> float:
        return 100.0 + 0.05 * d + (0.6 if int(d / 5.0) % 2 else -0.6)

    climb = _track(elevation=noisy)

    per_pair = [grade_bin(g) for g, _ in _strides(climb, stride_m=0.0) if g is not None]
    per_stride = [grade_bin(g) for g, _ in _strides(climb, stride_m=STRIDE_M) if g is not None]

    assert "+4" not in per_pair
    assert per_stride and set(per_stride) == {"+4"}


def test_missing_elevation_is_an_unknown_grade_not_a_cliff() -> None:
    """The reader used to subtract a known elevation from an absent one as though it were
    zero, filing a run at 100 m altitude under a -1000% bin."""
    gappy = _track(elevation=lambda d: 100.0 if d < 2000.0 else None)

    grades = [g for g, _ in _strides(gappy) if g is not None]

    assert grades and all(abs(g) < 0.01 for g in grades)
    assert any(g is None for g, _ in _strides(gappy))


def test_a_stop_ends_a_stride_rather_than_diluting_one() -> None:
    run = _track(metres=4000.0)
    paused = [
        TrackPoint(p.lat, p.lon, p.cum_dist_m, p.at + timedelta(seconds=120), p.ele_m)
        if p.cum_dist_m > 2000.0
        else p
        for p in run.points
    ]

    speeds = [s for _, s in _strides(Activity(activity_id="p", points=paused))]

    assert min(speeds) == pytest.approx(3.0, rel=1e-3)


# --- terrain ---------------------------------------------------------------------


def test_terrain_replaces_device_elevation_inside_the_trim_and_nowhere_else() -> None:
    asked: list[Any] = []

    def sample(route: Any) -> list[float | None]:
        asked.append(route)
        return [42.0] * len(route.points)

    device = _track(metres=3000.0, elevation=lambda d: 7.0)
    (on_terrain,), uncovered = with_terrain([device], sample)

    assert uncovered == 0
    for point in on_terrain.points:
        inside = PRIVACY_TRIM_M <= point.cum_dist_m <= 3000.0 - PRIVACY_TRIM_M
        assert point.ele_m == (42.0 if inside else None)
    # The terrain lookup is a question about where somebody was. The ends are never asked.
    southern, northern = 39.0 + PRIVACY_TRIM_M * 9e-6, 39.0 + (3000.0 - PRIVACY_TRIM_M) * 9e-6
    assert all(southern - 1e-9 <= p.lat <= northern + 1e-9 for p in asked[0].points)


def test_a_run_with_no_terrain_under_it_is_counted_and_grades_nothing() -> None:
    (abroad,), uncovered = with_terrain([_track()], lambda route: [None] * len(route.points))

    assert uncovered == 1
    assert derive([abroad]).curves.speed_by_grade_bin == {}


def test_the_curve_says_which_elevation_its_bins_were_measured_on() -> None:
    from longrun.core.pacing.model import _caveats

    run = _track(metres=30_000.0)
    device = derive([run]).curves
    terrain = derive([run], elevation="terrain").curves

    assert device.grade_elevation == "device"
    assert terrain.grade_elevation == "terrain"
    assert any("device's own elevation" in c for c in _caveats(device, 20_000.0, [1.0]))
    assert not any("device's own elevation" in c for c in _caveats(terrain, 20_000.0, [1.0]))
