"""`longrun refresh` (scope 7.8), and the five bugs the old `refresh_plan` carried.

The MCP tool has existed since M5.7 and none of these could bite, because it never wrote its
result anywhere. M10 makes it write, which is what arms them - so each has a test here and
they land in the same commit as the write-back.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from longrun.core.data.cache import SqliteCache
from longrun.core.data.file_store import FileLayerStore, FileRasterStore
from longrun.core.models.context import Budget, FrozenClock, ScorerContext
from longrun.core.models.coverage import CoverageManifest
from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.models.plan import Manifest, Plan, SnapshotPins, TradeOff
from longrun.core.models.request import PlanRequest
from longrun.core.plan.pipeline import build_plan, score_once
from longrun.core.plan.refresh import rescore_plan
from longrun.core.preferences.store import load_defaults
from longrun.core.scorers.freshness import TIME_INDEPENDENT

MADE_AT = datetime(2026, 3, 15, 7, 30)
REFRESHED_AT = datetime(2026, 9, 20, 7, 30)


def _route() -> Route:
    return Route(
        id="refresh",
        points=[
            RoutePoint(
                lat=37.7749,
                lon=-122.4194 + i * 0.00114,
                cum_dist_m=i * 100.0,
                ele_m=100.0 + i,
            )
            for i in range(21)
        ],
    )


def _ctx(root: Path, at: datetime) -> ScorerContext:
    return ScorerContext(
        layers=FileLayerStore(root),
        rasters=FileRasterStore(root),
        cache=SqliteCache(offline=True),
        clock=FrozenClock(at),
        coverage=CoverageManifest(),
        profile=load_defaults(),
        budget=Budget(),
    )


def _stored_plan(tmp_path: Path) -> Plan:
    """A plan made on MADE_AT, the way `repair` would have made one."""
    request = PlanRequest(mode="repair", date=MADE_AT.date(), start_time=MADE_AT.time())
    manifest = Manifest(snapshot=SnapshotPins(osm_extract_date="2026-03-01"))
    ctx = _ctx(tmp_path, MADE_AT)
    scored = score_once(_route(), request, ctx, start_at=MADE_AT, manifest=manifest)
    return build_plan(
        scored,
        request,
        profile=load_defaults(),
        coverage=scored.coverage,
        manifest=manifest,
    )


def test_a_refreshed_plan_keeps_its_identity(tmp_path: Path) -> None:
    """A refresh restates the same plan as of a new date; it does not mint a new one.

    `build_plan` mints `f"{route.id}-{uuid4hex8}"` when no `plan_id` is passed, and the old
    tool passed none - so a refreshed plan was untraceable to the plan it refreshed.
    """
    stored = _stored_plan(tmp_path)
    out = rescore_plan(stored, _ctx(tmp_path, REFRESHED_AT), start_at=REFRESHED_AT)
    assert out.plan.id == stored.id


def test_a_refreshed_plan_reports_this_passs_cost_and_not_the_last_ones(
    tmp_path: Path,
) -> None:
    """A fresh manifest seeded with the stored pins, not the stored manifest.

    `Manifest.record` is a bare append and `total_elapsed_s` sums, so reusing the stored one
    accumulates forever and makes scope 6.4's latency budget unmeasurable after one refresh.
    """
    stored = _stored_plan(tmp_path)
    before = len(stored.manifest.tool_calls)
    out = rescore_plan(stored, _ctx(tmp_path, REFRESHED_AT), start_at=REFRESHED_AT)

    tools = {c.tool for c in out.plan.manifest.tool_calls}
    assert tools and tools.isdisjoint(TIME_INDEPENDENT), "a carried scorer was timed"
    assert len(out.plan.manifest.tool_calls) < before, "the stored calls were re-used"
    assert out.plan.manifest.snapshot.osm_extract_date == "2026-03-01"


def test_a_refresh_keeps_the_trade_offs_the_user_already_resolved(tmp_path: Path) -> None:
    """`build_plan` writes 14 of `Plan`'s 17 fields; the loop patches the other three back.

    Nothing patched them on a refresh, so refreshing a plan that was waiting on the user
    discarded their trade-offs and flipped `needs_input` to `complete`.
    """
    stored = _stored_plan(tmp_path).model_copy(
        update={
            "trade_offs": [
                TradeOff(
                    segment_id="s0",
                    option_a="A",
                    option_b="B",
                    comparison="A is shadier; B is shorter",
                )
            ],
            "warnings": ["a warning worth keeping"],
            "status": "needs_input",
        }
    )
    out = rescore_plan(stored, _ctx(tmp_path, REFRESHED_AT), start_at=REFRESHED_AT)

    assert len(out.plan.trade_offs) == 1
    assert out.plan.warnings == ["a warning worth keeping"]
    assert out.plan.status == "needs_input"


def test_a_refresh_does_not_destroy_the_elevation_profile(tmp_path: Path) -> None:
    """The armed bug: this fixture root has no DEM, and a refresh writes back."""
    stored = _stored_plan(tmp_path)
    stored = stored.model_copy(update={"route": _route()})  # the stored route has elevations

    out = rescore_plan(stored, _ctx(tmp_path, REFRESHED_AT), start_at=REFRESHED_AT)

    assert [p.ele_m for p in out.plan.route.points] == [p.ele_m for p in stored.route.points]


def test_a_refresh_re_runs_the_date_sensitive_scorers_and_carries_the_rest(
    tmp_path: Path,
) -> None:
    """One assertion, and it is the milestone."""
    stored = _stored_plan(tmp_path)
    out = rescore_plan(stored, _ctx(tmp_path, REFRESHED_AT), start_at=REFRESHED_AT)

    assert {r.name for r in out.plan.results if r.carried} == TIME_INDEPENDENT
    assert set(out.delta.carried) == TIME_INDEPENDENT
    assert not set(out.delta.rescored) & TIME_INDEPENDENT


def test_a_carried_result_says_how_stale_it_is(tmp_path: Path) -> None:
    stored = _stored_plan(tmp_path)
    out = rescore_plan(stored, _ctx(tmp_path, REFRESHED_AT), start_at=REFRESHED_AT)

    legality = next(r for r in out.plan.results if r.name == "legality")
    assert legality.carried_from == MADE_AT


def test_a_refresh_reports_what_it_did_rather_than_what_the_route_looks_like(
    tmp_path: Path,
) -> None:
    """The delta speaks about time, not geometry - `route_diff` on a refresh is vacuous."""
    stored = _stored_plan(tmp_path)
    out = rescore_plan(stored, _ctx(tmp_path, REFRESHED_AT), start_at=REFRESHED_AT)

    lines = out.delta.lines()
    assert any("re-scored" in line and "carried" in line for line in lines)
    assert not any("same line" in line for line in lines)
    assert out.delta.finish_before != out.delta.finish_after


def test_the_refreshed_plan_round_trips_through_json(tmp_path: Path) -> None:
    """`carried_from` has to survive `plan.json`, or the honesty stops at the first write."""
    stored = _stored_plan(tmp_path)
    out = rescore_plan(stored, _ctx(tmp_path, REFRESHED_AT), start_at=REFRESHED_AT)

    reloaded = Plan.model_validate_json(out.plan.model_dump_json())
    assert {r.name for r in reloaded.results if r.carried} == TIME_INDEPENDENT
