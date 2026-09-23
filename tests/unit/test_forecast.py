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
    to_utc,
    user_agent,
    window_gap_entry,
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
    """On an out-and-back a site 300 m away across the turnaround is 8 km away in fact.

    `utc_offset_hours=0.0` because this test is about *distance* and wants the clock out of
    the way — and stating it is now the only way to get that, which is the point of the
    field having no default (ADR 0045).
    """
    forecast = RouteForecast(
        sites=[_site_forecast(cum_dist_m=0.0, base_temp=10.0), _site_forecast(cum_dist_m=8000.0)],
        utc_offset_hours=0.0,
    )
    near = forecast.at_distance(500.0, NOON)
    far = forecast.at_distance(7800.0, NOON)
    assert near is not None and far is not None
    assert near.temp_c == 10.0
    assert far.temp_c == 20.0


# --- the two clocks (ADR 0045) ----------------------------------------------


class TestTheTwoClocks:
    """The bug M15 fixed: a naive local ETA read against naive UTC hours.

    Nothing in this suite fed NWS-parsed points into `at()` before — `_site_forecast` above
    labels itself `provider="nws"` but builds its `HourlyPoint`s by hand, so the stamps NWS
    actually produces never reached the comparison. That is the gap these close, and it is
    why the primary provider Scope §7.4 names had never run end to end: all seven golden
    cassettes record Open-Meteo only.
    """

    #: A Pacific NWS office writes its `validTime` with the offset attached. `fromisoformat`
    #: therefore returns a tz-**aware** datetime, where Open-Meteo's bare `2026-09-12T19:00`
    #: returns a naive one. Both name the same instant.
    PACIFIC = "2026-09-12T12:00:00-07:00/PT3H"

    def _nws_hours(self) -> list[HourlyPoint]:
        field = {"uom": "wmoUnit:degC", "values": [{"validTime": self.PACIFIC, "value": 24.0}]}
        return parse_nws_gridpoint({"properties": {"temperature": field}})

    def test_both_providers_produce_the_same_kind_of_stamp(self) -> None:
        """Naive UTC from both, or the comparison downstream is a coin toss."""
        nws = self._nws_hours()
        meteo = parse_open_meteo(
            {"hourly": {"time": ["2026-09-12T19:00"], "temperature_2m": [24.0]}}
        )
        assert nws[0].time.tzinfo is None
        assert meteo[0].time.tzinfo is None
        assert nws[0].time == meteo[0].time == datetime(2026, 9, 12, 19, 0)

    def test_an_nws_hour_fed_to_at_with_a_naive_eta_does_not_raise(self) -> None:
        """This raised `TypeError`, and `run_scorers` reported it as `scorer failed`.

        The regression that matters is not the exception: it is that a caught exception
        three layers from its cause is indistinguishable from a provider being down, so the
        primary path could stay broken for thirteen milestones without anybody seeing it.
        """
        site = SiteForecast(
            site=ForecastSite(index=0, route_index=0, lat=37.8, lon=-122.4, cum_dist_m=0.0),
            provider="nws",
            hours=self._nws_hours(),
        )
        assert site.at(datetime(2026, 9, 12, 19, 30)) is not None

    def test_a_known_local_time_selects_the_hour_a_human_would_name(self) -> None:
        """The round trip, stated in wall clocks rather than in arithmetic.

        Noon in San Francisco on 12 September 2026 is 19:00 UTC. A runner told "24 C at
        noon" must get the row NWS published for 12:00 Pacific — not the row numbered 12:00.
        """
        forecast = RouteForecast(
            sites=[
                SiteForecast(
                    site=ForecastSite(index=0, route_index=0, lat=37.8, lon=-122.4, cum_dist_m=0.0),
                    provider="nws",
                    hours=self._nws_hours(),
                )
            ],
            utc_offset_hours=-7.0,
        )
        at_noon_local = forecast.at_distance(0.0, datetime(2026, 9, 12, 12, 0))
        assert at_noon_local is not None
        assert at_noon_local.temp_c == 24.0

        # And the row numbered 12:00 is the one it used to return: 05:00 Pacific, before
        # the NWS block starts, which is outside the series entirely.
        assert forecast.sites[0].at(datetime(2026, 9, 12, 12, 0)) is None

    def test_an_eta_outside_the_fetched_window_is_absent_rather_than_clamped(self) -> None:
        """`open_meteo_args` asks for one local calendar day of UTC hours (ADR 0045).

        At UTC-7 that window ends at local 17:00, so `bay-urban`'s 17:30 start has no
        forecast at all. Pinned here because it is a **known coverage gap** rather than an
        accident: widening the request changes the cache key, and an air-quality cassette
        for a past date can never be re-fetched.
        """
        hours = parse_open_meteo(
            {
                "hourly": {
                    "time": [f"2026-09-12T{h:02d}:00" for h in range(24)],
                    "temperature_2m": [17.0] * 24,
                }
            }
        )
        forecast = RouteForecast(
            sites=[
                SiteForecast(
                    site=ForecastSite(index=0, route_index=0, lat=37.8, lon=-122.4, cum_dist_m=0.0),
                    provider="open-meteo",
                    hours=hours,
                )
            ],
            utc_offset_hours=-7.0,
        )
        assert forecast.at_distance(0.0, datetime(2026, 9, 12, 16, 0)) is not None
        assert forecast.at_distance(0.0, datetime(2026, 9, 12, 17, 30)) is None

        entry = window_gap_entry(forecast, [None])
        assert entry is not None and not entry.checked
        assert "local 2026-09-11 17:00 to 16:00" in (entry.reason or "")

    def test_no_resolved_offset_reads_as_absent_rather_than_as_utc(self) -> None:
        """A defaulted zero would look well-formed and be seven hours wrong.

        Absence is not zero (scope §12). `route_forecast` always fills the field, because
        `utc_offset_for` always answers and says how (ADR 0008); this is what happens when
        something else does not.
        """
        forecast = RouteForecast(sites=[_site_forecast()], utc_offset_hours=None)
        assert forecast.at_distance(0.0, NOON) is None

    def test_the_offset_is_subtracted_not_added(self) -> None:
        """The sign, pinned on its own, because getting it backwards is a 14-hour error."""
        assert to_utc(NOON, -7.0) == datetime(2026, 9, 12, 19, 0)
        assert to_utc(NOON, 1.0) == datetime(2026, 9, 12, 11, 0)


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


