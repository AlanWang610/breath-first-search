"""The router, against a running GraphHopper (scope 4.3, 6.1; ADR 0001, risks R1 and R4).

`network`-marked, and skipped when nothing is listening. What it holds is the three claims
M0's spikes made and nothing since has re-checked, because until M3 there was no adapter to
re-check them through:

* **R1** — LTS is an encoded value on the served graph, and a query-time custom model moves
  the route by it. ADR 0001 measured Ferry Building → de Young at 7,695 m neutral and
  10,863 m seeking LTS ≥ 3. If a graph is ever rebuilt without the import module those
  numbers collapse to one, and `/info` stops listing `lts`.
* **R4** — `POST /match` returns real `osm_way_id` values per edge, which is what scope
  6.2's accepted-road set is built from and what generate mode uses instead of geometric
  snapping.
* **The custom model needs `ch.disable`**, and the failure mode without it is a route that
  comes back looking fine.

    ./deploy/graphhopper/run.ps1 -Config deploy/graphhopper/config-bayarea-lts.yml
    uv run pytest tests/contract/test_graphhopper.py -m network
"""

from __future__ import annotations

from typing import Any

import pytest

from longrun.core.models.geometry import LatLon, Route, RoutePoint
from longrun.core.routing.base import NoRouteError
from longrun.core.routing.graphhopper import GraphHopperRouter, url_from_env

pytestmark = pytest.mark.network

#: ADR 0001's own pair, so the numbers here are comparable with the ones it recorded.
FERRY = LatLon(lat=37.7955, lon=-122.3937)
DEYOUNG = LatLon(lat=37.7715, lon=-122.4686)

AVOID = {"priority": [{"if": "lts >= 3", "multiply_by": "0.1"}]}
SEEK = {"priority": [{"if": "lts < 3", "multiply_by": "0.1"}]}


@pytest.fixture(scope="module")
def info() -> dict[str, Any]:
    """`/info` from whatever is listening, or a skip.

    A skip rather than a failure when the graph is a *different region*: one port serves
    one graph, and once a second region exists - which is scope 11's plan and M3's exit
    criterion - the server on 8989 is as likely to be Phoenix as the Bay Area. A test that
    failed instead would report "no route" for a question about the wrong continent's worth
    of graph, which is a true statement and a useless one.
    """
    import httpx

    url = url_from_env()
    try:
        payload = dict(httpx.get(f"{url}/info", timeout=5.0).json())
    except Exception as exc:  # noqa: BLE001 - any failure to reach it is a skip
        pytest.skip(f"no GraphHopper at {url}: {exc}")
        raise

    west, south, east, north = payload.get("bbox") or (0, 0, 0, 0)
    for point in (FERRY, DEYOUNG):
        if not (west <= point.lon <= east and south <= point.lat <= north):
            pytest.skip(
                f"the graph at {url} covers ({west:.1f},{south:.1f})-({east:.1f},{north:.1f}), "
                f"which is not the Bay Area"
            )
    return payload


@pytest.fixture(scope="module")
def router(info: dict[str, Any]) -> GraphHopperRouter:
    return GraphHopperRouter(url_from_env())


# --- R1: the encoded value ---------------------------------------------------


def test_the_served_graph_carries_the_lts_encoded_value(info: dict[str, Any]) -> None:
    """ADR 0001's mechanism: the import writes `lts`, `load()` rebuilds the encoding
    manager from the graph's own properties, and the stock jar serves it. A graph rebuilt
    without the import module still serves - it just quietly has no `lts` to route by."""
    assert "lts" in (info.get("encoded_values") or {}), sorted(info.get("encoded_values") or {})


def test_the_foot_profile_exists(info: dict[str, Any]) -> None:
    assert "foot" in {p["name"] for p in info.get("profiles") or []}


def test_a_custom_model_moves_the_route(router: GraphHopperRouter) -> None:
    """The claim scope 7.1 rests on. Seeking LTS >= 3 has to produce a *different and
    longer* route than avoiding it, or the encoded value is present and inert."""
    avoiding = router.route([FERRY, DEYOUNG], custom_model=AVOID)
    seeking = router.route([FERRY, DEYOUNG], custom_model=SEEK)
    assert seeking.length_m > avoiding.length_m * 1.2, (
        f"avoiding {avoiding.length_m:.0f} m, seeking {seeking.length_m:.0f} m - "
        f"the custom model is not reaching the graph"
    )


def test_avoiding_high_stress_is_close_to_neutral(router: GraphHopperRouter) -> None:
    """M0.5's finding, kept standing because it is what risk R1 turned into an M6 task:
    stock `foot_priority` already keeps pedestrians off arterials, so `avoid` and `neutral`
    differ by under a percent. The six parameters scope 7.1 wants have to be **fitted
    against real preference pairs**, not assumed to help."""
    neutral = router.route([FERRY, DEYOUNG])
    avoiding = router.route([FERRY, DEYOUNG], custom_model=AVOID)
    assert abs(avoiding.length_m - neutral.length_m) / neutral.length_m < 0.05


# --- the route itself --------------------------------------------------------


def test_a_route_comes_back_dense_enough_to_score(router: GraphHopperRouter) -> None:
    """A router emits points at junctions; the scorers sample per point. `path_to_route`
    densifies, and this is what would catch that being dropped."""
    from longrun.core.geo.gpx import DEFAULT_MAX_SPACING_M, haversine_m

    route = router.route([FERRY, DEYOUNG])
    gaps = [
        haversine_m(a.lat, a.lon, b.lat, b.lon)
        for a, b in zip(route.points[:-1], route.points[1:], strict=True)
    ]
    assert max(gaps) <= DEFAULT_MAX_SPACING_M + 1.0, f"largest gap {max(gaps):.0f} m"


