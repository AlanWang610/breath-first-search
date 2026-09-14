"""Our parsers still match what the providers actually send (scope 7.4, 11).

`tests/unit/test_forecast.py` pins the parsers against literal payloads, which proves they
are self-consistent and proves nothing about whether those payloads still resemble the
live API. This is the half that can only be answered by asking.

It is also how a cassette gets recorded: the run writes into a real `SqliteCache`, and
that file is the same thing a golden route reads back in offline mode. There is no second
recording system — the production cache *is* the cassette store.

`network`-marked, never in CI.

    uv run pytest tests/contract/test_forecast_providers.py -m network

NWS is skipped unless `LONGRUN_NWS_USER_AGENT` is set. Its policy requires a real contact
string, and this suite will not invent one or borrow anybody's.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from longrun.core.data.cache import SqliteCache
from longrun.core.data.file_store import FileLayerStore, FileRasterStore
from longrun.core.data.forecast import (
    OPEN_METEO_FIELDS,
    USER_AGENT_ENV_VAR,
    parse_nws_gridpoint,
    parse_open_meteo,
    route_forecast,
)
from longrun.core.models.context import Budget, FrozenClock, ScorerContext
from longrun.core.models.coverage import CoverageManifest
from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.models.profile import PreferenceProfile

pytestmark = pytest.mark.network

#: The Embarcadero, which is where every fixture in this repo lives.
LAT, LON = 37.7955, -122.3937


def _tomorrow() -> date:
    """A live forecast needs a future date; a fixed past one would have no data.

    Relative on purpose, and only legitimate because this is a contract test rather than a
    golden — a golden's date is frozen forever precisely because a past forecast can never
    be re-fetched.
    """
    return date.today() + timedelta(days=1)


def _route() -> Route:
    return Route(
        id="contract",
        points=[RoutePoint(lat=LAT, lon=LON + i * 0.06, cum_dist_m=i * 5300.0) for i in range(3)],
    )


def _ctx(cache: SqliteCache, tmp_path: Path) -> ScorerContext:
    from datetime import datetime

    return ScorerContext(
        layers=FileLayerStore(tmp_path),
        rasters=FileRasterStore(tmp_path),
        cache=cache,
        clock=FrozenClock(datetime(2026, 1, 1, 12, 0)),
        coverage=CoverageManifest(),
        profile=PreferenceProfile(),
        budget=Budget(),
    )


def _get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
    import httpx

    try:
        response = httpx.get(url, params=params, headers=headers, timeout=20.0)
    except httpx.HTTPError as exc:  # pragma: no cover - transient
        pytest.skip(f"{url} unreachable: {exc}")
    if response.status_code >= 500:  # pragma: no cover - transient
        pytest.skip(f"{url} returned {response.status_code}")
    response.raise_for_status()
    return response.json()


# --- Open-Meteo (no key required) -------------------------------------------


def test_open_meteo_still_returns_the_fields_we_parse() -> None:
    """Every field named in `OPEN_METEO_FIELDS`, because a silent rename reads as None.

    A dropped column does not error — `parse_open_meteo` returns None for it and the
    scorer reports lower confidence. That is the right behaviour and exactly why the
    failure would otherwise go unnoticed for a release or two.
    """
    payload = _get(
        "https://api.open-meteo.com/v1/forecast",
        {
            "latitude": LAT,
            "longitude": LON,
            "hourly": ",".join(OPEN_METEO_FIELDS),
            "start_date": _tomorrow().isoformat(),
            "end_date": _tomorrow().isoformat(),
            "timezone": "UTC",
        },
        {"Accept": "application/json"},
    )

    hourly = payload.get("hourly") or {}
    for field in OPEN_METEO_FIELDS:
        assert field in hourly, f"Open-Meteo no longer returns {field}"

    hours = parse_open_meteo(payload)
    assert len(hours) >= 24, "a full day of hours"
    assert any(h.temp_c is not None for h in hours)
    assert any(h.cloud_cover_pct is not None for h in hours)


def test_a_recorded_open_meteo_run_replays_offline(tmp_path: Path) -> None:
    """Record once against the live API, then serve the same route with no network.

    This is the whole cassette mechanism in one test. If it holds, a golden route can be
    driven by a committed `cache.sqlite` and the suite stays hermetic.
    """
    cassette = tmp_path / "cassette.sqlite"
    route, day = _route(), _tomorrow()

    with SqliteCache(cassette) as recording:
        live = route_forecast(route, _ctx(recording, tmp_path), day)
    assert live.answered, f"nothing recorded: {[s.reason for s in live.sites]}"

    with SqliteCache(cassette, offline=True) as replay:
        offline = route_forecast(route, _ctx(replay, tmp_path), day)

    assert offline.answered
    assert offline.providers == live.providers
    first_live = live.sites[0].hours[0]
    first_offline = offline.sites[0].hours[0]
    assert first_offline.time == first_live.time
    assert first_offline.temp_c == first_live.temp_c


# --- NWS (needs a contact string) -------------------------------------------


def test_the_nws_gridpoint_payload_still_carries_sky_cover() -> None:
    """The reason we read the raw gridpoint rather than `/forecast/hourly`.

    `sun_exposure` scales clear-sky irradiance by cloud cover, and the hourly endpoint
    does not publish it. If NWS ever moves `skyCover`, the shade figures quietly stop
    being cloud-scaled — so this asserts the field by name.
    """
    agent = os.environ.get(USER_AGENT_ENV_VAR, "").strip()
    if not agent:
        pytest.skip(f"{USER_AGENT_ENV_VAR} is unset; NWS requires a real contact string")

    headers = {"User-Agent": agent, "Accept": "application/geo+json"}
    points = _get(f"https://api.weather.gov/points/{LAT},{LON}", {}, headers)
    properties = points["properties"]
    office, grid_x, grid_y = (
        properties["gridId"],
        properties["gridX"],
        properties["gridY"],
    )

    payload = _get(f"https://api.weather.gov/gridpoints/{office}/{grid_x},{grid_y}", {}, headers)
    assert "skyCover" in payload["properties"], "NWS moved skyCover"

    hours = parse_nws_gridpoint(payload)
    assert hours, "the gridpoint expanded to no hours"
    assert any(h.cloud_cover_pct is not None for h in hours)
    assert any(h.temp_c is not None for h in hours)
    # Intervals are the awkward part: a three-hour block must become three hours.
    assert len({h.time for h in hours}) == len(hours)