def _record(path: Any, route: Route, times: list[str], temps: list[float]) -> int:
    """A cassette of Open-Meteo hours for every site on a route. Times are UTC, as fetched."""
    sites = sample_points(route)
    with SqliteCache(path) as recording:
        for site in sites:
            recording.put(
                "open_meteo.forecast",
                args_hash(open_meteo_args(site.lat, site.lon, DAY)),
                DAY.isoformat(),
                {
                    "hourly": {
                        "time": times,
                        "temperature_2m": temps,
                        "cloud_cover": [20.0 + 5.0 * i for i in range(len(times))],
                    }
                },
            )
    return len(sites)


def test_a_recorded_cassette_serves_the_forecast_offline(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mechanism a golden route runs on, exercised the same way.

    The ETA is **local**, the cassette is **UTC**, and the route is in California, so the
    row this selects is seven hours along from the one whose wall-clock number matches. It
    read 12:00 UTC for a 12:00 local ETA until M15 (ADR 0045).
    """
    monkeypatch.delenv(USER_AGENT_ENV_VAR, raising=False)
    route = _route(points=6, step_m=500.0)
    path = tmp_path / "cassette.sqlite"
    count = _record(
        path,
        route,
        ["2026-09-12T18:00", "2026-09-12T19:00", "2026-09-12T20:00"],
        [24.0, 25.0, 26.0],
    )

    with SqliteCache(path, offline=True) as cache:
        forecast = route_forecast(route, _ctx(tmp_path, cache), DAY)

    assert forecast.answered
    assert forecast.providers.get("open-meteo") == count
    assert forecast.utc_offset_hours == -7.0
    assert forecast.utc_offset_source == "zone America/Los_Angeles"

    # 12:00 in San Francisco on 12 September is 19:00 UTC, which is the middle row.
    reading = forecast.at_distance(0.0, NOON)
    assert reading is not None and reading.temp_c == 25.0


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


class TestTheArchiveFallback:
    """The archive endpoint was unreachable for eight milestones, and nothing said so.

    `open_meteo_root(day, today)` is only useful if `today` is the real date. Every call site
    passed `ctx.clock.now().date()`, and that clock is `FrozenClock(start_at)` — pinned to the
    plan's *own* date so plans are reproducible. Reference and day were therefore always equal,
    `(reference - day).days` was always 0, and `ARCHIVE_AFTER_DAYS` never once fired.

    It went unnoticed because `phoenix-heat` pins a date 57 days back, and Open-Meteo's
    *forecast* endpoint serves about 92 days of history itself — so the wrong endpoint returned
    the right answer. `boston-winter` is 244 days back, got `400 Bad Request` at every site, and
    would have pinned a golden with no forecast in it at all.

    The fix asks rather than calculates, so no wall clock enters `core/` (scope 3.3).
    """

    QUERY = {"latitude": 42.37, "longitude": -71.13, "start_date": "2026-01-15"}

    def test_the_forecast_endpoint_is_tried_first(self, monkeypatch: Any) -> None:
        from longrun.core.data import forecast as module

        asked: list[str] = []
        monkeypatch.setattr(
            module, "_get_json", lambda root, *_a, **_k: asked.append(root) or {"ok": 1}
        )
        module._open_meteo_fetch(module.OPEN_METEO_ROOT, self.QUERY)
        assert asked == [module.OPEN_METEO_ROOT]

    def test_a_refusal_falls_back_to_the_archive(self, monkeypatch: Any) -> None:
        from longrun.core.data import forecast as module

        asked: list[str] = []

        def fake(root: str, *_a: Any, **_k: Any) -> Any:
            asked.append(root)
            if root == module.OPEN_METEO_ROOT:
                raise RuntimeError("400 Bad Request")
            return {"hourly": {}}

        monkeypatch.setattr(module, "_get_json", fake)
        module._open_meteo_fetch(module.OPEN_METEO_ROOT, self.QUERY)
        assert asked == [module.OPEN_METEO_ROOT, module.OPEN_METEO_ARCHIVE_ROOT]

    def test_the_archive_refusing_is_raised_rather_than_looped(self, monkeypatch: Any) -> None:
        """A day neither endpoint covers is a reason on the coverage line, not a retry storm."""
        from longrun.core.data import forecast as module

        calls: list[str] = []

        def always_fails(root: str, *_a: Any, **_k: Any) -> Any:
            calls.append(root)
            raise RuntimeError("nope")

        monkeypatch.setattr(module, "_get_json", always_fails)
        with pytest.raises(RuntimeError):
            module._open_meteo_fetch(module.OPEN_METEO_ARCHIVE_ROOT, self.QUERY)
        assert calls == [module.OPEN_METEO_ARCHIVE_ROOT]
