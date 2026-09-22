"""A partial re-score across a geometry edit (M11.7), on M11.3's re-keying.

Scope 10.3: "click a flagged segment -> choose an alternative … **each action re-runs only
the affected scorers**". That is a partial pass over a line that has *moved*, which is the
one case `run_scorers` could not do honestly before M11.3: it merged carried results by
scorer name, and `segment_id` is positional, so every carried measurement downstream of the
edit silently re-pointed at different ground.

The headline test is the one the milestone exists for and the one nothing checked: edit a
route, re-score part of it, and assert the carried measurements describe the same ground a
full re-score of the same edited line describes. It carries a sabotage half - the fixture
has to actually renumber, or the comparison is vacuous.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from longrun.core.data.cache import SqliteCache
from longrun.core.data.file_store import FileLayerStore, FileRasterStore
from longrun.core.geo.gpx import normalize
from longrun.core.models.context import Budget, FrozenClock, ScorerContext
from longrun.core.models.coverage import CoverageManifest
from longrun.core.models.geometry import Route
from longrun.core.models.measurement import ScorerResult, SegmentMeasurement
from longrun.core.models.plan import Manifest, Plan, SnapshotPins
from longrun.core.models.request import PlanRequest
from longrun.core.plan.edits import splice
from longrun.core.plan.pipeline import build_plan, score_once
from longrun.core.plan.refresh import rescore_plan
from longrun.core.preferences.store import load_defaults
from longrun.core.scorers import registry

MADE_AT = datetime(2026, 3, 15, 7, 30)
STEP_DEG = 0.00114
BASE_LAT = 37.7749
BASE_LON = -122.4194


#: Per-point terrain heights, supplied rather than read: there is no DEM under `tmp_path`,
#: and a stored plan with no profile cannot show that an edit preserved one.
ELEVATIONS: list[float | None] = [100.0 + index for index in range(21)]


def _route() -> Route:
    """A line whose `cum_dist_m` is accumulated from its own geometry, as a real one is.

    Built through `normalize` rather than with round numbers, and that is not tidiness: a
    fixture whose distances disagree with its coordinates makes `splice` - which
    renormalizes - shift every distance on the line by the difference, and the comparison
    below would then be measuring the fixture's arithmetic rather than the re-keying.
    """
    return Route(
        id="edited-plan",
        points=normalize(
            [
                (BASE_LAT, BASE_LON + index * STEP_DEG, ELEVATIONS[index])
                for index in range(len(ELEVATIONS))
            ]
        ),
    )


def _detour() -> Route:
    """Six points where five were, bulging north - so the tail is renumbered *and* moved."""
    return Route(
        id="alternative",
        points=normalize(
            [(BASE_LAT + 0.0018, BASE_LON + (8 + index) * STEP_DEG, None) for index in range(6)]
        ),
    )


def _edited(original: Route) -> Route:
    return splice(original, 800.0, 1300.0, _detour())


def _ctx(root: Path, at: datetime = MADE_AT) -> ScorerContext:
    return ScorerContext(
        layers=FileLayerStore(root),
        rasters=FileRasterStore(root),
        cache=SqliteCache(offline=True),
        clock=FrozenClock(at),
        coverage=CoverageManifest(),
        profile=load_defaults(),
        budget=Budget(),
    )


def _request() -> PlanRequest:
    return PlanRequest(mode="repair", date=MADE_AT.date(), start_time=MADE_AT.time())


# --- the headline ------------------------------------------------------------


def _ground_scorer(name: str) -> Any:
    """A scorer whose every value is a function of the ground the segment covers.

    That is what makes "landed on the same ground" checkable: two measurements agree exactly
    when they were taken over the same stretch of line, so a re-pointed measurement shows up
    as a value mismatch rather than as nothing at all.
    """

    def score(route: Route, segments: list[Any], ctx: Any, etas: Any) -> ScorerResult:
        return ScorerResult(
            name=name,
            measurements=[
                SegmentMeasurement(
                    segment_id=segment.id,
                    values={
                        "at_m": round(segment.cum_start_m, 3),
                        "lat": round(route.points[segment.start_idx].lat, 7),
                        "lon": round(route.points[segment.start_idx].lon, 7),
                    },
                )
                for segment in segments
            ],
        )

    return score


@pytest.fixture
def two_stub_scorers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "SCORERS", {"alpha": "stub.alpha", "beta": "stub.beta"})
    monkeypatch.setattr(registry, "PRIOR_DEPENDENCIES", {})
    monkeypatch.setattr(
        registry, "load_scorer", lambda path: _ground_scorer(path.rsplit(".", 1)[-1])
    )


def _by_id(results: list[ScorerResult], name: str) -> dict[str, dict[str, Any]]:
    result = next(r for r in results if r.name == name)
    return {m.segment_id: dict(m.values) for m in result.measurements}


def test_a_carried_measurement_lands_on_the_same_ground_as_a_full_rescore(
    tmp_path: Path, two_stub_scorers: None
) -> None:
    """The point of M11, stated as the comparison nothing made before.

    `alpha` is carried across the edit and `beta` is re-run. Every `alpha` measurement that
    survives must say exactly what a full re-score of the *edited* line says about the
    segment it now names - and nothing may name a segment the edited line does not have.
    """
    original = _route()
    edited = _edited(original)
    request, ctx = _request(), _ctx(tmp_path)

    before = score_once(original, request, ctx, start_at=MADE_AT)
    reference = score_once(edited, request, ctx, start_at=MADE_AT)
    partial = score_once(
        edited,
        request,
        ctx,
        start_at=MADE_AT,
        only={"beta"},
        carried=before.results,
        carried_as_of=MADE_AT,
        carried_route=before.route,
        carried_segments=before.segments,
    )

    truth = _by_id(reference.results, "alpha")
    carried = _by_id(partial.results, "alpha")

    assert carried, "the untouched head of the route still carries its measurements"
    assert set(carried) <= set(truth), "nothing landed on a segment the edited line lacks"
    for segment_id, values in carried.items():
        assert values == truth[segment_id], (
            f"{segment_id} was carried onto ground a full re-score measures differently"
        )


def test_the_fixture_really_renumbers_or_the_comparison_above_proves_nothing(
    tmp_path: Path, two_stub_scorers: None
) -> None:
    """The sabotage half. Carrying `segment_id`s verbatim - which is what `run_scorers` did
    before M11.3 - must disagree with the full re-score, or the edit moved nothing and the
    headline test would pass on a no-op."""
    original = _route()
    edited = _edited(original)
    request, ctx = _request(), _ctx(tmp_path)

    before = score_once(original, request, ctx, start_at=MADE_AT)
    reference = score_once(edited, request, ctx, start_at=MADE_AT)

    stored = _by_id(before.results, "alpha")
    truth = _by_id(reference.results, "alpha")

    wrong = [key for key, values in stored.items() if key in truth and truth[key] != values]
    assert wrong, (
        "the edit left every segment id naming the same ground, so this fixture cannot "
        "distinguish a re-keyed carry from a verbatim one"
    )


def test_what_the_edit_took_away_is_reported_rather_than_dropped_quietly(
    tmp_path: Path, two_stub_scorers: None
) -> None:
    """The other half of ADR 0032: a measurement about ground that is gone is an answer, and
    a result that cannot say it lost one is a sheet describing a street nobody will run."""
    original = _route()
    edited = _edited(original)
    request, ctx = _request(), _ctx(tmp_path)

    before = score_once(original, request, ctx, start_at=MADE_AT)
    partial = score_once(
        edited,
        request,
        ctx,
        start_at=MADE_AT,
        only={"beta"},
        carried=before.results,
        carried_as_of=MADE_AT,
        carried_route=before.route,
        carried_segments=before.segments,
    )

    alpha = next(r for r in partial.results if r.name == "alpha")
    assert alpha.carried_from == MADE_AT
    assert alpha.regrounded is not None
    assert alpha.regrounded.measurements_dropped > 0
    assert alpha.regrounded.measurements_kept == len(alpha.measurements)
    assert "no longer on the route" in alpha.regrounded.describe()


def test_a_rescored_scorer_is_measured_on_the_new_line_not_carried(
    tmp_path: Path, two_stub_scorers: None
) -> None:
    """`beta` was re-run, so it covers the whole edited line including the new stretch."""
    original = _route()
    edited = _edited(original)
    request, ctx = _request(), _ctx(tmp_path)

    before = score_once(original, request, ctx, start_at=MADE_AT)
    partial = score_once(
        edited,
        request,
        ctx,
        start_at=MADE_AT,
        only={"beta"},
        carried=before.results,
        carried_as_of=MADE_AT,
        carried_route=before.route,
        carried_segments=before.segments,
    )

    beta = next(r for r in partial.results if r.name == "beta")
    assert beta.carried_from is None
    assert beta.regrounded is None
    assert len(beta.measurements) == len(partial.segments)


# --- through `rescore_plan`, which is the door the CLI and the API use --------


def _stored_plan(tmp_path: Path) -> Plan:
    request = _request()
    manifest = Manifest(snapshot=SnapshotPins(osm_extract_date="2026-03-01"))
    ctx = _ctx(tmp_path)
    scored = score_once(
        _route(),
        request,
        ctx,
        start_at=MADE_AT,
        manifest=manifest,
        # There is no DEM under `tmp_path`, so without this the stored plan has no profile
        # at all and cannot show that an edit preserved one.
        elevations=list(ELEVATIONS),
    )
    return build_plan(
        scored, request, profile=load_defaults(), coverage=scored.coverage, manifest=manifest
    )


def test_an_edit_reports_where_the_line_moved_and_a_refresh_does_not(tmp_path: Path) -> None:
    """ADR 0030 argues a route diff of a refresh is provably vacuous and worse than useless -
    "same line" beside a plan whose trail has just closed reads as "nothing changed". Across
    an edit it is the first thing a reader wants, so it is present exactly when it means
    something."""
    stored = _stored_plan(tmp_path)

    refreshed = rescore_plan(stored, _ctx(tmp_path), start_at=MADE_AT)
    assert refreshed.delta.geometry is None

    edited = rescore_plan(
        stored, _ctx(tmp_path), start_at=MADE_AT, route=_edited(stored.route), only={"lighting"}
    )
    assert edited.delta.geometry is not None
    assert edited.delta.geometry.spans, "the line moved and the diff found where"
    assert any("line:" in line for line in edited.delta.lines())


def test_an_edited_plan_carries_the_edited_line_and_says_so(tmp_path: Path) -> None:
    stored = _stored_plan(tmp_path)
    out = rescore_plan(
        stored, _ctx(tmp_path), start_at=MADE_AT, route=_edited(stored.route), only={"lighting"}
    )
    assert out.plan.route.source == "edited"
    assert out.plan.id == stored.id, "the same plan with a different line, not a new plan"


def test_an_edit_re_runs_everything_unless_told_otherwise(tmp_path: Path) -> None:
    """A refresh's default set is the time-dependent scorers, because legality did not change
    overnight. An edit's cannot be: every scorer is about the line, and carrying
    `segment_hostility` across a reroute would report the old street's traffic stress."""
    stored = _stored_plan(tmp_path)

    out = rescore_plan(stored, _ctx(tmp_path), start_at=MADE_AT, route=_edited(stored.route))

    assert out.delta.carried == (), "an edit with no `only` carries nothing"
    assert set(out.delta.rescored) == set(registry.SCORERS)


