"""`longrun edit` (M11.8) - scope 10.3's five gestures, driven the way a test can drive them.

Scope 3.9 says a capability exists as a tool and a command before it gets a UI control, and
this is the command half of §10.3. It is also the only way a headless run can exercise any
of it: nothing here can drag on a MapLibre canvas, so a milestone whose only door was a
browser would ship unverified.

`reroute` is the one gesture with no test below, and deliberately: it needs a GraphHopper
server, which the hermetic suite does not have and must not reach. `edits.redraw` is what it
calls and that is covered against a stub router; the CLI wiring around it is the same
`resolve` + `CachedRouter` pattern `longrun plan` has used since M9.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from longrun.cli.main import app
from longrun.core.data.cache import SqliteCache
from longrun.core.data.file_store import FileLayerStore, FileRasterStore
from longrun.core.geo.gpx import gpx_write, normalize
from longrun.core.models.context import Budget, FrozenClock, ScorerContext
from longrun.core.models.coverage import CoverageManifest
from longrun.core.models.geometry import Route
from longrun.core.models.plan import Manifest, Plan, SnapshotPins
from longrun.core.models.request import LockedRange, PlanRequest
from longrun.core.plan.pipeline import build_plan, score_once
from longrun.core.preferences.store import load_defaults

runner = CliRunner()

MADE_AT = datetime(2026, 3, 15, 7, 30)
BASE_LAT = 37.7749
BASE_LON = -122.4194
STEP_DEG = 0.00114
POINTS = 21


@pytest.fixture
def plan_file(tmp_path: Path) -> Path:
    """A scored plan on disk, the way `longrun repair --out` leaves one."""
    route = Route(
        id="editable",
        points=normalize(
            [(BASE_LAT, BASE_LON + index * STEP_DEG, 100.0 + index) for index in range(POINTS)]
        ),
    )
    request = PlanRequest(mode="repair", date=MADE_AT.date(), start_time=MADE_AT.time())
    manifest = Manifest(snapshot=SnapshotPins(osm_extract_date="2026-03-01"))
    ctx = ScorerContext(
        layers=FileLayerStore(tmp_path),
        rasters=FileRasterStore(tmp_path),
        cache=SqliteCache(offline=True),
        clock=FrozenClock(MADE_AT),
        coverage=CoverageManifest(),
        profile=load_defaults(),
        budget=Budget(),
    )
    scored = score_once(
        route,
        request,
        ctx,
        start_at=MADE_AT,
        manifest=manifest,
        elevations=[100.0 + index for index in range(POINTS)],
    )
    plan = build_plan(
        scored, request, profile=load_defaults(), coverage=scored.coverage, manifest=manifest
    )
    path = tmp_path / "plan.json"
    path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    return path


def _plan(path: Path) -> Plan:
    return Plan.model_validate_json(path.read_text(encoding="utf-8"))


def _run(*args: str) -> object:
    result = runner.invoke(app, ["edit", *args])
    assert result.exit_code == 0, result.output
    return result


# --- lock and unlock ---------------------------------------------------------


def test_locking_a_range_writes_it_where_the_loop_will_read_it(plan_file: Path) -> None:
    """`_fresh` seeds the scratchpad from `PlanRequest.locked`, so that is where a lock has
    to land for the next round to honour it."""
    _run("lock", str(plan_file), "--from-m", "400", "--to-m", "900", "--reason", "my bit")

    locked = _plan(plan_file).request.locked
    assert [(r.start_m, r.end_m, r.source) for r in locked] == [(400.0, 900.0, "user")]
    assert locked[0].reason == "my bit"


def test_unlocking_the_middle_of_a_lock_leaves_its_ends_locked(plan_file: Path) -> None:
    _run("lock", str(plan_file), "--from-m", "0", "--to-m", "2000")
    _run("unlock", str(plan_file), "--from-m", "800", "--to-m", "1200")

    locked = _plan(plan_file).request.locked
    assert [(r.start_m, r.end_m) for r in locked] == [(0.0, 800.0), (1200.0, 2000.0)]


def test_unlock_mine_leaves_the_loops_own_reroute_locks_standing(plan_file: Path) -> None:
    """The gesture `LockedRange.source` exists for."""
    plan = _plan(plan_file)
    seeded = plan.model_copy(
        update={
            "request": plan.request.model_copy(
                update={
                    "locked": [
                        LockedRange(start_m=0.0, end_m=500.0, reason="mine"),
                        LockedRange(
                            start_m=500.0, end_m=900.0, reason="round 2: rerouted", source="loop"
                        ),
                    ]
                }
            )
        }
    )
    plan_file.write_text(seeded.model_dump_json(indent=2), encoding="utf-8")

    result = _run("unlock", str(plan_file), "--from-m", "0", "--to-m", "2000", "--mine")

    locked = _plan(plan_file).request.locked
    assert [(r.start_m, r.end_m, r.source) for r in locked] == [(500.0, 900.0, "loop")]
    assert "released" in result.output  # type: ignore[attr-defined]


def test_unlocking_where_nothing_is_locked_says_so_rather_than_failing(plan_file: Path) -> None:
    result = _run("unlock", str(plan_file), "--from-m", "0", "--to-m", "100")
    assert "nothing locked" in result.output  # type: ignore[attr-defined]


def test_a_zero_length_unlock_exits_rather_than_writing_the_plan_back(plan_file: Path) -> None:
    result = runner.invoke(app, ["edit", "unlock", str(plan_file), "--from-m", "5", "--to-m", "5"])
    assert result.exit_code == 2
    assert "positive length" in result.output


# --- via ----------------------------------------------------------------------


def test_a_via_point_reaches_the_request_and_the_line_is_left_alone(plan_file: Path) -> None:
    """Drawing a line through it is a routing call, not a side effect of pinning it."""
    before = _plan(plan_file).route
    result = _run("via", str(plan_file), "--at", f"{BASE_LAT},{BASE_LON + 5 * STEP_DEG}")

    after = _plan(plan_file)
    assert [(p.lat, p.lon) for p in after.request.via] == [(BASE_LAT, BASE_LON + 5 * STEP_DEG)]
    assert after.route.points == before.points
    assert "still runs where it did" in result.output  # type: ignore[attr-defined]


def test_vias_are_kept_in_route_order_however_they_arrive(plan_file: Path) -> None:
    _run("via", str(plan_file), "--at", f"{BASE_LAT},{BASE_LON + 15 * STEP_DEG}")
    _run("via", str(plan_file), "--at", f"{BASE_LAT},{BASE_LON + 5 * STEP_DEG}")

    lons = [point.lon for point in _plan(plan_file).request.via]
    assert lons == sorted(lons), "the router draws through `via` in order"


# --- avoid --------------------------------------------------------------------


def _polygon(path: Path, half_deg: float) -> Path:
    lat, lon = BASE_LAT + 0.004, BASE_LON + 0.004
    ring = [
        [lon - half_deg, lat - half_deg],
        [lon + half_deg, lat - half_deg],
        [lon + half_deg, lat + half_deg],
        [lon - half_deg, lat + half_deg],
        [lon - half_deg, lat - half_deg],
    ]
    path.write_text(json.dumps({"type": "Polygon", "coordinates": [ring]}), encoding="utf-8")
    return path


def test_an_avoid_polygon_is_stored_rounded_and_the_stale_line_is_admitted(
    plan_file: Path, tmp_path: Path
) -> None:
    """M5.13's lesson: an avoid area travels inside `custom_model`, which `CachedRouter`
    hashes into its key, so an unrounded coordinate from a browser poisons it."""
    result = _run("avoid", str(plan_file), "--polygon", str(_polygon(tmp_path / "a.json", 0.002)))

    areas = _plan(plan_file).request.avoid_polygons
    assert len(areas) == 1
    ring = areas[0]["geometry"]["coordinates"][0]
    assert all(value == round(value, 6) for point in ring for value in point)
    assert "drawn without it" in result.output  # type: ignore[attr-defined]


def test_an_over_cap_avoid_polygon_is_refused_by_name_with_its_size(
    plan_file: Path, tmp_path: Path
) -> None:
    """Closing four square kilometres takes the parallel streets a detour was going to use.
    A runner told "not honoured, that is 24 km2" can draw a smaller one; one told nothing
    gets a route through the area they asked to stay out of."""
    result = runner.invoke(
        app,
        ["edit", "avoid", str(plan_file), "--polygon", str(_polygon(tmp_path / "b.json", 0.03))],
    )
    assert result.exit_code == 2
    assert "km2" in result.output
    assert not _plan(plan_file).request.avoid_polygons


# --- choose -------------------------------------------------------------------


def _alternative(path: Path) -> Path:
    detour = Route(
        id="alt",
        points=normalize(
            [(BASE_LAT + 0.0018, BASE_LON + (8 + index) * STEP_DEG, None) for index in range(6)]
        ),
    )
    return gpx_write(detour, path)


def test_choosing_an_alternative_splices_locks_and_rescores_in_one_command(
    plan_file: Path, tmp_path: Path
) -> None:
    """The three are one command on purpose. A plan carrying an edited line and measurements
    of the old one is exactly the state M11 exists to prevent, and separate steps would make
    it reachable by stopping halfway."""
    before = _plan(plan_file)
    result = _run(
        "choose",
        str(plan_file),
        "--from-m",
        "800",
        "--to-m",
        "1300",
        "--alternative",
        str(_alternative(tmp_path / "alt.gpx")),
        "--fixtures",
        str(tmp_path),
    )

    after = _plan(plan_file)
    assert after.route.source == "edited"
    assert after.route.length_m != before.route.length_m
    assert after.id == before.id, "the same plan with a different line"
    assert [(r.start_m, r.end_m, r.source) for r in after.request.locked] == [
        (800.0, 1300.0, "user")
    ]
    assert "line:" in result.output, "where the line moved is reported"  # type: ignore[attr-defined]


def test_a_partial_choose_carries_what_it_did_not_run_and_says_what_that_cost(
    plan_file: Path, tmp_path: Path
) -> None:
    """Scope 10.3's "each action re-runs only the affected scorers", end to end."""
    result = _run(
        "choose",
        str(plan_file),
        "--from-m",
        "800",
        "--to-m",
        "1300",
        "--alternative",
        str(_alternative(tmp_path / "alt.gpx")),
        "--only",
        "lighting",
        "--fixtures",
        str(tmp_path),
    )

    after = _plan(plan_file)
    carried = [r for r in after.results if r.carried]
    assert carried, "a partial pass carries the scorers it did not run"
    assert any(r.regrounded is not None for r in carried), (
        "and a carry across a moved segmentation records what it lost"
    )
    assert "re-scored 1" in result.output  # type: ignore[attr-defined]


def test_choosing_with_no_line_left_to_join_to_is_refused(plan_file: Path, tmp_path: Path) -> None:
    """Splicing from zero is not a splice: there is no head, and the result would be a
    different route wearing this one's id."""
    result = runner.invoke(
        app,
        [
            "edit",
            "choose",
            str(plan_file),
            "--from-m",
            "0",
            "--to-m",
            "1300",
            "--alternative",
            str(_alternative(tmp_path / "alt.gpx")),
        ],
    )
    assert result.exit_code == 2
    assert "consume one of its ends" in result.output


def test_an_edit_does_not_move_the_date_unless_asked(plan_file: Path, tmp_path: Path) -> None:
    """An edit asks "what is this line now", not "what will it be in March". Moving the date
    under cover of a geometry change would change every time-dependent answer silently."""
    _run(
        "choose",
        str(plan_file),
        "--from-m",
        "800",
        "--to-m",
        "1300",
        "--alternative",
        str(_alternative(tmp_path / "alt.gpx")),
        "--fixtures",
        str(tmp_path),
    )
    assert _plan(plan_file).request.date == MADE_AT.date()
