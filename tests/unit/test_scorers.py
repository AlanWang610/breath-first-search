"""Tag-and-service scorer tests (scope 3.2, 3.6, 7.2, 7.5, 7.6, 8.3, 12).

Every corridor here is synthetic and hand-tagged, so each expected LTS level, crossing,
surface run and water gap is known *by construction* rather than by running the code and
writing down what it said. Where a value is derived from geometry it is asserted against
the route's own `cum_dist_m`, not against a hardcoded metre count.

Four properties get more attention than the arithmetic, because they are the ones that
would quietly rot:

* a scorer never raises — an absent layer produces coverage, not an exception (scope 3.6);
* a missing tag is *unknown* and lowers confidence, never *absent* (scope 12);
* hard flags come from `core.preferences.floors` and cannot be silenced by a profile;
* `reason_code` is machine-stable, so nothing below asserts on prose.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, box

from longrun.core.data.cache import SqliteCache
from longrun.core.data.file_store import FileLayerStore, FileRasterStore
from longrun.core.geo.gpx import normalize
from longrun.core.geo.segments import segment_route
from longrun.core.models.context import FrozenClock, ScorerContext
from longrun.core.models.coverage import CoverageManifest
from longrun.core.models.geometry import Route, Segment
from longrun.core.models.measurement import FlagKind, ScorerResult, Tier
from longrun.core.models.profile import PreferenceEntry, PreferenceProfile
from longrun.core.preferences.floors import LTS_HARD
from longrun.core.preferences.store import apply_overrides, load_defaults
from longrun.core.scorers import crossings as crossings_mod
from longrun.core.scorers import hostility as hostility_mod
from longrun.core.scorers import legality as legality_mod
from longrun.core.scorers import services as services_mod
from longrun.core.scorers import stop_density as stop_density_mod
from longrun.core.scorers import surface as surface_mod
from longrun.core.scorers.crossings import classify, crossings, grade_separated
from longrun.core.scorers.hostility import (
    ROUTE_SUMMARY_ID,
    UNKNOWN_WAY_CONFIDENCE,
    segment_hostility,
    severity_for_level,
)
from longrun.core.scorers.legality import legality, violation_of
from longrun.core.scorers.services import max_gap_m, services_along
from longrun.core.scorers.stop_density import stop_density, window_density
from longrun.core.scorers.surface import (
    UNKNOWN_SURFACE_CONFIDENCE,
    has_shoulder,
    on_shoulder,
    surface_class,
    surface_profile,
)

LON = -122.40
LAT0 = 37.77

#: ~100 m of latitude. Kept coarse enough that segment boundaries are easy to reason about.
STEP_DEG = 0.0009

SCORER_MODULES = [
    legality_mod,
    hostility_mod,
    crossings_mod,
    stop_density_mod,
    surface_mod,
    services_mod,
]


# --- fixture construction ---------------------------------------------------


def _route(n: int, lon: float = LON) -> Route:
    """A straight northbound route with `n` points about 100 m apart."""
    return Route(id="r", points=normalize([(LAT0 + i * STEP_DEG, lon, None) for i in range(n)]))


def _segments(route: Route, way_ids: list[int], max_len_m: float = 10_000.0) -> list[Segment]:
    return segment_route(route, way_ids=way_ids, max_len_m=max_len_m)


def _lat(route: Route, index: int) -> float:
    return route.points[index].lat


def _write(path: Path, records: Sequence[dict[str, Any]], geometries: Sequence[Any]) -> None:
    """Write one GeoPackage layer, suppressing pyogrio's chatter about the driver."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        gpd.GeoDataFrame(list(records), geometry=list(geometries), crs="EPSG:4326").to_file(
            path, driver="GPKG"
        )


def _way_line(route: Route, way_ids: list[int], way_id: int) -> LineString:
    """The stretch of route a way carries, as its geometry."""
    indices = [i for i, wid in enumerate(way_ids) if wid == way_id]
    low, high = min(indices), min(max(indices) + 1, len(route.points) - 1)
    return LineString([(route.points[i].lon, route.points[i].lat) for i in range(low, high + 1)])


def _write_ways(
    root: Path,
    route: Route,
    way_ids: list[int],
    tags: dict[int, dict[str, Any]],
    extra: Iterable[tuple[int, dict[str, Any], Any]] = (),
) -> None:
    records: list[dict[str, Any]] = []
    geometries: list[Any] = []
    for way_id, way_tags in tags.items():
        records.append({"way_id": way_id, **way_tags})
        geometries.append(_way_line(route, way_ids, way_id))
    for way_id, way_tags, geometry in extra:
        records.append({"way_id": way_id, **way_tags})
        geometries.append(geometry)
    _write(root / "ways.gpkg", records, geometries)


def _crossing_way(route: Route, index: int) -> LineString:
    """An east-west line crossing the route at one of its points."""
    lat = _lat(route, index)
    return LineString([(LON - 0.002, lat), (LON + 0.002, lat)])


def _write_points(root: Path, records: Sequence[dict[str, Any]], geometries: Sequence[Any]) -> None:
    """Write point features to both node layers.

    Signals and gates come from `nodes`; water, toilets and food come from `amenities`.
    A test fixture writes both so a scorer reading either finds its data - otherwise a
    change to which layer a scorer consults turns its tests vacuous instead of red.
    """
    _write(root / "amenities.gpkg", records, geometries)
    _write(root / "nodes.gpkg", records, geometries)


#: Caches opened by `_ctx`, closed after each test by `_close_caches`.
_OPEN_CACHES: list[SqliteCache] = []


