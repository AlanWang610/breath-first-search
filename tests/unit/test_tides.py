"""The coastal classifier and the NOAA CO-OPS client, without a network (scope 7.6, 4.4).

Split the way `test_forecast.py` splits, and for the same reason. The vocabulary and the
parsers are pure functions over literal inputs and are pinned exactly — including the
awkward parts, which is where the bugs are: CO-OPS reports a refused request as **HTTP 200
with an error object**, and a classifier built on `surface=sand` would call a Sonoran desert
track a beach.

The fetching half runs through the cache in offline mode, which is the mechanism a golden
route uses. Nothing here has a golden to run against: **no route in the suite crosses a
tidal segment**, and no golden fixture's `ways` layer carries `tidal`, `natural`, `ford` or
a soft surface at all — measured, not assumed, before the classifier was written. So these
tests are the only coverage the classifier has, and what that leaves unverified is the join
between a real OSM extract and `coastal_kind`: nothing proves that a way a mapper tagged
`tidal=yes` survives `WAY_KEYS` into `osm.ways` with that tag intact. It should — the loader
stores the whole tag dict — but it is untested against a `.pbf`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest

from longrun.core.data.cache import STATIC_DAY, SqliteCache, args_hash
from longrun.core.data.file_store import FileLayerStore, FileRasterStore
from longrun.core.data.tides import (
    SOURCE,
    StationTides,
    TideStation,
    nearest_station,
    parse_predictions,
    parse_stations,
    predictions_args,
    station_tides,
    stations_args,
)
from longrun.core.models.context import Budget, FrozenClock, ScorerContext
from longrun.core.models.coverage import CoverageManifest
from longrun.core.models.geometry import Segment
from longrun.core.models.profile import PreferenceProfile
from longrun.core.scorers._coastal import coastal_kind, coastal_stretches

DAY = date(2026, 9, 15)
START = datetime(2026, 9, 15, 7, 0)

#: Ocean Beach, San Francisco, and the CO-OPS gauge that governs it.
BEACH_LAT, BEACH_LON = 37.7595, -122.5107
PRESIDIO = TideStation(id="9414290", name="San Francisco", lat=37.8063, lon=-122.4659)


def _ctx(tmp_path: Any, cache: SqliteCache, api_calls_max: int = 200) -> ScorerContext:
    return ScorerContext(
        layers=FileLayerStore(tmp_path),
        rasters=FileRasterStore(tmp_path),
        cache=cache,
        clock=FrozenClock(START),
        coverage=CoverageManifest(),
        profile=PreferenceProfile(),
        budget=Budget(api_calls_max=api_calls_max),
    )


# --- the classifier ---------------------------------------------------------


def test_a_way_tagged_tidal_is_tidal() -> None:
    """The only categorical statement available: the mapper says the water covers it."""
    assert coastal_kind({"highway": "path", "tidal": "yes"}) == "tidal"
    assert coastal_kind({"highway": "track", "tidal": "1", "ford": "yes"}) == "tidal"


def test_tidal_no_is_a_statement_that_it_does_not_flood() -> None:
    """`tidal=no` is positive evidence the other way, not a missing tag."""
    assert coastal_kind({"highway": "path", "tidal": "no"}) is None


def test_a_beach_way_is_classified_separately_from_a_tidal_one() -> None:
    """Different evidence, so a different class - and in `access_hours`, a different tier."""
    assert coastal_kind({"highway": "footway", "natural": "beach"}) == "beach"
    assert coastal_kind({"highway": "path", "natural": "shoal"}) == "beach"


def test_a_sand_surface_alone_is_not_a_beach() -> None:
    """The rejected third rule, and the route it would have been wrong about.

    `highway=track surface=sand` is a desert track, and `phoenix-heat` is a golden. A
    heuristic whose false positive is a Sonoran wash does not get to raise a safety-tier
    flag about the tide.
    """
    assert coastal_kind({"highway": "track", "surface": "sand"}) is None
    assert coastal_kind({"highway": "path", "surface": "mud"}) is None


def test_an_unmatched_segment_says_nothing_rather_than_nothing_here() -> None:
    """Scope 12: no tags is unknown. `None` here means "this did not say", not "no tide"."""
    assert coastal_kind(None) is None
    assert coastal_kind({}) is None


def _segments(count: int, way_ids: list[int | None]) -> list[Segment]:
    return [
        Segment(
            id=f"s{i:05d}",
            index=i,
            start_idx=i * 2,
            end_idx=i * 2 + 2,
            cum_start_m=i * 100.0,
            length_m=100.0,
            way_id=way_ids[i],
        )
        for i in range(count)
    ]


def test_contiguous_tidal_segments_become_one_stretch() -> None:
    """A causeway is one question asked once, not one question per scoring segment."""
    segments = _segments(5, [1, 2, 2, 2, 1])
    stretches = coastal_stretches(segments, {1: {"highway": "residential"}, 2: {"tidal": "yes"}})

    assert len(stretches) == 1
    stretch = stretches[0]
    assert stretch.kind == "tidal"
    assert stretch.segment_ids == ("s00001", "s00002", "s00003")
    assert (stretch.cum_start_m, stretch.cum_end_m) == (100.0, 400.0)
    assert stretch.length_m == 300.0
    assert stretch.mid_route_index == 4  # the middle segment's own start index


def test_a_run_breaks_on_ground_nothing_said_was_tidal() -> None:
    """Bridging an unknown gap would extend a tidal claim over ground nothing claimed."""
    segments = _segments(5, [2, None, 2, 2, 1])
    stretches = coastal_stretches(segments, {1: {"highway": "residential"}, 2: {"tidal": "yes"}})

    assert [s.segment_ids for s in stretches] == [("s00000",), ("s00002", "s00003")]


def test_a_beach_run_and_a_tidal_run_do_not_merge() -> None:
    """Different evidence classes carry different confidence, so they stay separate."""
    segments = _segments(4, [2, 2, 3, 3])
    stretches = coastal_stretches(segments, {2: {"tidal": "yes"}, 3: {"natural": "beach"}})

    assert [(s.kind, s.length_m) for s in stretches] == [("tidal", 200.0), ("beach", 200.0)]
    assert stretches[0].confidence > stretches[1].confidence


def test_a_route_with_no_coastal_tag_produces_no_stretches() -> None:
    """The "measured as none" answer, and the one every golden route takes."""
    segments = _segments(4, [1, 1, 1, 1])
    assert coastal_stretches(segments, {1: {"highway": "residential", "surface": "asphalt"}}) == []


# --- parsing ----------------------------------------------------------------


def _hilo_payload() -> dict[str, Any]:
    return {
        "predictions": [
            {"t": "2026-09-15 03:41", "v": "0.312", "type": "L"},
            {"t": "2026-09-15 10:02", "v": "1.688", "type": "H"},
            {"t": "2026-09-15 16:55", "v": "0.104", "type": "L"},
            {"t": "2026-09-15 23:19", "v": "1.902", "type": "H"},
        ]
    }


def test_a_hilo_payload_becomes_extremes_in_time_order() -> None:
    extremes = parse_predictions(_hilo_payload())
    assert [e.kind for e in extremes] == ["low", "high", "low", "high"]
    assert extremes[1].time == datetime(2026, 9, 15, 10, 2)
    assert extremes[1].height_m == pytest.approx(1.688)


def test_an_error_body_returned_with_http_200_parses_to_nothing() -> None:
    """The single most likely way this client goes quietly wrong.

    CO-OPS answers a refused request with a 200 and an `error` object, so `raise_for_status`
    sees success. Reading that as an empty tide would report "no high water today".
    """
    assert parse_predictions({"error": {"message": "No data was found."}}) == []


def test_a_prediction_row_with_no_height_still_carries_its_time() -> None:
    """The time is what the conflict test needs; the height is for a rule nobody wrote yet."""
    extremes = parse_predictions({"predictions": [{"t": "2026-09-15 10:02", "type": "H"}]})
    assert len(extremes) == 1
    assert extremes[0].height_m is None


def test_a_station_row_with_no_position_is_skipped_rather_than_defaulted() -> None:
    """A station at (0, 0) is in the Gulf of Guinea and would win every distance test."""
    stations = parse_stations(
        {
            "stations": [
                {"id": "9414290", "name": "San Francisco", "lat": 37.8063, "lng": -122.4659},
                {"id": "broken", "name": "No position"},
            ]
        }
    )
    assert [s.id for s in stations] == ["9414290"]


def test_the_nearest_station_is_the_nearest_by_great_circle() -> None:
    """Not along the route: a tide station is an external gauge, not a sample of the line."""
    far = TideStation(id="9410170", name="San Diego", lat=32.7142, lon=-117.1736)
    found = nearest_station([far, PRESIDIO], BEACH_LAT, BEACH_LON)
    assert found is not None
    station, distance_m = found
    assert station.id == PRESIDIO.id
    assert 5_000.0 < distance_m < 20_000.0


def test_cache_key_inputs_are_stable_and_separate_the_two_calls() -> None:
    """A cassette is worthless if these drift, and one key for both would be worse."""
    assert args_hash(stations_args()) == args_hash(stations_args())
    assert args_hash(predictions_args("9414290", DAY)) != args_hash(
        predictions_args("9414290", date(2026, 9, 16))
    )
    assert args_hash(predictions_args("9414290", DAY)) != args_hash(
        predictions_args("9410170", DAY)
    )


# --- reading a tide back ----------------------------------------------------


def _tides() -> StationTides:
    return StationTides(
        station=PRESIDIO,
        distance_m=9_500.0,
        extremes=parse_predictions(_hilo_payload()),
    )


def test_an_arrival_inside_the_window_conflicts_with_the_nearer_high_water() -> None:
    conflict = _tides().high_water_conflict(datetime(2026, 9, 15, 10, 40))
    assert conflict is not None
    assert conflict.time == datetime(2026, 9, 15, 10, 2)


def test_an_arrival_at_low_water_does_not_conflict() -> None:
    """The "measured as none" answer for a stretch that was genuinely checked."""
    assert _tides().high_water_conflict(datetime(2026, 9, 15, 16, 55)) is None


def test_the_window_is_symmetric_about_high_water() -> None:
    high = datetime(2026, 9, 15, 10, 2)
    assert _tides().high_water_conflict(high - timedelta(minutes=89)) is not None
    assert _tides().high_water_conflict(high - timedelta(minutes=91)) is None
    assert _tides().high_water_conflict(high + timedelta(minutes=89)) is not None
    assert _tides().high_water_conflict(high + timedelta(minutes=91)) is None


def test_the_wait_is_measured_to_the_next_low_water() -> None:
    """A tide is not bought back by starting earlier; it is bought back by waiting."""
    assert _tides().minutes_to_next_low(datetime(2026, 9, 15, 10, 2)) == pytest.approx(413.0)


def test_a_tide_that_never_falls_again_inside_the_series_says_so() -> None:
    """Two days of predictions end somewhere, and `None` is that answer rather than zero."""
    assert _tides().minutes_to_next_low(datetime(2026, 9, 16, 12, 0)) is None


def test_coverage_distinguishes_a_station_that_answered_from_one_that_did_not() -> None:
    answered = _tides().coverage("tides")
    assert answered.checked and answered.source == SOURCE
    assert "San Francisco (9414290)" in (answered.reason or "")

    silent = StationTides(reason="not in the cassette").coverage("tides")
    assert not silent.checked
    assert silent.reason == "not in the cassette"


# --- fetching, offline ------------------------------------------------------


def test_offline_with_an_empty_cassette_reports_rather_than_raises(tmp_path: Any) -> None:
    """A scorer inside the plan loop gets a recorded reason, never an exception."""
    with SqliteCache(offline=True) as cache:
        result = station_tides(_ctx(tmp_path, cache), BEACH_LAT, BEACH_LON, DAY)

    assert not result.answered
    assert result.station is None
    assert "not in the cassette" in (result.reason or "")


def _record(path: Any, stations: list[dict[str, Any]] | None = None) -> None:
    with SqliteCache(path) as recording:
        recording.put(
            f"{SOURCE}.stations",
            args_hash(stations_args()),
            STATIC_DAY,
            {
                "stations": stations
                if stations is not None
                else [
                    {
                        "id": PRESIDIO.id,
                        "name": PRESIDIO.name,
                        "lat": PRESIDIO.lat,
                        "lng": PRESIDIO.lon,
                    }
                ]
            },
        )
        recording.put(
            f"{SOURCE}.predictions",
            args_hash(predictions_args(PRESIDIO.id, DAY)),
            DAY.isoformat(),
            _hilo_payload(),
        )


def test_a_recorded_cassette_serves_the_tide_offline_and_costs_nothing(tmp_path: Any) -> None:
    """The budget is charged inside the producer, so a replay is free (scope 6.4)."""
    path = tmp_path / "cassette.sqlite"
    _record(path)

    with SqliteCache(path, offline=True) as cache:
        ctx = _ctx(tmp_path, cache)
        result = station_tides(ctx, BEACH_LAT, BEACH_LON, DAY)

    assert result.answered
    assert result.station is not None and result.station.id == PRESIDIO.id
    assert ctx.budget.api_calls_used == 0


def test_a_station_beyond_reach_is_reported_as_not_governing_the_stretch(tmp_path: Any) -> None:
    """The `GATE_REACH_M` question, answered with its own number rather than the gate's.

    A gauge 600 km away is not the tide at this beach, and saying "no station governs this
    stretch" is a different answer from "the endpoint failed".
    """
    path = tmp_path / "far.sqlite"
    _record(path, stations=[{"id": "9410170", "name": "San Diego", "lat": 32.7142, "lng": -117.17}])

    with SqliteCache(path, offline=True) as cache:
        result = station_tides(_ctx(tmp_path, cache), BEACH_LAT, BEACH_LON, DAY)

    assert not result.answered
    assert result.station is None
    assert "beyond the 50 km" in (result.reason or "")


def test_an_error_body_in_the_cassette_becomes_a_reason_not_an_empty_tide(tmp_path: Any) -> None:
    path = tmp_path / "error.sqlite"
    _record(path)
    with SqliteCache(path) as recording:
        recording.put(
            f"{SOURCE}.predictions",
            args_hash(predictions_args(PRESIDIO.id, DAY)),
            DAY.isoformat(),
            {"error": {"message": "No data was found."}},
        )

    with SqliteCache(path, offline=True) as cache:
        result = station_tides(_ctx(tmp_path, cache), BEACH_LAT, BEACH_LON, DAY)

    assert not result.answered
    assert result.station is not None  # the station was found; it is the tide that was not
    assert "no high or low waters" in (result.reason or "")


def test_an_exhausted_budget_is_reported_as_a_resource_decision(tmp_path: Any) -> None:
    """Not an error in the data. `BudgetExceeded` must not escape into the scoring loop."""
    with SqliteCache() as cache:
        ctx = _ctx(tmp_path, cache, api_calls_max=0)
        result = station_tides(ctx, BEACH_LAT, BEACH_LON, DAY)

    assert not result.answered
    assert "budget exhausted" in (result.reason or "")


def test_the_caller_identity_string_is_not_in_the_cache_key() -> None:
    """A cassette must not stop replaying because somebody renamed the caller.

    `application` is a courtesy parameter CO-OPS does not enforce and that changes nothing
    about the answer, so it belongs in the request and not in the hash.
    """
    from longrun.core.data.tides import APPLICATION

    assert APPLICATION not in str(predictions_args(PRESIDIO.id, DAY))
    assert APPLICATION not in str(stations_args())
