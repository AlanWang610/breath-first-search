"""The raster tile provider (ADR 0023): what was measured, and what must never be cached.

No network. `httpx.get` is replaced with a fake that records what it was asked, so the
tests can see whether a request went out at all - which is the question most of these ask.

Two groups carry the file. **The measured facts** - the `{z}/{y}/{x}` order and the zoom-16
ceiling - each contradicted what reading would have concluded, so they are pinned against
the tiles that were actually fetched on 2026-09-14. **The cache rules** are ADR 0017's,
applied to tiles: a replay costs nothing, and an outage never becomes a cached "no imagery
here".
"""

from __future__ import annotations

from typing import Any

import pytest

from longrun.core.data.cache import CacheMiss, SqliteCache
from longrun.core.data.tiles import (
    PROVIDER_ENV_VAR,
    USGS_IMAGERY,
    USGS_TOPO,
    TileConfigError,
    fetch_tile,
    provider_from_env,
    tile_args,
    tile_for,
)
from longrun.core.models.context import Budget, BudgetExceeded

JPEG = b"\xff\xd8\xff\xe0 a tile"


class _Response:
    def __init__(self, status: int, content: bytes = b"", media_type: str = "image/jpeg") -> None:
        self.status_code = status
        self.content = content
        self.headers = {"content-type": media_type}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Served(list[str]):
    """Every URL requested, and what the fake server will answer next."""

    def __init__(self) -> None:
        super().__init__()
        self.response = _Response(200, JPEG)


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> _Served:
    """Answers with a JPEG unless a test sets `served.response`."""
    import httpx

    urls = _Served()

    def fake_get(url: str, **kwargs: Any) -> _Response:
        urls.append(url)
        return urls.response

    monkeypatch.setattr(httpx, "get", fake_get)
    return urls


# --- what was measured ---------------------------------------------------------------


def test_the_tile_maths_matches_tiles_fetched_from_the_live_service() -> None:
    """Both of these were requested on 2026-09-14 and came back as real imagery."""
    assert tile_for(37.78, -122.42, 12) == (655, 1583)
    assert tile_for(37.7715, -122.4686, 16) == (10473, 25331)


def test_usgs_paths_are_row_before_column() -> None:
    """ArcGIS's `{z}/{y}/{x}`, measured rather than assumed: `12/1583/655` is San Francisco
    and the swapped `12/655/1583` is a 404. Getting it backwards draws a blank map with no
    error, because every tile is a 404 the browser shrugs at."""
    assert USGS_IMAGERY.url(12, 655, 1583).endswith("/tile/12/1583/655")
    assert USGS_TOPO.url(12, 655, 1583).endswith("/tile/12/1583/655")


def test_usgs_maxzoom_is_the_measured_sixteen_and_not_the_advertised_twenty_three() -> None:
    """The service metadata lists 23 levels; every tile past 16 is a 404 in San Francisco.
    A maxzoom read from the metadata would 404 on every request a zoomed-in map makes."""
    assert USGS_IMAGERY.maxzoom == 16
    assert USGS_TOPO.maxzoom == 16


def test_a_point_past_the_mercator_limit_is_clamped_rather_than_undefined() -> None:
    x, y = tile_for(89.9, 0.0, 4)
    assert (x, y) == (8, 0)
    assert tile_for(-89.9, 179.9999, 4) == (15, 15)


# --- choosing the provider -----------------------------------------------------------


def test_the_default_is_usgs_imagery() -> None:
    """Unset means the public-domain, keyless provider - nothing to configure and no key to
    leak into a browser bundle."""
    assert provider_from_env({}) is USGS_IMAGERY


@pytest.mark.parametrize(
    ("value", "expected"), [("usgs-topo", USGS_TOPO), ("USGS-Imagery", USGS_IMAGERY)]
)
def test_a_named_provider_is_selected(value: str, expected: Any) -> None:
    assert provider_from_env({PROVIDER_ENV_VAR: value}) is expected


def test_none_switches_tiles_off() -> None:
    assert provider_from_env({PROVIDER_ENV_VAR: "none"}) is None


def test_an_unknown_provider_names_the_variable_and_the_choices() -> None:
    with pytest.raises(TileConfigError, match=f"{PROVIDER_ENV_VAR}='mapbox'.*usgs-topo"):
        provider_from_env({PROVIDER_ENV_VAR: "mapbox"})


@pytest.mark.parametrize(
    ("env", "fault"),
    [
        ({}, "LONGRUN_TILE_URL"),
        ({"LONGRUN_TILE_URL": "https://t.example/{z}/{x}/{y}.png"}, "LONGRUN_TILE_ATTRIBUTION"),
        (
            {
                "LONGRUN_TILE_URL": "http://t.example/{z}/{x}/{y}.png",
                "LONGRUN_TILE_ATTRIBUTION": "x",
            },
            "https",
        ),
        (
            {"LONGRUN_TILE_URL": "https://t.example/{z}/{x}.png", "LONGRUN_TILE_ATTRIBUTION": "x"},
            "missing {y}",
        ),
    ],
)
def test_a_custom_provider_refuses_to_start_without_what_it_owes(
    env: dict[str, str], fault: str
) -> None:
    """Attribution is required, not defaulted: scope 14 makes it an obligation, and a
    provider nobody can attribute is one nobody may use."""
    with pytest.raises(TileConfigError, match=fault):
        provider_from_env({PROVIDER_ENV_VAR: "custom", **env})


