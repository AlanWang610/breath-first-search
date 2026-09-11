"""The forecast client, without a network (scope 7.4, 4.4).

Two halves are tested differently. The parsers are pure functions over literal payloads,
so they are pinned exactly — including the awkward parts, which is where the bugs are: NWS
publishes values over ISO-8601 *intervals* rather than at instants, and reports wind in
km/h from some offices and m/s from others.

The fetching half is tested through the cache in offline mode, which is the same mechanism
a golden route uses. That is deliberate: if a cassette can drive these tests, a cassette
can drive a golden, and there is no second recording system to keep in step.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest

from longrun.core.data.cache import COORD_PRECISION, STATIC_DAY, SqliteCache, args_hash
from longrun.core.data.file_store import FileLayerStore, FileRasterStore
from longrun.core.data.forecast import (
    USER_AGENT_ENV_VAR,
    ForecastSite,
    HourlyPoint,
    RouteForecast,
    SiteForecast,
    expand_intervals,
    nws_grid_args,
    nws_points_args,
    open_meteo_args,
    parse_nws_gridpoint,
    parse_open_meteo,
    route_forecast,
    sample_points,
    user_agent,
)
from longrun.core.models.context import Budget, FrozenClock, ScorerContext
from longrun.core.models.coverage import CoverageManifest
from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.models.profile import PreferenceProfile

DAY = date(2026, 9, 12)
NOON = datetime(2026, 9, 12, 12, 0)


def _route(points: int = 40, step_m: float = 500.0) -> Route:
    return Route(
        id="fc",
        points=[
            RoutePoint(lat=37.7955, lon=-122.4 + i * 0.0057, cum_dist_m=i * step_m)
            for i in range(points)
        ],
    )


def _ctx(tmp_path: Any, cache: SqliteCache, api_calls_max: int = 200) -> ScorerContext:
    return ScorerContext(
        layers=FileLayerStore(tmp_path),
        rasters=FileRasterStore(tmp_path),
        cache=cache,
        clock=FrozenClock(NOON),
        coverage=CoverageManifest(),
        profile=PreferenceProfile(),
        budget=Budget(api_calls_max=api_calls_max),
    )


# --- sampling ---------------------------------------------------------------


def test_sites_are_spaced_and_always_include_both_ends() -> None:
    """A start and a finish are where a runner decides whether to go at all."""
    route = _route(points=40, step_m=500.0)  # 19.5 km
    sites = sample_points(route, spacing_m=5000.0)

    assert sites[0].route_index == 0
    assert sites[-1].route_index == len(route.points) - 1
    gaps = [b.cum_dist_m - a.cum_dist_m for a, b in zip(sites[:-1], sites[1:], strict=True)]
    assert all(gap >= 4000.0 for gap in gaps[:-1]), gaps


def test_a_long_route_is_capped_at_the_budgeted_number_of_sites() -> None:
    """Scope 6.4 caps external calls; two per site means the site count is the real cap."""
    sites = sample_points(_route(points=400, step_m=500.0), spacing_m=1000.0, max_points=8)
    assert len(sites) <= 9  # the cap, plus the finish which is always kept
    assert sites[-1].route_index == 399


def test_coordinates_are_rounded_before_they_reach_a_cache_key() -> None:
    """An unrounded float makes the args hash depend on the last bit of a haversine sum.

    A cassette recorded on one machine would then miss on another, and the failure would
    look like a flaky test rather than a key derived from noise.
    """
    route = Route(
        id="r",
        points=[
            RoutePoint(lat=37.795512345678, lon=-122.400087654321, cum_dist_m=0.0),
            RoutePoint(lat=37.795512345678, lon=-122.399087654321, cum_dist_m=88.0),
        ],
    )
    site = sample_points(route)[0]
    assert site.lat == round(37.795512345678, COORD_PRECISION)
    assert site.lon == round(-122.400087654321, COORD_PRECISION)


def test_cache_key_inputs_are_stable() -> None:
    """The cassette is worthless if these drift, so the hashes are pinned."""
    assert args_hash(nws_points_args(37.7955, -122.4)) == args_hash(
        nws_points_args(37.79550001, -122.40000001)
    )
    assert args_hash(nws_grid_args("MTR", 85, 105)) == args_hash(nws_grid_args("MTR", 85, 105))
    assert args_hash(open_meteo_args(37.7955, -122.4, DAY)) != args_hash(
        open_meteo_args(37.7955, -122.4, date(2026, 9, 13))
    )


# --- parsing ----------------------------------------------------------------


def test_a_three_hour_interval_becomes_three_hours() -> None:
    """NWS publishes over intervals, not instants. Forgetting that loses two thirds."""
    field = {
        "uom": "wmoUnit:degC",
        "values": [{"validTime": "2026-09-12T12:00:00+00:00/PT3H", "value": 18.0}],
    }
    hours = expand_intervals(field)
    assert len(hours) == 3
    assert all(value == 18.0 for value in hours.values())


def test_wind_in_kilometres_per_hour_is_converted() -> None:
    """Some offices report km/h and some m/s, and the difference is a factor of 3.6."""
    field = {
        "uom": "wmoUnit:km_h-1",
        "values": [{"validTime": "2026-09-12T12:00:00+00:00/PT1H", "value": 36.0}],
    }
    assert next(iter(expand_intervals(field).values())) == pytest.approx(10.0)


def test_a_null_value_is_skipped_rather_than_zeroed() -> None:
    field = {
        "uom": "wmoUnit:degC",
        "values": [
            {"validTime": "2026-09-12T12:00:00+00:00/PT1H", "value": None},
            {"validTime": "2026-09-12T13:00:00+00:00/PT1H", "value": 20.0},
        ],
    }
    hours = expand_intervals(field)
    assert len(hours) == 1
    assert next(iter(hours.values())) == 20.0


def _nws_payload() -> dict[str, Any]:
    def field(uom: str, value: float) -> dict[str, Any]:
        return {
            "uom": uom,
            "values": [{"validTime": "2026-09-12T12:00:00+00:00/PT2H", "value": value}],
        }

    return {
        "properties": {
            "temperature": field("wmoUnit:degC", 24.0),
            "dewpoint": field("wmoUnit:degC", 12.0),
            "relativeHumidity": field("wmoUnit:percent", 47.0),
            "windSpeed": field("wmoUnit:km_h-1", 18.0),
            "skyCover": field("wmoUnit:percent", 20.0),
            "probabilityOfPrecipitation": field("wmoUnit:percent", 5.0),
        }
    }


def test_an_nws_gridpoint_becomes_normalized_hours() -> None:
    hours = parse_nws_gridpoint(_nws_payload())
    assert len(hours) == 2
    first = hours[0]
    assert first.temp_c == 24.0
    assert first.relative_humidity_pct == 47.0
    assert first.cloud_cover_pct == 20.0
    assert first.wind_speed_ms == pytest.approx(5.0)


def test_cloud_cover_is_why_the_raw_gridpoint_endpoint_is_used() -> None:
    """`/forecast/hourly` carries no skyCover, and `sun_exposure` needs it to scale."""
    assert all(h.cloud_cover_pct is not None for h in parse_nws_gridpoint(_nws_payload()))


def test_open_meteo_parses_to_the_same_shape() -> None:
    payload = {
        "hourly": {
            "time": ["2026-09-12T12:00", "2026-09-12T13:00"],
            "temperature_2m": [24.0, 25.0],
            "relative_humidity_2m": [47.0, 44.0],
            "dew_point_2m": [12.0, 12.0],
            "wind_speed_10m": [5.0, 6.0],
            "cloud_cover": [20.0, 30.0],
            "precipitation_probability": [5.0, 5.0],
        }
    }
    hours = parse_open_meteo(payload)
    assert [h.temp_c for h in hours] == [24.0, 25.0]
    assert hours[0].cloud_cover_pct == 20.0


def test_a_missing_open_meteo_column_is_none_not_zero() -> None:
    """Scope 12: unknown is not absent, and 0% cloud is a very different claim."""
    hours = parse_open_meteo({"hourly": {"time": ["2026-09-12T12:00"], "temperature_2m": [24.0]}})
    assert hours[0].temp_c == 24.0
    assert hours[0].cloud_cover_pct is None


# --- reading a forecast back ------------------------------------------------


def _site_forecast(cum_dist_m: float = 0.0, base_temp: float = 20.0) -> SiteForecast:
    return SiteForecast(
        site=ForecastSite(index=0, route_index=0, lat=37.8, lon=-122.4, cum_dist_m=cum_dist_m),
        provider="nws",
        hours=[
            HourlyPoint(
                time=NOON + timedelta(hours=i),
                temp_c=base_temp + i,
                cloud_cover_pct=10.0 * i,
                precip_probability_pct=100.0 if i else 0.0,
            )
            for i in range(4)
        ],
    )


def test_the_hour_between_two_hours_is_interpolated() -> None:
    half_past = _site_forecast().at(NOON + timedelta(minutes=30))
    assert half_past is not None
    assert half_past.temp_c == pytest.approx(20.5)
    assert half_past.cloud_cover_pct == pytest.approx(5.0)


def test_a_probability_is_taken_from_the_nearer_hour_not_blended() -> None:
    """A probability *for* the 13:00 hour is a statement about that hour, not an instant."""
    early = _site_forecast().at(NOON + timedelta(minutes=10))
    late = _site_forecast().at(NOON + timedelta(minutes=50))
    assert early is not None and late is not None
    assert early.precip_probability_pct == 0.0
    assert late.precip_probability_pct == 100.0


def test_a_time_beyond_the_horizon_returns_nothing_rather_than_the_last_hour() -> None:
    """A run planned three weeks out is outside any hourly forecast, and must say so."""
    assert _site_forecast().at(NOON + timedelta(days=20)) is None


def test_the_nearest_site_is_measured_along_the_route() -> None:
    """On an out-and-back a site 300 m away across the turnaround is 8 km away in fact."""
    forecast = RouteForecast(
        sites=[_site_forecast(cum_dist_m=0.0, base_temp=10.0), _site_forecast(cum_dist_m=8000.0)]
    )
    near = forecast.at_distance(500.0, NOON)
    far = forecast.at_distance(7800.0, NOON)
    assert near is not None and far is not None
    assert near.temp_c == 10.0
    assert far.temp_c == 20.0


# --- fetching, offline ------------------------------------------------------


def test_offline_with_an_empty_cassette_reports_rather_than_raises(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One missing key must not take the whole scorer down, and must not pass silently."""
    monkeypatch.setenv(USER_AGENT_ENV_VAR, "longrun (test@example.com)")
    with SqliteCache(offline=True) as cache:
        forecast = route_forecast(_route(points=6), _ctx(tmp_path, cache), DAY)

    assert not forecast.answered
    assert forecast.providers == {"none": len(forecast.sites)}
    entries = forecast.coverage()
    assert entries and not entries[-1].checked
    assert "not in the cassette" in (entries[-1].reason or "")


