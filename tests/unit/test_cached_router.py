"""The router behind the cache (scope 4.4, 6.4; M5.4).

No server. A fake engine records what it was asked and returns paths, so what is tested is
the wrapper's four promises: a hit costs nothing, a miss is charged, a failure is never
recorded, and a missing recording is loud.

Proven end to end against a live GraphHopper before these were written - recorded three
keys at a cost of three API calls, then replayed all three against a router pointed at a
closed port for **0 API calls** and identical geometry. These tests are what keep that
true without a server.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from longrun.core.data.cache import CacheMiss, SqliteCache
from longrun.core.models.context import Budget
from longrun.core.models.geometry import LatLon, Route, RoutePoint
from longrun.core.routing.base import NoRouteError, RouterUnavailable
from longrun.core.routing.cached import ROUTE_TOOL, CachedRouter

FERRY = LatLon(lat=37.7955, lon=-122.3937)
DEYOUNG = LatLon(lat=37.7715, lon=-122.4686)


def _path(*coords: tuple[float, float]) -> dict[str, Any]:
    return {"points": {"coordinates": [[lon, lat] for lon, lat in coords]}, "distance": 100.0}


class FakeEngine:
    """An engine that counts what it was asked, and can be told to fail."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls = 0
        self.matches = 0
        self.fail = fail

    def paths(self, body: dict[str, Any], waypoints: Any) -> list[dict[str, Any]]:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return [_path((-122.3937, 37.7955), (-122.4686, 37.7715))]

    def map_match(self, track: Route) -> tuple[Route, list[int | None]]:
        self.matches += 1
        return track, [7] * len(track.points)


def _track() -> Route:
    return Route(
        id="track",
        points=[
            RoutePoint(lat=37.7955 - i * 0.001, lon=-122.3937 - i * 0.001, cum_dist_m=i * 140.0)
            for i in range(4)
        ],
    )


@pytest.fixture
def cache(tmp_path: Path) -> Any:
    with SqliteCache(tmp_path / "routes.sqlite", offline=False) as store:
        yield store


# --- the four promises -------------------------------------------------------


def test_a_second_identical_request_costs_nothing(cache: Any) -> None:
    """Spent inside the producer, so a replay is free - `forecast.py`'s rule, and the
    reason scope 6.4's cap can survive five rounds of rerouting."""
    budget = Budget()
    router = CachedRouter(FakeEngine(), cache, budget, graph="g1")

    router.route([FERRY, DEYOUNG])
    router.route([FERRY, DEYOUNG])

    assert router.inner.calls == 1  # type: ignore[attr-defined]
    assert budget.api_calls_used == 1


def test_every_router_call_is_charged_to_the_plan(cache: Any) -> None:
    """`grep Budget core/routing/` returned nothing before M5.4: the calls scope 8.1
    step 6 makes most of were the ones the 200-call cap did not count."""
    budget = Budget()
    router = CachedRouter(FakeEngine(), cache, budget, graph="g1")

    router.route([FERRY, DEYOUNG])
    router.map_match(_track())

    assert budget.api_calls_used == 2


def test_a_router_outage_is_never_recorded(cache: Any) -> None:
    """`alternatives` turns an outage into the same empty list an undetourable span
    produces. Recording that would pin "no alternatives here" into a cassette for good -
    and a cassette is permanent by design."""
    router = CachedRouter(FakeEngine(fail=RouterUnavailable("down")), cache, Budget(), graph="g1")

    assert router.alternatives(_track(), None, k=2) == []
    assert cache.keys() == []


def test_an_unrecorded_request_is_loud_rather_than_silent(cache: Any, tmp_path: Path) -> None:
    """The one guarantee the whole cassette design rests on."""
    router = CachedRouter(FakeEngine(), cache, Budget(), graph="g1")
    router.route([FERRY, DEYOUNG])

    with SqliteCache(tmp_path / "routes.sqlite", offline=True) as replay:
        offline = CachedRouter(FakeEngine(), replay, Budget(), graph="g1")
        offline.route([FERRY, DEYOUNG])  # recorded, so it answers

        with pytest.raises(CacheMiss):
            offline.route([FERRY, LatLon(lat=37.80, lon=-122.41)])


# --- the key -----------------------------------------------------------------


def test_two_graphs_do_not_share_an_answer(cache: Any) -> None:
    """A route is a function of the graph it was drawn on, and scope 13 rebuilds that.

    This is the whole reason the key is not `(tool, args, date)` like every other one: a
    route does not change with the day, it changes with the graph, so the graph goes in
    the args and the day is `STATIC_DAY`.
    """
    engine = FakeEngine()
    CachedRouter(engine, cache, Budget(), graph="2026-09-11").route([FERRY, DEYOUNG])
    CachedRouter(engine, cache, Budget(), graph="2026-10-02").route([FERRY, DEYOUNG])

    assert engine.calls == 2
    assert len(cache.keys()) == 2


def test_a_coordinate_recomputed_still_finds_its_recording(cache: Any) -> None:
    """`args_hash` does no rounding of its own, so every key builder applies
    `COORD_PRECISION` - otherwise a point that has been through arithmetic misses a
    cassette it is identical to at any resolution anyone cares about."""
    engine = FakeEngine()
    router = CachedRouter(engine, cache, Budget(), graph="g1")

    router.route([FERRY, DEYOUNG])
    router.route([LatLon(lat=37.79550000001, lon=-122.39370000002), DEYOUNG])

    assert engine.calls == 1


def test_what_is_stored_is_the_servers_answer_and_not_a_route(cache: Any) -> None:
    """`SqliteCache.put` stores `json.dumps(value, default=str)`, so a pydantic model
    would be stored as its *repr* and come back a `str` - which a scorer's blanket handler
    reports as "scorer failed" three layers away from the cause."""
    router = CachedRouter(FakeEngine(), cache, Budget(), graph="g1")
    router.route([FERRY, DEYOUNG])

    tool, key, day = cache.keys()[0]
    stored = cache.get(tool, key, day)

    assert tool == ROUTE_TOOL
    assert day == "static"
    assert isinstance(stored, dict)
    assert isinstance(stored["paths"][0]["points"]["coordinates"], list)


# --- the manifest ------------------------------------------------------------


def test_the_cache_logs_the_hash_and_whether_it_was_a_hit(cache: Any) -> None:
    """`ToolCall.args_hash` and `.cached` were declared in M1 and written by nothing.

    The cache is the one door every external call goes through and it is opened once per
    plan, so it is the only place that can answer "did this plan touch the network".
    """
    router = CachedRouter(FakeEngine(), cache, Budget(), graph="g1")

    router.route([FERRY, DEYOUNG])
    router.route([FERRY, DEYOUNG])

    assert [call.cached for call in cache.calls] == [False, True]
    assert all(call.tool == ROUTE_TOOL for call in cache.calls)
    assert len({call.args_hash for call in cache.calls}) == 1


def test_a_call_that_raised_is_not_logged_as_a_call(cache: Any) -> None:
    """It did not happen, and why it did not is already reported through coverage."""
    router = CachedRouter(FakeEngine(fail=NoRouteError(FERRY, DEYOUNG)), cache, Budget(), graph="g")

    with pytest.raises(NoRouteError):
        router.route([FERRY, DEYOUNG])

    assert cache.calls == []
