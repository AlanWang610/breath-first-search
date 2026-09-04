"""Cache and store tests (scope 3.6, 4.4, 6.4).

The offline-cache tests matter most: offline mode is what makes the golden suite
hermetic, and it is also the cassette mechanism, so if a miss ever silently returned
None instead of raising, tests would quietly start reaching the network.
"""

from __future__ import annotations

import os
import subprocess
import sys
import warnings
from datetime import date
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point

from longrun.core.data.base import Cache, LayerStore, RasterStore
from longrun.core.data.cache import CacheMiss, SqliteCache, args_hash
from longrun.core.data.file_store import FileLayerStore, FileRasterStore, LayerNotFound
from longrun.core.geo.segments import corridor
from longrun.core.models.geometry import Route, RoutePoint

SF_LINE = LineString([(-122.40, 37.77), (-122.40, 37.78)])
KANSAS_LINE = LineString([(-100.0, 40.0), (-100.0, 40.1)])


@pytest.fixture
def sf_route() -> Route:
    return Route(
        id="r",
        points=[
            RoutePoint(lat=37.77, lon=-122.40, cum_dist_m=0.0),
            RoutePoint(lat=37.78, lon=-122.40, cum_dist_m=1112.0),
        ],
    )


@pytest.fixture
def fixture_dir(tmp_path: Path) -> Path:
    """A miniature corridor extract: one relevant feature, one far away."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        gpd.GeoDataFrame(
            {"way_id": [1, 2], "highway": ["residential", "motorway"]},
            geometry=[SF_LINE, KANSAS_LINE],
            crs="EPSG:4326",
        ).to_file(tmp_path / "ways.gpkg", driver="GPKG")
        gpd.GeoDataFrame(
            {"kind": ["water", "toilet"]},
            geometry=[Point(-122.401, 37.775), Point(-100.0, 40.05)],
            crs="EPSG:4326",
        ).to_file(tmp_path / "amenities.gpkg", driver="GPKG")
    return tmp_path


# --- cache keys -------------------------------------------------------------


def test_args_hash_is_stable_across_key_order() -> None:
    assert args_hash({"a": 1, "b": 2}) == args_hash({"b": 2, "a": 1})


def test_args_hash_distinguishes_different_args() -> None:
    assert args_hash({"lat": 37.7}) != args_hash({"lat": 37.8})


def test_args_hash_is_stable_across_processes() -> None:
    """`hash()` is salted per process; using it would make cassettes unreproducible.

    Run in subprocesses under two different PYTHONHASHSEEDs, which is the condition that
    would actually expose a salted hash. Asserting a hardcoded digest here would only
    restate the implementation.
    """
    code = "from longrun.core.data.cache import args_hash; print(args_hash({'lat': 37.7}))"
    digests = set()
    for seed in ("0", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
        )
        digests.add(out.stdout.strip())
    assert len(digests) == 1
    assert digests.pop() == args_hash({"lat": 37.7})


# --- cache behaviour --------------------------------------------------------


def test_cache_round_trips() -> None:
    with SqliteCache() as cache:
        cache.put("microclimate", "abc", "2026-03-15", {"temp_c": 14})
        assert cache.get("microclimate", "abc", "2026-03-15") == {"temp_c": 14}


def test_cache_satisfies_the_protocol() -> None:
    assert isinstance(SqliteCache(), Cache)


def test_cache_is_keyed_by_date() -> None:
    """A forecast is only meaningful for the day it describes (scope 4.4)."""
    with SqliteCache() as cache:
        cache.put("microclimate", "abc", "2026-03-15", {"temp_c": 14})
        assert cache.get("microclimate", "abc", "2026-03-16") is None


def test_online_miss_returns_none() -> None:
    with SqliteCache() as cache:
        assert cache.get("microclimate", "nope", "2026-03-15") is None


def test_offline_miss_raises_and_names_the_key() -> None:
    """A silent None would produce a plan with an unreported gap (scope 3.6)."""
    with SqliteCache(offline=True) as cache, pytest.raises(CacheMiss) as excinfo:
        cache.get("air_quality", "deadbeef", "2026-03-15")
    assert excinfo.value.tool == "air_quality"
    assert "2026-03-15" in str(excinfo.value)


def test_offline_hit_is_served_normally() -> None:
    """A pre-populated offline cache is exactly what a recorded cassette is."""
    path = ":memory:"
    with SqliteCache(path) as recorder:
        recorder.put("microclimate", "abc", "2026-03-15", {"temp_c": 14})
        recorder._offline = True
        assert recorder.get("microclimate", "abc", "2026-03-15") == {"temp_c": 14}


def test_offline_cache_never_writes() -> None:
    """A cassette must not mutate when a test runs against it."""
    with SqliteCache(offline=True) as cache:
        cache.put("microclimate", "abc", "2026-03-15", {"temp_c": 14})
        assert cache.keys() == []


def test_fetch_calls_the_producer_once_then_serves_from_cache() -> None:
    calls = []

    def producer() -> dict[str, int]:
        calls.append(1)
        return {"temp_c": 14}

    with SqliteCache() as cache:
        args = {"lat": 37.7, "lon": -122.4}
        first = cache.fetch("microclimate", args, date(2026, 3, 15), producer)
        second = cache.fetch("microclimate", args, date(2026, 3, 15), producer)
    assert first == second == {"temp_c": 14}
    assert len(calls) == 1


def test_fetch_offline_raises_rather_than_calling_the_producer() -> None:
    """The guarantee that makes CI network-free: the producer is never reached."""

    def producer() -> dict[str, int]:
        raise AssertionError("producer must not be called in offline mode")

    with SqliteCache(offline=True) as cache, pytest.raises(CacheMiss):
        cache.fetch("microclimate", {"lat": 37.7}, date(2026, 3, 15), producer)


def test_cache_persists_to_disk(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "cache.sqlite"
    with SqliteCache(path) as cache:
        cache.put("closures", "abc", "2026-03-15", [])
    with SqliteCache(path, offline=True) as reopened:
        assert reopened.get("closures", "abc", "2026-03-15") == []
        assert reopened.keys() == [("closures", "abc", "2026-03-15")]


# --- layer store ------------------------------------------------------------


def test_file_layer_store_satisfies_the_protocol(fixture_dir: Path) -> None:
    assert isinstance(FileLayerStore(fixture_dir), LayerStore)


def test_corridor_query_excludes_distant_features(fixture_dir: Path, sf_route: Route) -> None:
    store = FileLayerStore(fixture_dir)
    ways = store.ways_in_corridor(corridor(sf_route))
    assert len(ways) == 1
    assert ways.iloc[0]["way_id"] == 1


def test_amenity_kinds_are_filtered(fixture_dir: Path, sf_route: Route) -> None:
    store = FileLayerStore(fixture_dir)
    c = corridor(sf_route)
    assert len(store.points_in_corridor(c, ["water"])) == 1
    assert len(store.points_in_corridor(c, ["toilet"])) == 0


def test_missing_layer_is_distinct_from_an_empty_result(fixture_dir: Path, sf_route: Route) -> None:
    """ "No parks here" and "nobody extracted parks" are different claims (scope 3.6)."""
    store = FileLayerStore(fixture_dir)
    assert store.has_layer("ways")
    assert not store.has_layer("padus")
    with pytest.raises(LayerNotFound, match="padus"):
        store.polygons_intersecting(corridor(sf_route), "padus")


def test_lines_crossing_uses_the_route_not_the_corridor(fixture_dir: Path, sf_route: Route) -> None:
    store = FileLayerStore(fixture_dir)
    assert len(store.lines_crossing(sf_route, "ways")) == 1


def test_vintage_is_recorded_for_the_manifest(fixture_dir: Path) -> None:
    store = FileLayerStore(fixture_dir)
    assert store.vintage("ways") is None
    store.set_vintage("ways", "2026-09-04")
    assert store.vintage("ways") == "2026-09-04"


# --- raster store -----------------------------------------------------------


def test_file_raster_store_satisfies_the_protocol(tmp_path: Path) -> None:
    assert isinstance(FileRasterStore(tmp_path), RasterStore)


def test_absent_raster_reads_as_no_coverage(tmp_path: Path, sf_route: Route) -> None:
    """No DEM is unknown elevation, not zero elevation (scope 12)."""
    store = FileRasterStore(tmp_path)
    assert store.read_window("dem", corridor(sf_route).bbox) is None
    assert store.resolution_m("dem") is None