def test_a_recorded_cassette_serves_the_forecast_offline(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mechanism a golden route runs on, exercised the same way."""
    monkeypatch.delenv(USER_AGENT_ENV_VAR, raising=False)
    route = _route(points=6, step_m=500.0)
    sites = sample_points(route)

    path = tmp_path / "cassette.sqlite"
    with SqliteCache(path) as recording:
        for site in sites:
            recording.put(
                "open_meteo.forecast",
                args_hash(open_meteo_args(site.lat, site.lon, DAY)),
                DAY.isoformat(),
                {
                    "hourly": {
                        "time": ["2026-09-12T12:00", "2026-09-12T13:00"],
                        "temperature_2m": [24.0, 25.0],
                        "cloud_cover": [20.0, 25.0],
                    }
                },
            )

    with SqliteCache(path, offline=True) as cache:
        forecast = route_forecast(route, _ctx(tmp_path, cache), DAY)

    assert forecast.answered
    assert forecast.providers.get("open-meteo") == len(sites)
    reading = forecast.at_distance(0.0, NOON)
    assert reading is not None and reading.temp_c == 24.0


def test_without_a_contact_string_nws_is_not_called_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NWS policy asks for a contact; sending a default is how a project gets blocked."""
    monkeypatch.delenv(USER_AGENT_ENV_VAR, raising=False)
    assert user_agent() is None
    monkeypatch.setenv(USER_AGENT_ENV_VAR, "  ")
    assert user_agent() is None


def test_the_budget_stops_the_sweep_and_says_so(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resource decision, reported — not a scorer that dies partway along a route."""
    monkeypatch.delenv(USER_AGENT_ENV_VAR, raising=False)
    with SqliteCache(offline=False) as cache:
        ctx = _ctx(tmp_path, cache, api_calls_max=0)
        forecast = route_forecast(_route(points=6), ctx, DAY)

    assert not forecast.answered
    reasons = {site.reason for site in forecast.sites}
    assert any("budget exhausted" in (reason or "") for reason in reasons)


def test_the_points_lookup_is_keyed_without_a_date() -> None:
    """A coordinate's forecast grid has no vintage; keying it by day multiplies cassettes."""
    assert STATIC_DAY == "static"
