"""A routed line, summarised from the router's own details (M23; ADR 0020, amended).

A preference pair compares two lines the router drew, and what it stores is metres per way
class. Until M23 that summary could only come from matching a GPX against a ways layer,
which no built region has outside a golden's fixtures - so no pair could be made on ground
a person actually runs. GraphHopper already carries `lts`, `surface` and `road_class` on
every edge it serves; these pin how a path's `details` become a `RouteSummary`, and that the
summary speaks the same vocabulary the custom model's terms match on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from longrun.core.data.cache import SqliteCache, args_hash
from longrun.core.geo.gpx import haversine_m
from longrun.core.models.context import Budget
from longrun.core.models.geometry import LatLon
from longrun.core.routing.base import NullRouter, RouterUnavailable
from longrun.core.routing.cached import CachedRouter, route_args
from longrun.core.routing.custom_model import (
    COLLECTOR_ROAD_CLASSES,
    PATH_ROAD_CLASSES,
    UNPAVED_SURFACES,
    CustomModelParams,
    to_custom_model,
)
from longrun.core.routing.graphhopper import (
    SUMMARY_DETAILS,
    path_to_summary,
    path_way_classes,
    route_body,
    summaries_of,
    summary_details,
)
from longrun.core.routing.preferences import way_class
from longrun.core.routing.tuning import WayClass

A = LatLon(lat=37.8300, lon=-122.5000)
B = LatLon(lat=37.8400, lon=-122.4900)

#: Four points, three edges, and every reconciliation `path_way_classes` owns:
#: edge 0 - an LTS 3 collector on asphalt; edge 1 - a footway the importer stored LTS 0 on;
#: edge 2 - a compacted track, which is the Marin fire road the M23 surface fix was for.
COORDS = [[-122.5000, 37.8300], [-122.4990, 37.8305], [-122.4980, 37.8310], [-122.4970, 37.8320]]
PATH: dict[str, Any] = {
    "points": {"coordinates": COORDS},
    "details": {
        "lts": [[0, 1, 3], [1, 3, 0]],
        "surface": [[0, 2, "asphalt"], [2, 3, "compacted"]],
        "road_class": [[0, 1, "secondary"], [1, 2, "footway"], [2, 3, "track"]],
    },
}


def _edge(index: int) -> float:
    (lon_a, lat_a), (lon_b, lat_b) = COORDS[index], COORDS[index + 1]
    return haversine_m(lat_a, lon_a, lat_b, lon_b)


def test_each_edge_gets_the_class_its_details_say() -> None:
    classes = path_way_classes(PATH)

    assert [klass for klass, _ in classes] == [
        WayClass(lts=3, unpaved=False, is_path=False, is_collector=True),
        WayClass(lts=None, unpaved=False, is_path=True, is_collector=False),
        WayClass(lts=None, unpaved=True, is_path=False, is_collector=False),
    ]
    assert [metres for _, metres in classes] == pytest.approx([_edge(0), _edge(1), _edge(2)])


def test_lts_zero_is_unknown_not_low_stress() -> None:
    """The importer stores 0 on ways it could not score (docs/tuning.md). Read as LTS 0 - or
    worse, as LTS 1 - it would charge the fitter's multipliers against missing data."""
    classes = path_way_classes(PATH)

    assert classes[1][0].lts is None and classes[2][0].lts is None


def test_a_path_with_no_details_is_unknown_everywhere_rather_than_defaulted() -> None:
    bare = {"points": {"coordinates": COORDS}}

    assert {klass for klass, _ in path_way_classes(bare)} == {WayClass()}


def test_the_summary_is_every_metre_of_the_line() -> None:
    summary = path_to_summary(PATH, "a")

    assert summary.name == "a"
    assert summary.length_m == pytest.approx(sum(_edge(i) for i in range(3)))
    assert sum(summary.metres_by_class.values()) == pytest.approx(summary.length_m)


def test_the_primary_keeps_the_name_and_alternatives_are_numbered() -> None:
    lines = summaries_of([PATH, PATH], "neutral")

    assert [summary.name for _, summary in lines] == ["neutral", "neutral-alt1"]
    assert [route.id for route, _ in lines] == ["neutral", "neutral-alt1"]


# --- one vocabulary --------------------------------------------------------------