def test_a_custom_providers_key_never_reaches_the_cache_key() -> None:
    """A key in a query string is the common shape, and a key in a cache key is a secret in
    a committed fixture."""
    provider = provider_from_env(
        {
            PROVIDER_ENV_VAR: "custom",
            "LONGRUN_TILE_URL": "https://tiles.example/{z}/{x}/{y}.png?key=SECRET123",
            "LONGRUN_TILE_ATTRIBUTION": "Example tiles",
        }
    )
    assert provider is not None

    assert "SECRET123" not in str(tile_args(provider, 3, 1, 2))


# --- the cache rules -----------------------------------------------------------------


def test_a_fetched_tile_is_the_bytes_the_server_sent(served: _Served) -> None:
    with SqliteCache() as cache:
        tile = fetch_tile(USGS_IMAGERY, 37.7715, -122.4686, 16, cache=cache, budget=Budget())

    assert tile.found
    assert tile.data == JPEG
    assert tile.media_type == "image/jpeg"
    assert served == [USGS_IMAGERY.url(16, 10473, 25331)]


def test_a_replay_from_the_cache_costs_no_imagery_budget(served: _Served) -> None:
    """Spent inside the producer, as ADR 0017 does for the router - so the ~10 per plan cap
    counts tiles fetched, not tiles looked at twice."""
    budget = Budget()
    with SqliteCache() as cache:
        fetch_tile(USGS_IMAGERY, 37.77, -122.47, 16, cache=cache, budget=budget)
        fetch_tile(USGS_IMAGERY, 37.77, -122.47, 16, cache=cache, budget=budget)

    assert budget.imagery_tiles_used == 1
    assert len(served) == 1


def test_zoom_is_clamped_before_the_request_goes_out(served: _Served) -> None:
    """The reason clamping cannot happen after the fact. Past 16 USGS answers 404, the same
    answer it gives for a point outside the US - so a request at 18 that went out would be
    cached as "no imagery here" for a place that has imagery."""
    with SqliteCache() as cache:
        tile = fetch_tile(USGS_IMAGERY, 37.7715, -122.4686, 18, cache=cache, budget=Budget())

    assert (tile.z, tile.requested_zoom) == (16, 18)
    assert served == [USGS_IMAGERY.url(16, 10473, 25331)]


def test_outside_coverage_is_an_answer_and_is_remembered(served: _Served) -> None:
    """Inside the zoom range a USGS 404 is deterministic - the point is not in the US - so it
    is cached like any other answer and not asked again."""
    served.response = _Response(404, b"<html>", "text/html")
    with SqliteCache() as cache:
        first = fetch_tile(USGS_IMAGERY, 30.0, -150.0, 12, cache=cache, budget=Budget())
        second = fetch_tile(USGS_IMAGERY, 30.0, -150.0, 12, cache=cache, budget=Budget())

    assert not first.found and not second.found
    assert "no tile at this location" in (first.reason or "")
    assert len(served) == 1


@pytest.mark.parametrize(
    "response",
    [_Response(503, b"down", "text/html"), _Response(200, b"<html>error</html>", "text/html")],
    ids=["server-error", "html-served-as-200"],
)
def test_an_outage_is_never_cached_as_an_absence(served: _Served, response: _Response) -> None:
    """ADR 0017's second rule. One bad minute during a recording session must not pin a
    false "no imagery here" into a cassette for good - so the next request asks again."""
    served.response = response
    with SqliteCache() as cache:
        with pytest.raises((RuntimeError, ValueError)):
            fetch_tile(USGS_IMAGERY, 37.77, -122.47, 16, cache=cache, budget=Budget())
        served.response = _Response(200, JPEG)
        tile = fetch_tile(USGS_IMAGERY, 37.77, -122.47, 16, cache=cache, budget=Budget())

    assert tile.found
    assert len(served) == 2


def test_offline_a_tile_nobody_recorded_is_a_loud_miss(served: _Served) -> None:
    with SqliteCache(offline=True) as cache:
        with pytest.raises(CacheMiss):
            fetch_tile(USGS_IMAGERY, 37.77, -122.47, 16, cache=cache, budget=Budget())

    assert served == []


def test_the_eleventh_tile_in_a_plan_is_refused_by_the_budget(served: _Served) -> None:
    """Scope 7.9's "hard cap ~10 calls per plan" is `Budget.imagery_tiles_max`, and until
    this module it metered a cost nothing incurred."""
    budget = Budget()
    with SqliteCache() as cache:
        for column in range(budget.imagery_tiles_max):
            fetch_tile(USGS_IMAGERY, 37.77, -122.47 + column * 0.01, 16, cache=cache, budget=budget)
        with pytest.raises(BudgetExceeded):
            fetch_tile(USGS_IMAGERY, 37.77, -121.0, 16, cache=cache, budget=budget)


def test_a_recorded_tile_replays_byte_for_byte_from_disk_offline(
    served: _Served, tmp_path: Any
) -> None:
    """`SqliteCache.put` stores JSON and a tile is binary. Recorded by one cache, replayed by
    a *new* offline one on the same file with nothing served - so the bytes can only have
    come back through storage, not from memory. A tile that returned as a `str` would be
    handed to a vision model as text."""
    path = tmp_path / "cassette.sqlite"
    with SqliteCache(path) as recording:
        fetch_tile(USGS_IMAGERY, 37.7715, -122.4686, 16, cache=recording, budget=Budget())

    with SqliteCache(path, offline=True) as replay:
        tile = fetch_tile(USGS_IMAGERY, 37.7715, -122.4686, 16, cache=replay, budget=Budget())

    assert tile.data == JPEG
    assert isinstance(tile.data, bytes)
    assert len(served) == 1
