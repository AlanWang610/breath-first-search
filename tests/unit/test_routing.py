"""The GraphHopper adapter and route conditioning (scope 4.3, 6.1; ADR 0001).

No server here. What is tested is the request body, the response parsing and the two
geometry corrections a routed path needs — everything that can be wrong while the HTTP
works fine. The live half is `tests/contract/test_graphhopper.py`.

The request body earns most of the attention because it is the whole contract with the
router and two of its fields are silently wrong rather than loudly wrong: `points` is
**[lon, lat]** where everything else in this codebase is (lat, lon), and without
`ch.disable` a custom model is ignored on a CH-prepared profile rather than rejected. Both
produce a route. Neither produces the right one.
"""

from __future__ import annotations

from typing import Any

import pytest

from longrun.core.geo.gpx import DEFAULT_MAX_SPACING_M, densify, haversine_m, normalize
from longrun.core.models.geometry import LatLon, Route, RoutePoint
from longrun.core.routing.base import NoRouteError, RouterUnavailable
from longrun.core.routing.graphhopper import (
    _routing_error,
    _way_ids_per_point,
    path_to_route,
    route_body,
    url_from_env,
)

FERRY = LatLon(lat=37.7955, lon=-122.3937)
DEYOUNG = LatLon(lat=37.7715, lon=-122.4686)


# --- the request body -------------------------------------------------------


def test_points_go_out_as_lon_lat() -> None:
    """Everything else in this codebase is (lat, lon). GraphHopper is not, and a swap
    routes across the Indian Ocean or, worse, somewhere plausible."""
    body = route_body([FERRY, DEYOUNG])
    assert body["points"] == [[-122.3937, 37.7955], [-122.4686, 37.7715]]


def test_flexible_mode_is_always_on() -> None:
    """A custom model on a CH-prepared profile is *ignored*, not rejected. The route comes
    back looking fine and costed by the wrong model."""
    assert route_body([FERRY, DEYOUNG])["ch.disable"] is True
    assert route_body([FERRY, DEYOUNG], custom_model={"priority": []})["ch.disable"] is True


def test_a_custom_model_is_sent_only_when_there_is_one() -> None:
    """An empty `custom_model` key is not the same as no key: GraphHopper validates it."""
    assert "custom_model" not in route_body([FERRY, DEYOUNG])
    model = {"priority": [{"if": "lts >= 3", "multiply_by": "0.2"}]}
    assert route_body([FERRY, DEYOUNG], custom_model=model)["custom_model"] == model


def test_avoid_polygons_become_inline_areas_with_a_priority_rule() -> None:
    """Named `custom_areas` are an import-time artefact of the graph, so a per-plan
    avoidance has to travel in the body - as a FeatureCollection the model refers to."""
    polygon = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    model = route_body([FERRY, DEYOUNG], avoid_polygons=[polygon])["custom_model"]
    assert model["areas"]["type"] == "FeatureCollection"
    feature = model["areas"]["features"][0]
    assert feature["geometry"] == polygon
    assert {"if": f"in_{feature['id']}", "multiply_by": "0"} in model["priority"]


def test_avoid_polygons_do_not_discard_an_existing_priority_rule() -> None:
    """Both are `priority` entries, and overwriting one with the other would silently
    drop whichever the caller cared about."""
    keep = {"if": "lts >= 3", "multiply_by": "0.2"}
    model = route_body(
        [FERRY, DEYOUNG],
        avoid_polygons=[{"type": "Polygon", "coordinates": []}],
        custom_model={"priority": [keep]},
    )["custom_model"]
    assert keep in model["priority"]
    assert len(model["priority"]) == 2


def test_alternatives_are_asked_for_explicitly() -> None:
    body = route_body([FERRY, DEYOUNG], alternatives=3)
    assert body["algorithm"] == "alternative_route"
    assert body["alternative_route.max_paths"] == 3
    assert "algorithm" not in route_body([FERRY, DEYOUNG])


def test_the_url_comes_from_the_environment_or_the_default() -> None:
    assert url_from_env({}) == "http://localhost:8989"
    assert url_from_env({"LONGRUN_GRAPHHOPPER_URL": "http://elsewhere:1"}) == "http://elsewhere:1"
    assert url_from_env({"LONGRUN_GRAPHHOPPER_URL": "  "}) == "http://localhost:8989"


# --- the response -----------------------------------------------------------


def _path(coords: list[list[float]]) -> dict[str, Any]:
    return {"points": {"type": "LineString", "coordinates": coords}}


def test_a_path_becomes_a_route_with_its_own_cumulative_distance() -> None:
    """Recomputed, never interpolated from `path["distance"]`: `cum_dist_m` is what every
    segment boundary and every ETA keys off, and a router's points are unevenly spaced."""
    route = path_to_route(_path([[-122.4, 37.79], [-122.4, 37.80], [-122.4, 37.81]]), "r")
    assert route.points[0].cum_dist_m == 0.0
    assert route.points[-1].cum_dist_m == pytest.approx(route.length_m, rel=1e-9)