@pytest.fixture(autouse=True)
def _close_caches() -> Iterator[None]:
    """Close every cache `_ctx` opened during a test.

    An in-memory SQLite database still holds a connection, and leaving it to the garbage
    collector raises a ResourceWarning attributed to whatever code happened to trigger the
    collection - so the traceback points somewhere irrelevant. Left alone it is only noise,
    but it is the noise that would hide a real handle leak later, when the cache is a file
    on disk and Windows is holding a lock on it.
    """
    _OPEN_CACHES.clear()
    try:
        yield
    finally:
        for cache in _OPEN_CACHES:
            cache.close()
        _OPEN_CACHES.clear()


def _ctx(root: Path, profile: PreferenceProfile | None = None) -> ScorerContext:
    cache = SqliteCache(offline=True)
    _OPEN_CACHES.append(cache)
    return ScorerContext(
        layers=FileLayerStore(root),
        rasters=FileRasterStore(root),
        cache=cache,
        clock=FrozenClock(datetime(2026, 9, 4, 7, 0)),
        coverage=CoverageManifest(),
        profile=profile if profile is not None else load_defaults(),
    )


def _codes(result: ScorerResult) -> list[str]:
    return [flag.reason_code for flag in result.flags]


def _flag(result: ScorerResult, code: str) -> Any:
    return next(flag for flag in result.flags if flag.reason_code == code)


def _values(result: ScorerResult, segment_id: str) -> dict[str, Any]:
    return next(m for m in result.measurements if m.segment_id == segment_id).values


def _confidence(result: ScorerResult, segment_id: str) -> float:
    return next(m for m in result.measurements if m.segment_id == segment_id).confidence


def _summary(result: ScorerResult) -> dict[str, Any]:
    return _values(result, ROUTE_SUMMARY_ID)


# --- the contract every scorer keeps ----------------------------------------


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        (legality_mod, "legality"),
        (hostility_mod, "segment_hostility"),
        (crossings_mod, "crossings"),
        (stop_density_mod, "stop_density"),
        (surface_mod, "surface_profile"),
        (services_mod, "services_along"),
    ],
)
def test_module_name_matches_the_scope_tool_name(module: Any, expected: str) -> None:
    """The tool names in scope 7.2/7.5/7.6 are the registry keys; drift breaks lookup."""
    assert module.name == expected
    assert callable(getattr(module, module.name))


@pytest.mark.parametrize("module", SCORER_MODULES, ids=lambda m: m.name)
def test_absent_layers_produce_coverage_not_an_exception(tmp_path: Path, module: Any) -> None:
    """A scorer that raises takes the whole plan down; one that reports tells the truth."""
    route = _route(5)
    segments = _segments(route, [1] * 5)
    ctx = _ctx(tmp_path)

    result = getattr(module, module.name)(route, segments, ctx)

    assert result.name == module.name
    assert result.flags == []
    assert result.coverage
    assert all(not entry.checked for entry in result.coverage)
    assert result.coverage[0].reason


@pytest.mark.parametrize("module", SCORER_MODULES, ids=lambda m: m.name)
def test_a_scorer_does_not_write_to_the_plan_manifest_itself(tmp_path: Path, module: Any) -> None:
    """Scope 4.1: a scorer is a pure function of its inputs. Coverage is *returned*, and
    the scoring loop copies it into the manifest with `base.publish`. A scorer that also
    published its own would double every line of the sheet's coverage section."""
    route = _route(5)
    ctx = _ctx(tmp_path)

    result = getattr(module, module.name)(route, _segments(route, [1] * 5), ctx)

    assert result.coverage
    assert ctx.coverage.entries == []


@pytest.mark.parametrize("module", SCORER_MODULES, ids=lambda m: m.name)
def test_the_registry_alias_is_the_same_function(module: Any) -> None:
    """`core.scorers` is loaded by name; `module.score` is what the loop dispatches on."""
    assert module.score is getattr(module, module.name)


# --- legality (scope 7.6, gpx_verify check 5) -------------------------------


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"highway": "motorway"}, "motorway"),
        ({"highway": "motorway_link"}, "motorway"),
        ({"highway": "residential", "foot": "no"}, "foot_no"),
        ({"highway": "path", "foot": "private"}, "foot_private"),
        ({"highway": "service", "access": "private"}, "access_private"),
        ({"highway": "service", "access": "no"}, "access_no"),
        ({"railway": "rail"}, "railway_row"),
        ({"railway": "light_rail"}, "railway_row"),
        # Legal, and each for a different reason.
        ({"highway": "residential"}, None),
        ({"highway": "footway"}, None),
        ({"highway": "service", "access": "private", "foot": "yes"}, None),
        ({"highway": "service", "access": "private", "foot": "designated"}, None),
        ({"railway": "abandoned"}, None),
        ({"railway": "tram", "highway": "residential"}, None),
        ({"railway": "rail", "foot": "designated"}, None),
    ],
)
def test_violation_table(tags: dict[str, Any], expected: str | None) -> None:
    assert violation_of(tags) == expected


def test_a_mode_tag_beats_a_general_access_tag() -> None:
    """`access=private` + `foot=yes` is a driveway you may walk; flagging it cries wolf."""
    assert violation_of({"highway": "service", "access": "private"}) == "access_private"
    assert violation_of({"highway": "service", "access": "private", "foot": "yes"}) is None


def test_motorway_segment_is_a_hard_safety_flag(tmp_path: Path) -> None:
    route = _route(7)
    way_ids = [1] * 3 + [2] * 4
    _write_ways(
        tmp_path,
        route,
        way_ids,
        {1: {"highway": "residential"}, 2: {"highway": "motorway", "maxspeed": "100"}},
    )
    segments = _segments(route, way_ids)

    result = legality(route, segments, _ctx(tmp_path))

    assert _codes(result) == ["motorway"]
    flag = _flag(result, "motorway")
    assert flag.kind is FlagKind.HARD
    assert flag.tier is Tier.SAFETY
    assert flag.segment_id == segments[1].id
    assert flag.scorer == "legality"