def test_a_partial_rescore_across_an_edit_reports_what_the_carry_cost(tmp_path: Path) -> None:
    """The delta is where a caller with a terminal learns that carrying was not free."""
    stored = _stored_plan(tmp_path)

    out = rescore_plan(
        stored, _ctx(tmp_path), start_at=MADE_AT, route=_edited(stored.route), only={"lighting"}
    )

    assert out.delta.carried, "something was carried"
    assert out.delta.regrounded, "and it did not all survive the edit"
    assert any(scorer in line for scorer, _ in out.delta.regrounded for line in out.delta.lines())


def test_an_edited_plan_keeps_the_elevations_of_the_ground_it_did_not_touch(
    tmp_path: Path,
) -> None:
    """M10.4's bug through a different door: there is no DEM here, so resampling would
    replace a good profile with nulls. `splice` keeps what it knew and the re-score reads
    the elevations off the edited line rather than off the stored one, which has a different
    point count."""
    stored = _stored_plan(tmp_path)

    out = rescore_plan(
        stored, _ctx(tmp_path), start_at=MADE_AT, route=_edited(stored.route), only={"lighting"}
    )

    heights = [point.ele_m for point in out.plan.route.points]
    assert heights[0] == 100.0, "the head keeps the profile it had"
    assert heights[-1] is not None, "and so does the tail"
    assert any(height is None for height in heights), "the spliced stretch does not pretend"
