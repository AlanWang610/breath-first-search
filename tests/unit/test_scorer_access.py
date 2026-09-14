"""The three adapter-fed scorers (scope 7.6, 7.10; ADR 0013).

Every feature here is hand-built, so what is tested is the *rule* rather than any feed's
behaviour - which is the split the contract tests exist for. The rules worth this much
attention are the five gates between a work zone and a hard flag, because check 6 fails a
route on one and the data that would trip it is real: Maricopa County's live feed carries
115 work zones, most of them lane closures on highways no pedestrian is on, and one of them
runs from 2024 to 2028.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest

from longrun.core.data.cache import SqliteCache
from longrun.core.data.file_store import FileLayerStore, FileRasterStore
from longrun.core.geo.segments import segment_route
from longrun.core.models.context import Budget, FrozenClock, ScorerContext
from longrun.core.models.coverage import CoverageManifest
from longrun.core.models.features import Feature, FeatureSet, JurisdictionAnswer
from longrun.core.models.geometry import Route
from longrun.core.models.measurement import FlagKind, Tier
from longrun.core.models.profile import PreferenceProfile
from longrun.core.scorers import access_hours as ah
from longrun.core.scorers import closures as cl
from longrun.core.scorers import trail_status as ts

START = datetime(2026, 9, 15, 7, 0)

# An east-west route along a line of latitude, so "parallel" is due east and "across" is
# due north - the geometry stays checkable by eye.
LAT = 37.7955


def _route(points: int = 40, span_deg: float = 0.02) -> Route:
    from longrun.core.geo.gpx import normalize

    return Route(
        id="r",
        points=normalize(
            [(LAT, -122.40 + i * span_deg / (points - 1), None) for i in range(points)]
        ),
    )


def _feature(**kwargs: Any) -> Feature:
    base: dict[str, Any] = {
        "kind": "closures",
        "category": "all-lanes-closed",
        "geometry": {},
        "tier": 1,
        "confidence": 0.95,
        "start": START - timedelta(days=1),
        "end": START + timedelta(days=1),
    }
    return Feature(**{**base, **kwargs})


def _along(lon0: float = -122.395, lon1: float = -122.392) -> dict[str, Any]:
    """A line running along the route, at its latitude."""
    return {"type": "LineString", "coordinates": [[lon0, LAT], [lon1, LAT]]}


def _across(lon: float = -122.395) -> dict[str, Any]:
    """A cross-street: same place, ninety degrees off."""
    return {"type": "LineString", "coordinates": [[lon, LAT - 0.002], [lon, LAT + 0.002]]}


class StubSource:
    """A `FeatureSource` that returns what the test hands it."""

    def __init__(self, features: list[Feature], answers: list[JurisdictionAnswer] | None = None):
        self._features = features
        self._answers = answers

    def fetch(self, kind: Any, jurisdictions: Any, polygon: Any, day: date) -> FeatureSet:
        answers = self._answers or [
            JurisdictionAnswer(
                jurisdiction=j.id, name=j.name, kind=kind, checked=True, tier=1, adapter="stub"
            )
            for j in jurisdictions
        ]
        return FeatureSet.from_answers([f for f in self._features if f.kind == kind], answers)

    def adapters_for(self, kind: Any, jurisdiction: Any) -> list[Any]:
        return []


@pytest.fixture
def ctx_factory(tmp_path: Any) -> Any:
    """A context over a fixture directory the test writes boundaries and parks into."""

    def build(features: list[Feature] | None = None, *, layers: Any = None) -> ScorerContext:
        with SqliteCache() as cache:
            return ScorerContext(
                layers=layers if layers is not None else FileLayerStore(tmp_path),
                rasters=FileRasterStore(tmp_path),
                cache=cache,
                clock=FrozenClock(START),
                coverage=CoverageManifest(),
                profile=PreferenceProfile(),
                budget=Budget(),
                features=StubSource(features) if features is not None else None,
            )

    return build


def _write_boundaries(directory: Any) -> None:
    import json

    directory.mkdir(parents=True, exist_ok=True)
    (directory / "boundaries.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "geoid": geoid,
                            "level": level,
                            "name": label,
                            "statefp": "06",
                        },
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [-122.50, 37.70],
                                    [-122.30, 37.70],
                                    [-122.30, 37.85],
                                    [-122.50, 37.85],
                                    [-122.50, 37.70],
                                ]
                            ],
                        },
                    }
                    # Two, so "partial coverage" has something to be partial about: a county
                    # and the city inside it is the ordinary shape of a US street route.
                    for geoid, level, label in (
                        ("06075", "county", "San Francisco County"),
                        ("0667000", "place", "San Francisco"),
                    )
                ],
            }
        ),
        encoding="utf-8",
    )


# --- ADR 0013 gate 1: pedestrian passage ------------------------------------


def test_all_lanes_closed_blocks_pedestrians() -> None:
    assert cl.blocks_pedestrians(_feature(category="all-lanes-closed"))


def test_some_lanes_closed_does_not() -> None:
    """Most of Maricopa's 115 work zones. A rule that treated these as impassable would
    hard-flag almost every Phoenix route, which is indistinguishable from flagging none."""
    assert not cl.blocks_pedestrians(_feature(category="some-lanes-closed"))


def test_a_sidewalk_closure_described_in_prose_is_caught() -> None:
    """WZDx has no pedestrian-impact field, so a feed that closes a footway says so in the
    description or not at all."""
    assert cl.blocks_pedestrians(
        _feature(category="some-lanes-closed", detail="Sidewalk closed, use other side")
    )


# --- ADR 0013 gate 2: along the route, not across it ------------------------


def test_a_closure_running_along_the_route_is_parallel() -> None:
    route = _route()
    frame = cl.RouteFrame(route)
    parallel, offset_m, _ = cl.runs_along(_feature(geometry=_along()), frame, route)
    assert parallel and offset_m < cl.CLOSURE_BUFFER_M


def test_a_cross_street_closure_is_not_parallel() -> None:
    """The gate doing the most work. You can get across a work zone on a cross-street; you
    cannot get through four hundred metres of closed sidewalk."""
    route = _route()
    frame = cl.RouteFrame(route)
    parallel, offset_m, _ = cl.runs_along(_feature(geometry=_across()), frame, route)
    assert not parallel
    assert offset_m < cl.CLOSURE_BUFFER_M, "it does touch the route - that is the point"


def test_a_closure_digitised_backwards_is_still_parallel() -> None:
    """A line is undirected. A 180-degree bearing difference is the same piece of ground,
    and treating it as perpendicular would silently drop half of every feed."""
    route = _route()
    frame = cl.RouteFrame(route)
    backwards = _feature(geometry=_along(lon0=-122.392, lon1=-122.395))
    assert cl.runs_along(backwards, frame, route)[0]


def test_a_closure_far_from_the_route_is_not_on_it() -> None:
    route = _route()
    frame = cl.RouteFrame(route)
    far = _feature(
        geometry={"type": "LineString", "coordinates": [[-122.395, 37.85], [-122.39, 37.85]]}
    )
    parallel, offset_m, _ = cl.runs_along(far, frame, route)
    assert not parallel and offset_m > 1000


def test_a_single_point_closure_is_placed_but_never_parallel() -> None:
    """A point has no bearing. Counting it parallel is how one reported incident would shut
    a route down."""
    route = _route()
    frame = cl.RouteFrame(route)
    point = _feature(geometry={"type": "Point", "coordinates": [-122.395, LAT]})
    parallel, offset_m, _ = cl.runs_along(point, frame, route)
    assert not parallel and offset_m < cl.CLOSURE_BUFFER_M


def test_geometry_type_does_not_matter() -> None:
    """A feed that switches LineString for MultiLineString between releases must not go
    silent - which is what a type-switch on `geometry["type"]` would do."""
    route = _route()
    frame = cl.RouteFrame(route)
    multi = _feature(
        geometry={"type": "MultiLineString", "coordinates": [[[-122.395, LAT], [-122.392, LAT]]]}
    )
    assert cl.runs_along(multi, frame, route)[0]


# --- ADR 0013 gate 4: standing conditions -----------------------------------


def test_a_four_year_work_zone_is_a_standing_condition() -> None:
    """Maricopa's feed carries one running 2024-10-22 to 2028-06-16. It is either stale or a
    permanent reconfiguration OSM has already absorbed; either way it is not an event."""
    assert cl.is_standing_condition(
        _feature(start=datetime(2024, 10, 22), end=datetime(2028, 6, 16))
    )


def test_a_two_week_closure_is_an_event() -> None:
    assert not cl.is_standing_condition(
        _feature(start=START - timedelta(days=2), end=START + timedelta(days=12))
    )


def test_an_open_ended_closure_counts_as_standing() -> None:
    """No end date is a statement that nobody knows when it lifts."""
    assert cl.is_standing_condition(_feature(start=START, end=None))


# --- ADR 0013 gate 5, and the arithmetic that makes tier 4 safe -------------


def test_a_tier_four_extraction_can_never_hard_flag() -> None:
    """The load-bearing consequence. `MAX_EXTRACTION_CONFIDENCE` is 0.5 and
    `HARD_FLAG_CONFIDENCE` is 0.8, so this holds by arithmetic rather than by care: a
    verification gate trippable by a model reading a PDF is worse than no gate."""
    from longrun.core.models.features import MAX_EXTRACTION_CONFIDENCE

    assert MAX_EXTRACTION_CONFIDENCE < cl.HARD_FLAG_CONFIDENCE
    best_possible = _feature(tier=4, confidence=MAX_EXTRACTION_CONFIDENCE, geometry=_along())
    assert not cl.is_hard(best_possible, parallel=True, when=START)


def test_a_tier_three_portal_can_never_hard_flag() -> None:
    assert not cl.is_hard(
        _feature(tier=3, confidence=0.99, geometry=_along()), parallel=True, when=START
    )


def test_a_confident_tier_one_feed_can() -> None:
    assert cl.is_hard(_feature(geometry=_along()), parallel=True, when=START)


def test_a_low_confidence_tier_one_feed_cannot() -> None:
    assert not cl.is_hard(_feature(confidence=0.6, geometry=_along()), parallel=True, when=START)


# --- the scorer end to end ---------------------------------------------------


def test_a_closure_along_the_route_hard_flags(ctx_factory: Any, tmp_path: Any) -> None:
    _write_boundaries(tmp_path)
    route = _route()
    segments = segment_route(route)
    ctx = ctx_factory([_feature(geometry=_along())])
    result = cl.closures(route, segments, ctx, [START] * len(segments))
    hard = [f for f in result.flags if f.kind is FlagKind.HARD]
    assert len(hard) == 1
    assert hard[0].tier is Tier.SAFETY and hard[0].severity == 1.0
    assert hard[0].reason_code == "closed_to_pedestrians"


def test_a_cross_street_closure_is_soft(ctx_factory: Any, tmp_path: Any) -> None:
    _write_boundaries(tmp_path)
    route = _route()
    segments = segment_route(route)
    ctx = ctx_factory([_feature(geometry=_across())])
    result = cl.closures(route, segments, ctx, [START] * len(segments))
    assert [f.kind for f in result.flags] == [FlagKind.SOFT]
    assert result.flags[0].tier is Tier.COMFORT


def test_confidence_scales_soft_severity(ctx_factory: Any, tmp_path: Any) -> None:
    """The only channel confidence has: `arbitrate` reads no confidence field, so a tier-4
    extraction and a tier-1 feed would otherwise rank identically."""
    _write_boundaries(tmp_path)
    route = _route()
    segments = segment_route(route)
    low = ctx_factory([_feature(geometry=_across(), tier=4, confidence=0.4)])
    high = ctx_factory([_feature(geometry=_across(), tier=1, confidence=0.95)])
    a = cl.closures(route, segments, low, [START] * len(segments)).flags[0]
    b = cl.closures(route, segments, high, [START] * len(segments)).flags[0]
    assert a.severity < b.severity


def test_a_closure_that_has_lifted_by_arrival_does_not_hard_flag(
    ctx_factory: Any, tmp_path: Any
) -> None:
    """ADR 0013 gate 3, and the reason it is the segment's ETA rather than the route's date:
    a closure lifting at noon does not block a runner who reaches it at 14:00."""
    _write_boundaries(tmp_path)
    route = _route()
    segments = segment_route(route)
    lifted = _feature(
        geometry=_along(), start=START - timedelta(days=2), end=START - timedelta(hours=1)
    )
    result = cl.closures(route, segments, ctx_factory([lifted]), [START] * len(segments))
    assert not [f for f in result.flags if f.kind is FlagKind.HARD]


def test_coverage_is_recorded_per_jurisdiction(ctx_factory: Any, tmp_path: Any) -> None:
    """What lets check 6 tell a route where every jurisdiction answered from one where two
    of four did - and the first use of `CoverageEntry.jurisdiction`, which has existed
    since M1 and been passed by nothing."""
    _write_boundaries(tmp_path)
    route = _route()
    segments = segment_route(route)
    result = cl.closures(route, segments, ctx_factory([]), [START] * len(segments))
    jurisdictional = [c for c in result.coverage if c.jurisdiction]
    assert jurisdictional, "the county the route crosses has to appear by name"
    assert all(c.kind == "closures" for c in result.coverage)


def test_no_registry_is_not_a_clean_sheet(ctx_factory: Any, tmp_path: Any) -> None:
    """The failure this milestone exists to prevent."""
    _write_boundaries(tmp_path)
    route = _route()
    segments = segment_route(route)
    result = cl.closures(route, segments, ctx_factory(None), [START] * len(segments))
    assert not result.flags
    assert result.coverage and not any(c.checked for c in result.coverage)


def test_unresolved_boundaries_are_reported_even_when_parks_resolved(
    ctx_factory: Any, tmp_path: Any
) -> None:
    """Street closures are published by DOTs, which register against census boundaries. A
    route that resolved its park agencies and not its boundaries has established almost
    nothing about closures, and five park entries must not imply otherwise."""
    route = _route()
    segments = segment_route(route)  # tmp_path has no boundaries.geojson
    result = cl.closures(route, segments, ctx_factory([]), [START] * len(segments))
    assert any("boundaries" in (c.reason or "") for c in result.coverage)


# --- trail_status -----------------------------------------------------------


def test_trail_alerts_are_soft_and_comfort(ctx_factory: Any, tmp_path: Any) -> None:
    """ADR 0013: a park alert is prose - "muddy", "bridge out", "lion sighted" - and sorting
    the disqualifying from the merely wet is exactly what scope 3.2 forbids a scorer."""
    alert = Feature(
        kind="trail_status",
        category="Closure",
        geometry={},
        tier=3,
        confidence=0.5,
        detail="Trail closed for bridge repair",
    )
    route = _route()
    segments = segment_route(route)
    ctx = ctx_factory([alert])
    # No parks layer, so no agency resolves and nothing is asked - the honest path.
    result = ts.trail_status(route, segments, ctx, [START] * len(segments))
    assert not [f for f in result.flags if f.kind is FlagKind.HARD]


def test_a_serious_category_outranks_an_advisory() -> None:
    serious = Feature(kind="trail_status", category="closure", geometry={}, tier=1, confidence=1.0)
    notice = Feature(
        kind="trail_status", category="information", geometry={}, tier=1, confidence=1.0
    )
    assert ts.severity_for(serious) > ts.severity_for(notice)


def test_trail_status_never_produces_a_hard_flag_at_any_confidence() -> None:
    """There is no check 11, so a hard flag here would have no route back to rerouting -
    which is what ADR 0013 defines a hard flag to be."""
    import inspect

    source = inspect.getsource(ts)
    assert "FlagKind.HARD" not in source


def test_access_hours_never_produces_a_hard_flag_at_any_confidence() -> None:
    import inspect

    source = inspect.getsource(ah)
    assert "FlagKind.HARD" not in source


# --- access_hours -----------------------------------------------------------


def test_a_gate_shut_at_arrival_proposes_a_start_shift() -> None:
    """Scope 6.4 files "earliest start (e.g. gate opens)" as a request constraint, and 8.1
    step 7's `start_time_optimizer` is the machinery. A locked gate is bought back by
    starting later, which is why it is soft rather than a reroute demand."""
    gate = Feature(
        kind="access_hours",
        category="gate",
        geometry={},
        tier=1,
        confidence=1.0,
        start=START + timedelta(minutes=90),
        end=START + timedelta(hours=12),
    )
    assert ah.shift_to_open(gate, START) == pytest.approx(90.0)


def test_a_gate_already_open_needs_no_shift() -> None:
    gate = Feature(
        kind="access_hours",
        category="gate",
        geometry={},
        tier=1,
        confidence=1.0,
        start=START - timedelta(hours=1),
        end=START + timedelta(hours=12),
    )
    assert ah.shift_to_open(gate, START) is None


def test_a_gate_that_has_already_shut_cannot_be_opened_by_starting_later() -> None:
    """Starting later makes it worse, so there is no shift to propose - and reporting one
    would send `start_time_optimizer` in the wrong direction."""
    gate = Feature(
        kind="access_hours",
        category="gate",
        geometry={},
        tier=1,
        confidence=1.0,
        start=START - timedelta(hours=12),
        end=START - timedelta(hours=1),
    )
    assert ah.shift_to_open(gate, START) is None


# --- check 6: fail > skip > pass (ADR 0013) ---------------------------------


def _check6(result: Any) -> Any:
    from longrun.core.verify import checks
    from longrun.core.verify.runner import _offenders_from, _unanswered

    return checks.check_6_no_active_closures(
        _offenders_from([result], "closures"), _unanswered([result], "closures", "closures")
    )


class PartialSource:
    """A registry where the first jurisdiction answers and the rest do not - which is what
    a real state-line route looks like for years to come."""

    def __init__(self, features: list[Feature]) -> None:
        self._features = features

    def fetch(self, kind: Any, jurisdictions: Any, polygon: Any, day: date) -> FeatureSet:
        answers = [
            JurisdictionAnswer(
                jurisdiction=j.id,
                name=j.name,
                kind=kind,
                checked=(i == 0),
                tier=1 if i == 0 else None,
                reason=None if i == 0 else "no adapter for this jurisdiction",
            )
            for i, j in enumerate(jurisdictions)
        ]
        return FeatureSet.from_answers([f for f in self._features if f.kind == kind], answers)

    def adapters_for(self, kind: Any, jurisdiction: Any) -> list[Any]:
        return []


def _ctx_with(source: Any, tmp_path: Any) -> ScorerContext:
    with SqliteCache() as cache:
        return ScorerContext(
            layers=FileLayerStore(tmp_path),
            rasters=FileRasterStore(tmp_path),
            cache=cache,
            clock=FrozenClock(START),
            coverage=CoverageManifest(),
            profile=PreferenceProfile(),
            budget=Budget(),
            features=source,
        )


def test_a_feed_read_with_nothing_in_it_clears_the_route(ctx_factory: Any, tmp_path: Any) -> None:
    """The one case where check 6 may pass: every jurisdiction answered and none had a
    closure. That is evidence, and reporting it as a pass is the honest thing."""
    _write_boundaries(tmp_path)
    route = _route()
    segments = segment_route(route)
    result = cl.closures(route, segments, ctx_factory([]), [START] * len(segments))
    assert _check6(result).status == "passed"


def test_partial_coverage_skips_and_names_who_was_missed(tmp_path: Any) -> None:
    """Two of four jurisdictions answering is not a cleared route. Reporting `passed` here
    is the vacuous pass ADR 0013 exists to prevent."""
    _write_boundaries(tmp_path)
    route = _route()
    segments = segment_route(route)
    ctx = _ctx_with(PartialSource([]), tmp_path)
    outcome = _check6(cl.closures(route, segments, ctx, [START] * len(segments)))
    assert outcome.status == "skipped"
    assert "San Francisco" in (outcome.detail or ""), outcome.detail


def test_a_closure_found_under_partial_coverage_still_fails(tmp_path: Any) -> None:
    """Fail beats skip. A closure found in a jurisdiction that answered is a fact whatever
    another jurisdiction's silence says - which is why `requires=` alone is the wrong
    mechanism here: it would throw the finding away."""
    _write_boundaries(tmp_path)
    route = _route()
    segments = segment_route(route)
    ctx = _ctx_with(PartialSource([_feature(geometry=_along())]), tmp_path)
    outcome = _check6(cl.closures(route, segments, ctx, [START] * len(segments)))
    assert outcome.status == "failed" and outcome.offenders


def test_nobody_answering_skips_rather_than_passing(tmp_path: Any) -> None:
    _write_boundaries(tmp_path)
    route = _route()
    segments = segment_route(route)
    ctx = _ctx_with(StubSource([], answers=[]), tmp_path)
    ctx = _ctx_with(
        type(
            "Silent",
            (),
            {
                "fetch": lambda self, kind, js, poly, day: FeatureSet.from_answers(
                    [],
                    [
                        JurisdictionAnswer(
                            jurisdiction=j.id, name=j.name, kind=kind, reason="no adapter"
                        )
                        for j in js
                    ],
                ),
                "adapters_for": lambda self, kind, j: [],
            },
        )(),
        tmp_path,
    )
    assert _check6(cl.closures(route, segments, ctx, [START] * len(segments))).status == "skipped"


def test_the_jurisdiction_tally_counts_jurisdictions_not_coverage_lines() -> None:
    """`answered + unanswered` must equal `crossed`, and the coverage list is not a proxy.

    The same list carries statements *about* the scan - boundaries unavailable, no registry
    configured, a park agency with no resolvable state - and counting those made the three
    numbers disagree. Arithmetic that does not close is the cheapest available signal that a
    count is measuring the wrong thing, so it is asserted rather than eyeballed.
    """
    from longrun.core.models.coverage import CoverageEntry
    from longrun.core.models.measurement import ScorerResult
    from longrun.core.scorers.closures import _summarise

    result = ScorerResult(name="closures")
    result.coverage.extend(
        [
            CoverageEntry(source="closures", kind="closures", checked=False, reason="about"),
            CoverageEntry(
                source="closures", kind="closures", checked=True, jurisdiction="tiger:state:29"
            ),
            CoverageEntry(
                source="closures", kind="closures", checked=False, jurisdiction="padus:CITY"
            ),
        ]
    )
    summary = _summarise(result, [], {}, jurisdictions=2)
    values = next(m.values for m in summary.measurements if m.segment_id == "route")
    assert values["jurisdictions_answered"] == 1
    assert values["jurisdictions_unanswered"] == 1
    assert (
        values["jurisdictions_answered"] + values["jurisdictions_unanswered"]
        == values["jurisdictions_crossed"]
    )