def test_one_flag_per_segment_however_many_tags_prohibit_it(tmp_path: Path) -> None:
    """A motorway ramp that is also foot=no is one problem; two flags would double-count
    it in the per-tier weighted sum in `core.plan.arbitrate`."""
    route = _route(4)
    way_ids = [1] * 4
    _write_ways(tmp_path, route, way_ids, {1: {"highway": "motorway", "foot": "no"}})

    result = legality(route, _segments(route, way_ids), _ctx(tmp_path))

    assert _codes(result) == ["motorway"]


def test_an_unmatched_way_is_unknown_not_legal(tmp_path: Path) -> None:
    """Scope 12: no tags is not evidence of permission, and not evidence of prohibition."""
    route = _route(4)
    way_ids = [99] * 4
    _write_ways(tmp_path, route, [1] * 4, {1: {"highway": "residential"}})
    segments = _segments(route, way_ids)

    result = legality(route, segments, _ctx(tmp_path))

    assert result.flags == []
    assert _values(result, segments[0].id)["prohibited"] is None
    assert _confidence(result, segments[0].id) == UNKNOWN_WAY_CONFIDENCE


def test_legality_records_the_layer_it_checked(tmp_path: Path) -> None:
    route = _route(4)
    way_ids = [1] * 4
    _write_ways(tmp_path, route, way_ids, {1: {"highway": "residential"}})
    ctx = _ctx(tmp_path)

    result = legality(route, _segments(route, way_ids), ctx)

    assert [e.source for e in result.coverage] == ["ways"]
    assert result.coverage[0].checked


# --- hostility (scope 7.2, 8.3) ---------------------------------------------

#: Hand-computed against the Furth rules in `core.routing.lts`, one way per level.
LTS_WAYS: dict[int, dict[str, Any]] = {
    1: {"highway": "footway"},
    2: {"highway": "secondary", "maxspeed": "50", "sidewalk": "both"},
    3: {"highway": "secondary", "maxspeed": "50"},
    4: {"highway": "primary", "maxspeed": "70", "sidewalk": "no", "lanes": "4"},
}


@pytest.fixture
def lts_corridor(tmp_path: Path) -> tuple[Path, Route, list[Segment]]:
    """Four ways, one per LTS level, three points each."""
    route = _route(13)
    way_ids = [1] * 3 + [2] * 3 + [3] * 3 + [4] * 4
    _write_ways(tmp_path, route, way_ids, LTS_WAYS)
    return tmp_path, route, _segments(route, way_ids)


def test_lts_level_is_measured_per_segment(lts_corridor: tuple[Path, Route, list[Segment]]) -> None:
    root, route, segments = lts_corridor

    result = segment_hostility(route, segments, _ctx(root))

    assert [_values(result, s.id)["lts"] for s in segments] == [1, 2, 3, 4]


def test_lts_4_is_a_hard_safety_flag(lts_corridor: tuple[Path, Route, list[Segment]]) -> None:
    root, route, segments = lts_corridor

    result = segment_hostility(route, segments, _ctx(root))
    flag = _flag(result, "lts_4")

    assert flag.kind is FlagKind.HARD
    assert flag.tier is Tier.SAFETY
    assert flag.severity == 1.0
    assert flag.segment_id == segments[3].id


def test_lts_3_is_a_soft_comfort_flag(lts_corridor: tuple[Path, Route, list[Segment]]) -> None:
    """Scope 8.4 calls an LTS 2 to 3 trade against shade a same-tier trade, which is only
    true if soft hostility sits in the comfort tier."""
    root, route, segments = lts_corridor

    flag = _flag(segment_hostility(route, segments, _ctx(root)), "lts_3")

    assert flag.kind is FlagKind.SOFT
    assert flag.tier is Tier.COMFORT
    assert flag.segment_id == segments[2].id