def test_a_point_off_the_graph_fails_usefully(router: GraphHopperRouter) -> None:
    """Scope 6.4 wants a graceful no-route. The Pacific is comfortably off the Bay Area
    graph, and what comes back has to name which point could not be placed."""
    with pytest.raises(NoRouteError) as caught:
        router.route([FERRY, LatLon(lat=37.0, lon=-127.0)])
    assert "point" in str(caught.value).lower()


def test_alternatives_are_distinct_routes(router: GraphHopperRouter) -> None:
    """Scope 8.1 step 6 needs candidates to score. Three copies of one route are not."""
    route = router.route([FERRY, DEYOUNG])
    found = router.alternatives(route, None, k=3)
    assert len(found) >= 2
    lengths = {round(alt.length_m) for alt in found}
    assert len(lengths) >= 2, lengths


def test_the_first_whole_route_alternative_is_the_route_itself(
    router: GraphHopperRouter,
) -> None:
    """`alternative_route.max_paths` is a total, not an extra.

    Measured, because it decides what `--alternatives 3` means: the primary path comes
    back first, and between the same two endpoints that is the original route. A caller
    that already holds it - generate mode just drew it - asks for one more and skips the
    first, which `cli/plan.py` did not until M5.4 and so scored the same route twice.
    """
    route = router.route([FERRY, DEYOUNG])
    found = router.alternatives(route, None, k=3)
    assert round(found[0].length_m) == round(route.length_m)


def test_a_detour_leaves_the_span_it_was_asked_to_avoid(router: GraphHopperRouter) -> None:
    """Scope 8.1 step 6's actual request: reroute what was flagged, keep the rest."""
    from shapely.geometry import Point, shape

    from longrun.core.routing.detour import detour_area

    route = router.route([FERRY, DEYOUNG])
    span = (route.length_m * 0.40, route.length_m * 0.50)
    area = shape(detour_area(route, *span)["geometry"])

    found = router.alternatives(route, span, k=1)

    assert found, "a detour around a mid-route span in a dense grid should exist"
    detour = found[0]
    assert detour.length_m > route.length_m
    assert not any(area.contains(Point(p.lon, p.lat)) for p in detour.points)


def test_pinning_a_span_without_avoiding_it_returns_the_same_route(
    router: GraphHopperRouter,
) -> None:
    """The control, and the reason the avoid-area exists.

    The via points sit *on* the route, so a router asked to pass through them honours the
    request by going exactly where it already went - measured at 0 m divergence. Without
    this test the pins look like they are doing the work, and the first person to simplify
    the avoid-area away would find every detour silently returning the original.
    """
    from longrun.core.routing.detour import detour_waypoints
    from longrun.core.routing.graphhopper import route_body

    route = router.route([FERRY, DEYOUNG])
    span = (route.length_m * 0.40, route.length_m * 0.50)
    pins = detour_waypoints(route, *span)

    pinned = router.paths(route_body(pins), pins)

    assert round(pinned[0]["distance"]) == round(route.length_m)


def test_the_alternatives_algorithm_refuses_via_points(router: GraphHopperRouter) -> None:
    """Why a detour is a single-answer request.

    GraphHopper answers "alternative routes work only with start and end point", and it
    refuses rather than degrading - so asking for both would make every detour an empty
    list rather than a worse one. Pinned here because it is a property of the server, and
    a server upgrade that lifted it is something this project would want to know about.
    """
    from longrun.core.routing.detour import detour_waypoints
    from longrun.core.routing.graphhopper import route_body

    route = router.route([FERRY, DEYOUNG])
    pins = detour_waypoints(route, route.length_m * 0.40, route.length_m * 0.50)
    assert len(pins) == 4

    with pytest.raises(NoRouteError, match="start and end point"):
        router.paths(route_body(pins, alternatives=3), pins)


# --- R4: map matching --------------------------------------------------------


def test_map_matching_returns_real_osm_way_ids(router: GraphHopperRouter) -> None:
    """Risk R4's answer, and the reason the geometry-hashing fallback was dropped from the
    plan. These are OSM way ids, so they join directly against `osm.ways`."""
    route = router.route([FERRY, DEYOUNG])
    matched, way_ids = router.map_match(route)
    known = [w for w in way_ids if w is not None]
    assert len(way_ids) == len(matched.points)
    assert len(known) > len(way_ids) // 2, f"only {len(known)} of {len(way_ids)} matched"
    # OSM way ids are positive and large; a small integer would mean an index leaked out.
    assert all(w > 1000 for w in known)
    assert len(set(known)) > 10, "one way for a 7 km route is not a match"


def test_map_matching_a_track_that_is_not_on_the_graph_degrades(
    router: GraphHopperRouter,
) -> None:
    """It must return the track unchanged rather than raise: an unmatched route is still
    scoreable from geometry alone, and `NullRouter` sets that expectation."""
    offshore = Route(
        id="t",
        points=[
            RoutePoint(lat=37.0 + i * 0.001, lon=-127.0, cum_dist_m=i * 111.0) for i in range(5)
        ],
    )
    matched, way_ids = router.map_match(offshore)
    assert matched.points == offshore.points
    assert way_ids == [None] * len(offshore.points)