def test_a_routed_path_is_densified() -> None:
    """A router emits points at junctions only - 133 m apart on a straight San Francisco
    street - and every per-point measurement in the system is that much coarser."""
    route = path_to_route(_path([[-122.4, 37.79], [-122.4, 37.81]]), "r")
    gaps = [
        haversine_m(a.lat, a.lon, b.lat, b.lon)
        for a, b in zip(route.points[:-1], route.points[1:], strict=True)
    ]
    assert len(route.points) > 2
    assert max(gaps) <= DEFAULT_MAX_SPACING_M + 1e-6


def test_a_path_with_fewer_than_two_points_is_not_a_route() -> None:
    with pytest.raises(NoRouteError):
        path_to_route(_path([[-122.4, 37.79]]), "r")


def test_a_client_error_is_a_routing_failure_and_a_server_error_is_an_outage() -> None:
    """They need different things done about them: one is a route to change, the other a
    service to restart, and scope 6.4 asks for the graceful version of the first."""

    class _Response:
        def __init__(self, status: int, payload: dict) -> None:
            self.status_code = status
            self._payload = payload
            self.text = ""

        def json(self) -> dict:
            return self._payload

    bad = _routing_error(
        _Response(400, {"message": "Connection between locations not found"}), [FERRY, DEYOUNG]
    )
    assert isinstance(bad, NoRouteError)
    assert "Connection between locations" in str(bad)
    assert isinstance(_routing_error(_Response(503, {}), [FERRY, DEYOUNG]), RouterUnavailable)


def test_a_routing_error_keeps_the_hint_the_server_gave() -> None:
    """ "Could not route" is useless where "nearest connected point is 240 m away" is
    actionable, and GraphHopper puts that in `hints`."""

    class _Response:
        status_code = 400
        text = ""

        @staticmethod
        def json() -> dict:
            return {"hints": [{"message": "Cannot find point 1: 37.8,-122.4"}]}

    assert "Cannot find point 1" in str(_routing_error(_Response(), [FERRY, DEYOUNG]))


# --- way id details ---------------------------------------------------------


def test_detail_ranges_are_half_open_at_the_end() -> None:
    """The off-by-one here is invisible: the route still scores, with every way id shifted
    one point along, and every tag-driven scorer quietly describes the wrong way."""
    ids = _way_ids_per_point({"details": {"osm_way_id": [[0, 2, 111], [2, 4, 222]]}}, 5)
    assert ids == [111, 111, 222, 222, None]


def test_a_detail_range_past_the_end_does_not_overflow() -> None:
    assert _way_ids_per_point({"details": {"osm_way_id": [[0, 99, 111]]}}, 3) == [111, 111, 111]


def test_a_null_detail_value_is_not_a_way_id() -> None:
    assert _way_ids_per_point({"details": {"osm_way_id": [[0, 2, None]]}}, 3) == [None] * 3


def test_no_details_is_no_ids_rather_than_an_error() -> None:
    assert _way_ids_per_point({}, 2) == [None, None]


# --- densify ----------------------------------------------------------------


def _line(count: int, step_deg: float) -> list[RoutePoint]:
    return normalize([(37.79 + i * step_deg, -122.4, None) for i in range(count)])


def test_densify_leaves_a_dense_route_alone() -> None:
    dense = _line(5, 0.0002)  # ~22 m apart
    assert len(densify(dense)) == len(dense)


def test_densify_recomputes_every_distance_after_an_inserted_point() -> None:
    """Inserting a point changes every cumulative distance after it, and `cum_dist_m` is
    what the pacing model integrates and what segment boundaries are cut on."""
    sparse = _line(3, 0.002)  # ~222 m apart
    filled = densify(sparse)
    assert filled[0].cum_dist_m == 0.0
    for a, b in zip(filled[:-1], filled[1:], strict=True):
        assert b.cum_dist_m > a.cum_dist_m
    route = Route(id="r", points=filled)
    assert filled[-1].cum_dist_m == pytest.approx(route.length_m, rel=1e-9)


def test_densify_does_not_invent_elevation() -> None:
    """Elevation comes from the terrain model, never from the route (scope 7.1). An
    interpolated one would be a number nobody measured, indistinguishable from one
    `sample_elevation` produced."""
    sparse = [
        RoutePoint(lat=37.79, lon=-122.4, ele_m=10.0, cum_dist_m=0.0),
        RoutePoint(lat=37.80, lon=-122.4, ele_m=90.0, cum_dist_m=1112.0),
    ]
    inserted = [p for p in densify(sparse) if p.ele_m is None]
    assert inserted, "nothing was inserted, so this proves nothing"


def test_densify_is_a_no_op_on_a_route_too_short_to_have_a_gap() -> None:
    assert densify([]) == []
    single = [RoutePoint(lat=37.79, lon=-122.4, cum_dist_m=0.0)]
    assert densify(single) == single