@pytest.mark.parametrize("surface", UNPAVED_SURFACES)
def test_a_surface_the_router_calls_unpaved_the_fitter_does_too(surface: str) -> None:
    """The M23 finding: the fitter counted `compacted`, `fine_gravel` and `grass` as unpaved
    and the router's term skipped them, so a vector fitted on a fire road was costed on ways
    its own `unpaved` term never touched. Now both read one list."""
    detail = path_way_classes(
        {"points": {"coordinates": COORDS[:2]}, "details": {"surface": [[0, 1, surface.lower()]]}}
    )[0][0]

    assert detail.unpaved
    assert way_class({"highway": "track", "surface": surface.lower()}).unpaved


def test_earth_is_dirt_to_the_router_and_so_unpaved_to_the_fitter() -> None:
    """GraphHopper's `Surface.SURFACE_MAP` folds `earth` into `DIRT` on import."""
    assert way_class({"highway": "path", "surface": "earth"}).unpaved


@pytest.mark.parametrize(
    ("road_class", "is_path", "is_collector"),
    [(value, True, False) for value in PATH_ROAD_CLASSES]
    + [(value, False, True) for value in COLLECTOR_ROAD_CLASSES]
    + [("RESIDENTIAL", False, False), ("TRACK", False, False)],
)
def test_a_road_class_is_a_path_or_a_collector_by_the_terms_that_match_it(
    road_class: str, is_path: bool, is_collector: bool
) -> None:
    detail = path_way_classes(
        {
            "points": {"coordinates": COORDS[:2]},
            "details": {"road_class": [[0, 1, road_class.lower()]]},
        }
    )[0][0]
    tags = way_class({"highway": road_class.lower()})

    assert (detail.is_path, detail.is_collector) == (is_path, is_collector)
    assert (tags.is_path, tags.is_collector) == (is_path, is_collector)


def test_the_unpaved_term_names_every_unpaved_surface() -> None:
    model = to_custom_model(CustomModelParams(unpaved=2.0))

    condition = model["priority"][0]["if"]
    for surface in UNPAVED_SURFACES:
        assert f"surface == {surface}" in condition


# --- the request, the key, and the recording ---------------------------------------


def test_details_are_asked_for_only_when_wanted() -> None:
    assert "details" not in route_body([A, B])
    assert route_body([A, B], details=SUMMARY_DETAILS)["details"] == list(SUMMARY_DETAILS)


def test_a_graph_without_lts_is_not_asked_for_it() -> None:
    """Asking a fallback graph for `lts` is a routing error, not an empty column."""
    assert "lts" not in summary_details(has_lts=False)
    assert summary_details(has_lts=True) == SUMMARY_DETAILS


def test_a_request_for_details_is_a_different_question() -> None:
    """A recording made without details served to a request that wants them would read as a
    line of unknown ways - a summary of nothing, indistinguishable from an unmapped route."""
    plain = args_hash(route_args(route_body([A, B]), [A, B], "g"))
    detailed = args_hash(route_args(route_body([A, B], details=SUMMARY_DETAILS), [A, B], "g"))

    assert plain != detailed
    assert "details" not in route_args(route_body([A, B]), [A, B], "g")


class _Engine:
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def paths(self, body: dict[str, Any], waypoints: Any) -> list[dict[str, Any]]:
        self.bodies.append(body)
        return [PATH, PATH]

    def map_match(self, track: Any) -> Any:  # pragma: no cover - not asked
        raise AssertionError


def test_summaries_are_recorded_charged_once_and_replayed(tmp_path: Path) -> None:
    engine = _Engine()
    budget = Budget()
    with SqliteCache(tmp_path / "c.sqlite", offline=False) as cache:
        router = CachedRouter(engine, cache, budget, graph="g")

        first = router.route_summaries([A, B], alternatives=2, name="neutral")
        again = router.route_summaries([A, B], alternatives=2, name="neutral")

    assert len(engine.bodies) == 1 and budget.api_calls_used == 1
    assert engine.bodies[0]["details"] == list(SUMMARY_DETAILS)
    assert engine.bodies[0]["alternative_route.max_paths"] == 2
    assert [s for _, s in first] == [s for _, s in again]


def test_alternatives_are_not_asked_for_through_a_via_point(tmp_path: Path) -> None:
    """GraphHopper refuses the alternatives algorithm with more than two points."""
    engine = _Engine()
    with SqliteCache(tmp_path / "c.sqlite", offline=False) as cache:
        CachedRouter(engine, cache, Budget(), graph="g").route_summaries(
            [A, LatLon(lat=37.835, lon=-122.495), B], alternatives=3
        )

    assert "algorithm" not in engine.bodies[0]


def test_no_router_is_a_refusal_not_an_empty_summary() -> None:
    with pytest.raises(RouterUnavailable):
        NullRouter().route_summaries([A, B])