def test_raising_traffic_tolerance_silences_the_soft_flag_only(
    lts_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    root, route, segments = lts_corridor
    profile = apply_overrides(load_defaults(), {"traffic_tolerance": 3})

    result = segment_hostility(route, segments, _ctx(root, profile))

    assert _codes(result) == ["lts_4"]


def test_no_profile_can_suppress_the_lts_4_hard_flag(
    lts_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    """`floors.check_traffic_tolerance` refuses a 4, but this scorer does not depend on
    that: it tests the level against the floor, so a hand-built profile fails too."""
    root, route, segments = lts_corridor
    unchecked = PreferenceProfile(traffic_tolerance=PreferenceEntry(value=LTS_HARD))

    result = segment_hostility(route, segments, _ctx(root, unchecked))

    assert _flag(result, "lts_4").kind is FlagKind.HARD


def test_missing_tags_lower_confidence_rather_than_the_level(
    lts_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    """The LTS 3 way has no sidewalk tag. That is a weaker claim, not a different one."""
    root, route, segments = lts_corridor

    result = segment_hostility(route, segments, _ctx(root))

    assert _values(result, segments[2].id)["lts"] == 3
    assert _confidence(result, segments[2].id) < 1.0
    assert _confidence(result, segments[1].id) == 1.0


def test_an_unmatched_segment_is_unknown_not_low_stress(tmp_path: Path) -> None:
    route = _route(4)
    way_ids = [99] * 4
    _write_ways(tmp_path, route, [1] * 4, {1: {"highway": "residential"}})
    segments = _segments(route, way_ids)

    result = segment_hostility(route, segments, _ctx(tmp_path))

    assert result.flags == []
    assert _values(result, segments[0].id)["lts"] is None
    assert _confidence(result, segments[0].id) == UNKNOWN_WAY_CONFIDENCE


def test_an_aadt_column_is_used_when_the_frame_carries_one(tmp_path: Path) -> None:
    """Scope 7.2: HPMS volume where populated. Same tags, different level."""
    route = _route(4)
    way_ids = [1] * 4
    quiet = {"highway": "tertiary", "maxspeed": "50", "sidewalk": "both"}

    _write_ways(tmp_path, route, way_ids, {1: quiet})
    without = segment_hostility(route, _segments(route, way_ids), _ctx(tmp_path))

    busy = tmp_path / "busy"
    busy.mkdir()
    _write_ways(busy, route, way_ids, {1: {**quiet, "aadt": 25_000}})
    with_aadt = segment_hostility(route, _segments(route, way_ids), _ctx(busy))

    assert _values(without, "s00000")["lts"] == 1
    assert _values(with_aadt, "s00000")["lts"] == 2
    assert _values(with_aadt, "s00000")["aadt"] == 25_000


@pytest.mark.parametrize(("level", "severity"), [(1, 0.0), (2, 1 / 3), (3, 2 / 3), (4, 1.0)])
def test_severity_scales_with_the_level(level: int, severity: float) -> None:
    assert severity_for_level(level) == pytest.approx(severity)


def test_route_summary_reports_the_scope_7_1_acceptance_metrics(
    lts_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    """ "Fraction of length at LTS >= 3, count of LTS 4 segments" are the tuning metrics."""
    root, route, segments = lts_corridor
    lts3_m = segments[2].length_m + segments[3].length_m
    scored_m = sum(s.length_m for s in segments)

    summary = _summary(segment_hostility(route, segments, _ctx(root)))

    assert summary["lts4_count"] == 1
    assert summary["fraction_lts3_plus"] == pytest.approx(lts3_m / scored_m)


# --- crossings (scope 7.2, 8.3, gpx_verify check 8) -------------------------


@pytest.mark.parametrize(
    ("rank", "speed", "expected"),
    [
        (2, 50.0, "unsignalized_secondary_crossing"),
        (2, None, "unsignalized_secondary_crossing"),
        (3, 80.0, "unsignalized_primary_crossing"),
        (4, 41.0, "unsignalized_primary_crossing"),
        (3, 40.0, "unsignalized_primary_crossing_low_speed"),
        (3, None, "unsignalized_primary_crossing_unknown_speed"),
    ],
)
def test_classify_table(rank: int, speed: float | None, expected: str) -> None:
    """40 kph is the floor and the threshold is strictly above it (scope 8.3)."""
    assert classify(rank, speed) == expected


@pytest.mark.parametrize(
    ("route_tags", "crossed_tags", "separated"),
    [
        ({}, {"highway": "motorway"}, False),
        ({}, {"highway": "motorway", "bridge": "yes"}, True),
        ({}, {"highway": "motorway", "tunnel": "yes"}, True),
        ({"bridge": "yes"}, {"highway": "motorway"}, True),
        ({"bridge": "no"}, {"highway": "motorway", "tunnel": "no"}, False),
        ({"layer": "1"}, {"highway": "motorway", "layer": "0"}, True),
        ({"layer": "0"}, {"highway": "motorway", "layer": "0"}, False),
        (None, {"highway": "motorway"}, False),
    ],
)
def test_grade_separation_table(
    route_tags: dict[str, Any] | None, crossed_tags: dict[str, Any], separated: bool
) -> None:
    assert grade_separated(route_tags, crossed_tags) is separated


@pytest.fixture
def crossing_corridor(tmp_path: Path) -> tuple[Path, Route, list[Segment]]:
    """A 2 km route crossed at six known points, one of each interesting kind.

    Crossings sit on odd route points, which are mid-segment, so no assertion depends on
    which side of a boundary a rounding error lands.
    """
    route = _route(21)
    way_ids = [1] * 21
    extra = [
        (10, {"highway": "primary", "maxspeed": "80"}, _crossing_way(route, 3)),
        (11, {"highway": "secondary", "maxspeed": "50"}, _crossing_way(route, 7)),
        (12, {"highway": "primary", "maxspeed": "80"}, _crossing_way(route, 11)),
        (13, {"highway": "residential"}, _crossing_way(route, 15)),
        (14, {"highway": "motorway", "maxspeed": "110", "bridge": "yes"}, _crossing_way(route, 17)),
        (15, {"highway": "primary"}, _crossing_way(route, 19)),
    ]
    # The route's own way is itself a primary, so an implementation that reported every
    # intersecting road would report the road the runner is standing on.
    _write_ways(
        tmp_path, route, way_ids, {1: {"highway": "primary", "maxspeed": "80"}}, extra=extra
    )
    _write_points(
        tmp_path,
        [{"kind": "traffic_signals"}],
        [Point(LON, _lat(route, 11))],
    )
    return tmp_path, route, _segments(route, way_ids, max_len_m=250.0)


def test_fast_primary_without_a_signal_is_a_hard_safety_flag(
    crossing_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    root, route, segments = crossing_corridor

    flag = _flag(crossings(route, segments, _ctx(root)), "unsignalized_primary_crossing")

    assert flag.kind is FlagKind.HARD
    assert flag.tier is Tier.SAFETY
    assert flag.severity == 1.0
    assert flag.segment_id == segments[1].id


def test_secondary_without_a_signal_is_a_soft_comfort_flag(
    crossing_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    root, route, segments = crossing_corridor

    flag = _flag(crossings(route, segments, _ctx(root)), "unsignalized_secondary_crossing")

    assert flag.kind is FlagKind.SOFT
    assert flag.tier is Tier.COMFORT
    assert flag.segment_id == segments[3].id


def test_a_signalized_crossing_is_counted_and_not_flagged(
    crossing_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    """The signal sits on the primary at point 11; the same road unsignalized is a hard
    flag two kilometres earlier, so only the signal can explain the difference."""
    root, route, segments = crossing_corridor

    result = crossings(route, segments, _ctx(root))

    assert _values(result, segments[5].id)["crossings"] == 1
    assert _values(result, segments[5].id)["unsignalized"] == 0
    assert all(flag.segment_id != segments[5].id for flag in result.flags)


def test_a_road_below_the_class_threshold_is_not_a_crossing(
    crossing_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    root, route, segments = crossing_corridor

    result = crossings(route, segments, _ctx(root))

    assert _values(result, segments[7].id)["crossings"] == 0


def test_a_bridge_is_not_an_at_grade_crossing(
    crossing_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    """Two lines meeting in plan view have not met on the ground."""
    root, route, segments = crossing_corridor

    result = crossings(route, segments, _ctx(root))

    assert _values(result, segments[8].id)["crossings"] == 0
    assert _summary(result)["grade_separated"] == 1
    assert "unsignalized_primary_crossing" not in [
        f.reason_code for f in result.flags if f.segment_id == segments[8].id
    ]


def test_a_primary_with_no_posted_speed_is_soft_not_hard(
    crossing_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    """Scope 12 again: an unknown speed is not a speed above the floor."""
    root, route, segments = crossing_corridor

    flag = _flag(
        crossings(route, segments, _ctx(root)), "unsignalized_primary_crossing_unknown_speed"
    )

    assert flag.kind is FlagKind.SOFT
    assert flag.segment_id == segments[9].id


def test_the_route_own_ways_are_not_reported_as_crossings(
    crossing_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    """The route runs along way 1 for its whole length, so it intersects it everywhere."""
    root, route, segments = crossing_corridor

    summary = _summary(crossings(route, segments, _ctx(root)))

    assert summary["crossings"] == 4
    assert summary["crossings_per_km"] == pytest.approx(4 / (route.length_m / 1000.0))


def test_an_absent_node_layer_makes_signalization_unknown_not_absent(tmp_path: Path) -> None:
    """The whole point of scope 12: never having looked is not the same as having looked
    and found nothing, and only the second may produce an unsignalized-crossing flag."""
    route = _route(21)
    way_ids = [1] * 21
    _write_ways(
        tmp_path,
        route,
        way_ids,
        {1: {"highway": "residential"}},
        extra=[(10, {"highway": "primary", "maxspeed": "80"}, _crossing_way(route, 3))],
    )
    segments = _segments(route, way_ids, max_len_m=250.0)

    result = crossings(route, segments, _ctx(tmp_path))

    assert result.flags == []
    assert _summary(result)["crossings"] == 1
    assert _summary(result)["signals_checked"] is False
    assert _values(result, segments[1].id)["unsignalized"] is None
    assert _confidence(result, segments[1].id) < 1.0
    unchecked = [e for e in result.coverage if not e.checked]
    assert [e.kind for e in unchecked] == ["traffic_signals"]


# --- stop density (scope 7.2) -----------------------------------------------


@pytest.fixture
def stops_corridor(tmp_path: Path) -> tuple[Path, Route, list[Segment]]:
    """Four kilometres with five signals packed into the first, one gate at 2.5 km."""
    route = _route(41)
    way_ids = [1] * 41
    _write_ways(tmp_path, route, way_ids, {1: {"highway": "residential"}})
    records = [{"kind": "traffic_signals", "crossing": None} for _ in range(5)]
    geometries = [Point(LON, _lat(route, i)) for i in (1, 3, 5, 7, 9)]
    records.append({"kind": "gate", "crossing": None})
    geometries.append(Point(LON, _lat(route, 25)))
    records.append({"kind": "crossing", "crossing": "unmarked"})
    geometries.append(Point(LON, _lat(route, 27)))
    records.append({"kind": "traffic_signals", "crossing": None})
    geometries.append(Point(LON + 0.0012, _lat(route, 29)))  # ~105 m off the line
    _write_points(tmp_path, records, geometries)
    return tmp_path, route, _segments(route, way_ids, max_len_m=250.0)


def test_signals_and_gates_are_counted_and_nothing_else_is(
    stops_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    """An unmarked crossing does not stop anyone, and a signal a block away is not on the
    route however comfortably it sits inside the 400 m corridor query."""
    root, route, segments = stops_corridor

    summary = _summary(stop_density(route, segments, _ctx(root)))

    assert summary["signals"] == 5
    assert summary["gates"] == 1
    assert summary["stops"] == 6


def test_the_dense_kilometre_is_flagged_and_the_rest_is_not(
    stops_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    root, route, segments = stops_corridor

    result = stop_density(route, segments, _ctx(root))
    flagged = {flag.segment_id for flag in result.flags}

    assert _values(result, segments[0].id)["stops_per_km"] == pytest.approx(5.0)
    assert segments[0].id in flagged
    assert segments[15].id not in flagged
    assert set(_codes(result)) == {"stops_per_km_above_tolerance"}


def test_the_stops_flag_is_soft_and_comfort_tier(
    stops_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    root, route, segments = stops_corridor

    flag = _flag(stop_density(route, segments, _ctx(root)), "stops_per_km_above_tolerance")

    assert flag.kind is FlagKind.SOFT
    assert flag.tier is Tier.COMFORT


def test_raising_stops_tolerance_silences_the_flag(
    stops_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    """Scope 8.3 marks this threshold "(profile)": it is the user's line, not a floor."""
    root, route, segments = stops_corridor
    patient = apply_overrides(load_defaults(), {"stops_tolerance": 6.0})

    assert stop_density(route, segments, _ctx(root, patient)).flags == []


def test_a_gate_counts_as_a_stop(stops_corridor: tuple[Path, Route, list[Segment]]) -> None:
    root, route, segments = stops_corridor
    gate_segment = next(s for s in segments if s.cum_start_m <= 2500.0 < s.cum_end_m)

    result = stop_density(route, segments, _ctx(root))

    assert _values(result, gate_segment.id)["gates"] == 1


@pytest.mark.parametrize(
    ("centre", "expected_span"),
    [(100.0, 1000.0), (2000.0, 1000.0), (3900.0, 1000.0)],
)
def test_the_density_window_slides_inward_rather_than_shrinking(
    centre: float, expected_span: float
) -> None:
    """A truncated window at the route ends would report double density there."""
    density, span = window_density([50.0, 150.0], centre, 4000.0)
    assert span == expected_span
    assert density == pytest.approx(2.0 if centre == 100.0 else 0.0)


def test_a_short_route_uses_its_whole_length_as_the_window() -> None:
    density, span = window_density([100.0], 150.0, 300.0)
    assert span == 300.0
    assert density == pytest.approx(1 / 0.3)


# --- surface (scope 7.2, 12) ------------------------------------------------


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"surface": "asphalt"}, "paved"),
        ({"surface": "paving_stones"}, "paved"),
        ({"surface": "gravel"}, "unpaved"),
        ({"surface": "dirt"}, "unpaved"),
        ({"tracktype": "grade3"}, "unpaved"),
        ({"surface": "brand_new_material"}, None),
        ({"highway": "footway"}, None),
    ],
)
def test_surface_class_table(tags: dict[str, Any], expected: str | None) -> None:
    """An untagged footway is unknown, not paved; a great many of them are dirt."""
    assert surface_class(tags) == expected


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"shoulder": "no"}, False),
        ({"shoulder:both": "none"}, False),
        ({"shoulder": "both"}, True),
        ({"shoulder:right": "yes"}, True),
        ({}, None),
    ],
)
def test_has_shoulder_table(tags: dict[str, Any], expected: bool | None) -> None:
    assert has_shoulder(tags) is expected


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"highway": "secondary", "sidewalk": "no", "shoulder": "no"}, True),
        ({"highway": "secondary", "sidewalk": "no", "shoulder": "both"}, False),
        ({"highway": "secondary", "sidewalk": "both", "shoulder": "no"}, False),
        # Untagged either way: unknown, which is most of rural US OSM (scope 12).
        ({"highway": "secondary", "shoulder": "no"}, None),
        ({"highway": "secondary", "sidewalk": "no"}, None),
        ({"highway": "secondary"}, None),
        # A separated path is not roadway running whatever else is tagged.
        ({"highway": "footway", "sidewalk": "no", "shoulder": "no"}, False),
    ],
)
def test_on_shoulder_table(tags: dict[str, Any], expected: bool | None) -> None:
    assert on_shoulder(tags) is expected


@pytest.fixture
def surface_corridor(tmp_path: Path) -> tuple[Path, Route, list[Segment]]:
    """Three ways: 700 m paved, 700 m gravel, 600 m untagged."""
    route = _route(21)
    way_ids = [1] * 7 + [2] * 7 + [3] * 7
    _write_ways(
        tmp_path,
        route,
        way_ids,
        {
            1: {"highway": "residential", "surface": "asphalt", "sidewalk": "both"},
            2: {"highway": "track", "surface": "gravel"},
            3: {"highway": "residential"},
        },
    )
    return tmp_path, route, _segments(route, way_ids)


def test_surface_fractions_account_for_the_whole_route(
    surface_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    """The unknown fraction is reported as its own number, never folded into paved."""
    root, route, segments = surface_corridor

    summary = _summary(surface_profile(route, segments, _ctx(root)))

    total = sum(s.length_m for s in segments)
    assert summary["paved_fraction"] == pytest.approx(segments[0].length_m / total)
    assert summary["unpaved_fraction"] == pytest.approx(segments[1].length_m / total)
    assert summary["unknown_fraction"] == pytest.approx(segments[2].length_m / total)
    assert summary["paved_fraction"] + summary["unpaved_fraction"] + summary[
        "unknown_fraction"
    ] == pytest.approx(1.0)


def test_an_untagged_surface_lowers_confidence(
    surface_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    root, route, segments = surface_corridor

    result = surface_profile(route, segments, _ctx(root))

    assert _values(result, segments[2].id)["surface_class"] is None
    assert _confidence(result, segments[2].id) == UNKNOWN_SURFACE_CONFIDENCE
    assert _confidence(result, segments[0].id) == 1.0


@pytest.mark.parametrize(
    ("preference", "expected"),
    [("paved", ["sustained_unpaved"]), ("mixed", []), ("dirt", ["sustained_paved"])],
)
def test_sustained_runs_are_flagged_against_the_profile_and_nothing_else(
    surface_corridor: tuple[Path, Route, list[Segment]],
    preference: str,
    expected: list[str],
) -> None:
    """Scope 3.2: the scorer holds no opinion about dirt. `mixed` — the shipped default —
    reports the fractions and flags neither direction."""
    root, route, segments = surface_corridor
    profile = apply_overrides(load_defaults(), {"surface": preference})

    result = surface_profile(route, segments, _ctx(root, profile))

    assert _codes(result) == expected


def test_a_short_unpaved_patch_is_not_a_sustained_run(tmp_path: Path) -> None:
    """200 m of gravel between two paved blocks is not a surface problem."""
    route = _route(21)
    way_ids = [1] * 9 + [2] * 2 + [3] * 10
    _write_ways(
        tmp_path,
        route,
        way_ids,
        {
            1: {"highway": "residential", "surface": "asphalt"},
            2: {"highway": "residential", "surface": "gravel"},
            3: {"highway": "residential", "surface": "asphalt"},
        },
    )
    profile = apply_overrides(load_defaults(), {"surface": "paved"})

    result = surface_profile(route, _segments(route, way_ids), _ctx(tmp_path, profile))

    assert "sustained_unpaved" not in _codes(result)


def test_a_long_run_split_into_many_segments_is_still_one_flag(tmp_path: Path) -> None:
    """One flag per run, not per segment: otherwise finer segmentation would make the
    same gravel look worse in the per-tier weighted sum."""
    route = _route(21)
    way_ids = [1] * 21
    _write_ways(tmp_path, route, way_ids, {1: {"highway": "track", "surface": "gravel"}})
    segments = _segments(route, way_ids, max_len_m=250.0)
    profile = apply_overrides(load_defaults(), {"surface": "paved"})

    result = surface_profile(route, segments, _ctx(tmp_path, profile))

    assert len(segments) > 5
    assert _codes(result) == ["sustained_unpaved"]
    assert _flag(result, "sustained_unpaved").segment_id == segments[0].id
    assert _flag(result, "sustained_unpaved").severity == 1.0


def test_sustained_shoulder_running_is_flagged_where_it_is_taggable(tmp_path: Path) -> None:
    route = _route(11)
    way_ids = [1] * 11
    _write_ways(
        tmp_path,
        route,
        way_ids,
        {1: {"highway": "secondary", "sidewalk": "no", "shoulder": "no", "surface": "asphalt"}},
    )

    result = surface_profile(route, _segments(route, way_ids), _ctx(tmp_path))
    flag = _flag(result, "sustained_shoulder_running")

    assert flag.kind is FlagKind.SOFT
    assert flag.tier is Tier.COMFORT


def test_an_untagged_rural_road_is_not_shoulder_running(tmp_path: Path) -> None:
    """Reading silence as "no sidewalk, no shoulder" would flag most of the country."""
    route = _route(11)
    way_ids = [1] * 11
    _write_ways(tmp_path, route, way_ids, {1: {"highway": "secondary", "surface": "asphalt"}})
    segments = _segments(route, way_ids)

    result = surface_profile(route, segments, _ctx(tmp_path))

    assert result.flags == []
    assert _values(result, segments[0].id)["on_shoulder"] is None


# --- services (scope 7.5) ---------------------------------------------------


@pytest.mark.parametrize(
    ("positions", "length", "expected"),
    [
        ([], 4000.0, 4000.0),
        ([500.0], 4000.0, 3500.0),
        ([500.0, 3000.0], 4000.0, 2500.0),
        ([3900.0], 4000.0, 3900.0),
    ],
)
def test_max_gap_table(positions: list[float], length: float, expected: float) -> None:
    """The run to the first fountain and the run home from the last one are both gaps."""
    assert max_gap_m(positions, length) == pytest.approx(expected)


@pytest.fixture
def services_corridor(tmp_path: Path) -> tuple[Path, Route, list[Segment]]:
    """Water at ~0.5 km and ~3 km, a toilet at ~1 km, no food, a park over 1-2 km."""
    route = _route(41)
    way_ids = [1] * 41
    _write_ways(tmp_path, route, way_ids, {1: {"highway": "residential"}})
    _write_points(
        tmp_path,
        [
            {"kind": "drinking_water"},
            {"kind": "drinking_water"},
            {"kind": "toilets"},
            {"kind": "drinking_water"},
        ],
        [
            Point(LON, _lat(route, 5)),
            Point(LON, _lat(route, 30)),
            Point(LON, _lat(route, 10)),
            Point(LON + 0.0035, _lat(route, 20)),  # ~308 m off the line
        ],
    )
    _write(
        tmp_path / "parks.gpkg",
        [{"name": "Test Park"}],
        [box(LON - 0.001, _lat(route, 10), LON + 0.001, _lat(route, 20))],
    )
    return tmp_path, route, _segments(route, way_ids, max_len_m=250.0)


def test_distance_to_the_next_service_is_measured_along_the_route(
    services_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    root, route, segments = services_corridor

    result = services_along(route, segments, _ctx(root))

    assert _values(result, segments[0].id)["dist_to_water_m"] == pytest.approx(
        route.points[5].cum_dist_m, abs=5.0
    )
    assert _values(result, segments[0].id)["dist_to_toilet_m"] == pytest.approx(
        route.points[10].cum_dist_m, abs=5.0
    )


def test_a_route_with_no_food_reports_the_whole_route_as_the_gap(
    services_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    """An empty category is a real answer, not a missing one."""
    root, route, segments = services_corridor

    summary = _summary(services_along(route, segments, _ctx(root)))

    assert summary["food_count"] == 0
    assert summary["max_food_gap_m"] == pytest.approx(route.length_m)
    assert (
        _values(services_along(route, segments, _ctx(root)), segments[0].id)["dist_to_food_m"]
        is None
    )


def test_the_largest_water_gap_is_the_one_between_the_two_fountains(
    services_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    root, route, segments = services_corridor
    expected = route.points[30].cum_dist_m - route.points[5].cum_dist_m

    summary = _summary(services_along(route, segments, _ctx(root)))

    assert summary["water_count"] == 2
    assert summary["max_water_gap_m"] == pytest.approx(expected, abs=5.0)


def test_a_service_beyond_the_buffer_is_not_on_the_route(
    services_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    """The third fountain is 300 m off the line: inside the corridor query, outside the
    200 m buffer, and a 600 m round trip nobody runs for water."""
    root, route, segments = services_corridor

    wide = _summary(services_along(route, segments, _ctx(root), buffer_m=400.0))
    narrow = _summary(services_along(route, segments, _ctx(root)))

    assert narrow["water_count"] == 2
    assert wide["water_count"] == 3


def test_park_containment_is_reported_per_segment(
    services_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    root, route, segments = services_corridor
    inside = next(s for s in segments if s.cum_start_m > route.points[11].cum_dist_m)

    result = services_along(route, segments, _ctx(root))

    assert _values(result, segments[0].id)["in_park"] is False
    assert _values(result, inside.id)["in_park"] is True


def test_an_absent_park_layer_degrades_one_value_not_the_scorer(tmp_path: Path) -> None:
    """Scope 3.6: water and toilets keep being reported; the park answer becomes unknown
    and says so in the manifest."""
    route = _route(41)
    way_ids = [1] * 41
    _write_points(tmp_path, [{"kind": "drinking_water"}], [Point(LON, _lat(route, 5))])
    segments = _segments(route, way_ids, max_len_m=250.0)
    ctx = _ctx(tmp_path)

    result = services_along(route, segments, ctx)

    assert _summary(result)["water_count"] == 1
    assert _summary(result)["parks_checked"] is False
    assert _values(result, segments[0].id)["in_park"] is None
    assert _confidence(result, segments[0].id) < 1.0
    assert [e.kind for e in result.coverage if not e.checked] == ["park_boundaries"]
    assert [e.kind for e in result.coverage if e.checked] == ["services"]


def test_services_along_never_flags(
    services_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    """Every services threshold in scope 8.3 is stated in minutes and belongs to
    `resupply_schedule`. Converting metres to minutes here would invent the pace that is
    the missing input, so this scorer measures and stops."""
    root, route, segments = services_corridor

    assert services_along(route, segments, _ctx(root)).flags == []


# --- cross-scorer invariants ------------------------------------------------


@pytest.fixture
def full_corridor(tmp_path: Path) -> tuple[Path, Route, list[Segment]]:
    """One 2 km route that trips something in every scorer at once."""
    route = _route(21)
    way_ids = [1] * 7 + [2] * 7 + [3] * 7
    _write_ways(
        tmp_path,
        route,
        way_ids,
        {
            1: {"highway": "primary", "maxspeed": "70", "sidewalk": "no", "lanes": "4"},
            2: {"highway": "motorway", "maxspeed": "110"},
            3: {"highway": "track", "surface": "gravel"},
        },
        extra=[(10, {"highway": "primary", "maxspeed": "80"}, _crossing_way(route, 3))],
    )
    _write_points(
        tmp_path,
        [{"kind": "traffic_signals"}, {"kind": "drinking_water"}, {"kind": "gate"}],
        [Point(LON, _lat(route, 17)), Point(LON, _lat(route, 5)), Point(LON, _lat(route, 9))],
    )
    _write(
        tmp_path / "parks.gpkg",
        [{"name": "Test Park"}],
        [box(LON - 0.001, _lat(route, 14), LON + 0.001, _lat(route, 20))],
    )
    return tmp_path, route, _segments(route, way_ids)


def _run_all(root: Path, route: Route, segments: list[Segment]) -> list[ScorerResult]:
    profile = apply_overrides(load_defaults(), {"surface": "paved"})
    ctx = _ctx(root, profile)
    return [getattr(m, m.name)(route, segments, ctx) for m in SCORER_MODULES]


def test_every_reason_code_is_machine_stable(
    full_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    """Tests and golden files assert on codes; prose belongs in `detail`."""
    root, route, segments = full_corridor
    pattern = re.compile(r"^[a-z][a-z0-9_]*$")

    flags = [flag for result in _run_all(root, route, segments) for flag in result.flags]

    assert flags
    assert all(pattern.match(flag.reason_code) for flag in flags)


def test_every_flag_names_its_scorer_and_a_real_segment(
    full_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    """`core.plan.arbitrate` looks segments up by id and weights by scorer name; a flag
    that names neither is silently unweighted and unrankable."""
    root, route, segments = full_corridor
    ids = {segment.id for segment in segments}

    results = _run_all(root, route, segments)

    for result in results:
        for flag in result.flags:
            assert flag.scorer == result.name
            assert flag.segment_id in ids


def test_each_scorer_covers_every_segment_plus_one_route_summary(
    full_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    """A segment missing from the measurements is indistinguishable, downstream, from a
    segment with nothing to report."""
    root, route, segments = full_corridor

    for result in _run_all(root, route, segments):
        measured = [m.segment_id for m in result.measurements]
        assert sorted(measured) == sorted([s.id for s in segments] + [ROUTE_SUMMARY_ID])


def test_hard_flags_are_safety_tier_and_soft_flags_are_not(
    full_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    """Scope 8.4's lexicographic order only works if the tier assignment is consistent:
    every hard flag these scorers raise is a safety floor, and no soft one outranks it."""
    root, route, segments = full_corridor

    flags = [flag for result in _run_all(root, route, segments) for flag in result.flags]
    hard = [flag for flag in flags if flag.kind is FlagKind.HARD]
    soft = [flag for flag in flags if flag.kind is FlagKind.SOFT]

    assert {flag.reason_code for flag in hard} == {
        "lts_4",
        "motorway",
        "unsignalized_primary_crossing",
    }
    assert all(flag.tier is Tier.SAFETY for flag in hard)
    assert soft and all(flag.tier is not Tier.SAFETY for flag in soft)


def test_every_scorer_reports_what_it_consulted(
    full_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    """Scope 3.6: a scorer that returns no coverage has told the plan sheet nothing."""
    root, route, segments = full_corridor

    for result in _run_all(root, route, segments):
        assert result.coverage
        assert all(entry.checked for entry in result.coverage)


def test_worst_orders_hard_safety_flags_first(
    full_corridor: tuple[Path, Route, list[Segment]],
) -> None:
    """The worst-N list is the product (scope 3.4), so its head must be the hard flags."""
    root, route, segments = full_corridor

    results = _run_all(root, route, segments)
    worst = [flag for result in results for flag in result.worst(1)]

    assert any(flag.kind is FlagKind.HARD for flag in worst)
    for result in results:
        ordered = result.worst(10)
        assert [f.kind for f in ordered] == sorted([f.kind for f in ordered], key=lambda k: -int(k))
