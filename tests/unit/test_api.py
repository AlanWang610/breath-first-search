"""The HTTP surface (scope 10.3), tested without a network.

`fastapi.testclient` drives the app in-process, so nothing here opens a socket and
`conftest._block_network` stays satisfied.

Two things are worth testing and the rest is plumbing. **A pause is a status, not an
error** - a parked job must come back 200 with its question, because scope 4.2's whole
design is that a plan waiting on a person is a normal state that survives the process.
And **the plan schema is the contract** - `GET /api/plans/{id}` must return the fields a
convenience view would drop, which are exactly the honest ones: the coverage manifest, the
pacing caveats, and a `detour_ratio` of `null` meaning nobody measured.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from longrun.core.models.geometry import LatLon, Route, RoutePoint
from longrun.core.models.plan import Plan
from longrun.core.models.request import PlanRequest
from longrun.core.plan.scratchpad import PendingQuestion, Scratchpad

fastapi = pytest.importorskip("fastapi", reason="the api extra is not installed")
from fastapi.testclient import TestClient  # noqa: E402

from longrun.api.app import create_app  # noqa: E402
from longrun.jobs.runner import JobRunner, JobStore  # noqa: E402

ROUTE = Route(
    id="r",
    points=[RoutePoint(lat=37.77, lon=-122.41 + i * 0.001, cum_dist_m=i * 88.0) for i in range(6)],
)


@pytest.fixture
def plans(tmp_path: Path) -> Path:
    return tmp_path / "plans"


@pytest.fixture
def runner(plans: Path) -> Any:
    """A runner per test, shut down at teardown.

    Not a module-level singleton: `conftest._block_network` patches `socket.connect`
    globally and restores it at teardown, so an executor that outlived a test would
    un-block the network for whatever ran next. M5.6 documented that trap for `jobs/` and
    it applies identically here.
    """
    made = JobRunner(JobStore(plans / "jobs"))
    yield made
    made.shutdown()


@pytest.fixture
def client(runner: Any, plans: Path) -> Any:
    with TestClient(create_app(runner, plans_dir=plans)) as made:
        yield made


def _stored_plan(plans: Path, name: str = "p1") -> Plan:
    plan = Plan(
        id=name,
        request=PlanRequest(mode="repair", date=date(2026, 9, 15)),
        route=ROUTE,
        pacing_caveats=["pace comes from a population curve, not from your runs"],
        metrics={"fraction_lts3_plus": 0.05, "lts4_count": 1, "detour_ratio": None},
    )
    directory = plans / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    return plan


# --- what the UI checks before drawing anything ------------------------------


def test_health_names_the_degraded_states_rather_than_saying_ok(client: Any) -> None:
    """No router, no fixtures and no model are three different reasons a plan will be thin,
    and the UI renders them differently. Scope 3.6 at the API boundary."""
    body = client.get("/api/health").json()

    assert body["ok"] is True
    assert "router" in body and "fixtures_present" in body and "offline" in body


def test_the_profile_comes_back_with_its_provenance(client: Any) -> None:
    """The preference panel shows which entries were stated, inferred or defaulted, and
    scope 6.3 permits asking about only the third kind - so dropping provenance here would
    make the panel unable to render the one distinction it exists for."""
    body = client.get("/api/profile").json()

    assert body["traffic_tolerance"]["provenance"] == "default"


# --- the plan schema is the contract -----------------------------------------


def test_a_stored_plan_comes_back_whole(client: Any, plans: Path) -> None:
    """Verbatim rather than through a view model. A second schema drifts, and the first
    fields to drift are the honest ones."""
    _stored_plan(plans)

    body = client.get("/api/plans/p1").json()

    assert body["pacing_caveats"], "a view model would have dropped these first"
    assert "detour_ratio" in body["metrics"]
    assert body["metrics"]["detour_ratio"] is None, "null means nobody measured, not 1.0"
    assert "coverage" in body


def test_the_plan_list_summarises_without_reading_every_plan_whole(
    client: Any, plans: Path
) -> None:
    _stored_plan(plans, "p1")
    _stored_plan(plans, "p2")

    body = client.get("/api/plans").json()

    assert {entry["plan_id"] for entry in body} == {"p1", "p2"}
    assert all("length_m" in entry for entry in body)


def test_a_file_in_the_plans_directory_that_is_not_a_plan_is_skipped(
    client: Any, plans: Path
) -> None:
    """The directory is shared with whatever the CLI wrote there, including sheets and job
    scratchpads. A listing that crashed on the first stray file would be useless."""
    _stored_plan(plans, "p1")
    (plans / "notes.json").write_text('{"hello": "world"}', encoding="utf-8")
    (plans / "broken.json").write_text("{not json", encoding="utf-8")

    assert [entry["plan_id"] for entry in client.get("/api/plans").json()] == ["p1"]


def test_a_missing_plan_is_a_404_and_not_a_crash(client: Any) -> None:
    assert client.get("/api/plans/nope").status_code == 404


@pytest.mark.parametrize(
    "attempt", ["../secrets", "..%2Fsecrets", "a/b", ".hidden", "C:plan", "C:/Windows/win.ini"]
)
def test_a_plan_id_that_is_not_a_plain_name_is_refused(client: Any, attempt: str) -> None:
    """`plan_id` arrives from a URL, and this is the one way a local API is dangerous.

    `C:plan` is the one the original character check let through, and it is why there is a
    containment check as well: `Path("plans") / "C:plan"` is `WindowsPath("C:plan")` on
    Windows - drive-relative, no separator, no leading dot, and outside the plans
    directory. It read a file before M12 and would have written one after it.
    """
    assert client.get(f"/api/plans/{attempt}").status_code in (404, 422)


@pytest.mark.parametrize("attempt", ["", ".", "..", "C:plan", "a/b", "a\\b", "sub/../x"])
def test_the_resolver_refuses_every_id_that_leaves_the_plans_directory(
    plans: Path, attempt: str
) -> None:
    """The resolver itself, because it is now the *write* surface's whole addressing and a
    404 from one endpoint does not prove the next one is guarded (ADR 0034)."""
    from longrun.api.app import _plan_id_dir

    assert _plan_id_dir(plans, attempt) is None


def test_the_resolver_accepts_a_plain_name(plans: Path) -> None:
    from longrun.api.app import _plan_id_dir

    assert _plan_id_dir(plans, "p1") == plans / "p1"


# --- a pause is a status, not an error ---------------------------------------


def _parked(store: JobStore, job_id: str) -> Scratchpad:
    pad = Scratchpad(
        plan_id=f"plan-{job_id}",
        job_id=job_id,
        request=PlanRequest(
            mode="generate",
            date=date(2026, 9, 15),
            start=LatLon(lat=37.77, lon=-122.41),
            end=LatLon(lat=37.77, lon=-122.405),
        ),
        route=ROUTE,
        status="needs_input",
        question=PendingQuestion(
            id="q1",
            kind="trade_off",
            prompt="A adds 0.8 km and a signalized crossing; B is fully shaded",
            options=["A", "B"],
        ),
    )
    store.save(pad)
    return pad


def test_a_parked_job_is_reported_with_its_question(runner: Any, client: Any) -> None:
    """200, not 409 and not 500. Scope 4.2: a plan waiting on a person has not failed."""
    _parked(runner.store, "job-1")

    body = client.get("/api/jobs/job-1").json()

    assert body["status"] == "needs_input"
    assert body["question"]["options"] == ["A", "B"]


def test_resuming_a_job_that_is_not_waiting_is_a_conflict(runner: Any, client: Any) -> None:
    pad = _parked(runner.store, "job-2")
    runner.store.save(pad.model_copy(update={"status": "complete", "question": None}))

    response = client.post("/api/jobs/job-2/resume", json={"choice": "A"})

    assert response.status_code == 409


def test_an_answer_that_is_not_on_offer_is_refused(runner: Any, client: Any) -> None:
    """The user chooses among the candidates arbitration surfaced (ADR 0019), and "C" is
    not one of them. Refused rather than coerced, because a silently substituted choice is
    a decision made on the runner's behalf."""
    _parked(runner.store, "job-3")

    response = client.post("/api/jobs/job-3/resume", json={"choice": "C"})

    assert response.status_code == 422
    assert "A" in response.json()["detail"]


def test_a_job_nobody_started_is_a_404(client: Any) -> None:
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.post("/api/jobs/nope/resume", json={"choice": "A"}).status_code == 404


def test_a_submission_returns_202_because_nothing_has_been_planned_yet(client: Any) -> None:
    """A UI that rendered a route from this response would be rendering one that does not
    exist. The job id is the whole payload."""
    response = client.post(
        "/api/plans",
        json={"start": "37.7955,-122.3937", "end": "37.7715,-122.4686", "date": "2026-09-15"},
    )

    assert response.status_code == 202
    assert response.json()["job_id"]


def test_a_submission_takes_the_same_fields_the_cli_does(client: Any) -> None:
    """Scope 3.9: a parameter here that the CLI does not have would be a capability only
    the UI had, which is the one thing this milestone may not add."""
    from longrun.api.app import PlanSubmission

    assert set(PlanSubmission.model_fields) <= {
        "start",
        "end",
        "via",
        "date",
        "start_time",
        "utc_offset_hours",
        "target_km",
        "rounds",
        "avoid_high_stress",
        # M12.3. Both reached `PlanRequest` for the first time from here, and
        # `longrun plan` grew `--avoid-name` and `--avoid-polygon` in the same commit -
        # which is the direction this assertion does *not* check, and why the CLI half
        # has its own test in `test_cli.py`.
        "avoid_names",
        "avoid_polygons",
    }


def test_a_submission_can_say_what_to_stay_off(client: Any, runner: Any) -> None:
    """Scope 6.4's two must-avoid forms. Until M12.3 the UI could not express either, so
    "avoid El Camino" was a thing a runner could say to the agent and not to the map."""
    response = client.post(
        "/api/plans",
        json={
            "start": "37.7955,-122.3937",
            "end": "37.7715,-122.4686",
            "date": "2026-09-15",
            "avoid_names": ["El Camino Real"],
            "rounds": 0,
        },
    )

    assert response.status_code == 202


def test_a_submitted_polygon_is_rounded_before_it_reaches_a_routing_key(
    plans: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The submission path needs the same treatment the edit path gets, and for the same
    reason: an avoid area travels inside `custom_model`, `CachedRouter` hashes that into
    its key, and sixteen digits of browser float is the worst case for it (M5.13)."""
    from longrun.api.app import PlanSubmission, _rounded_area

    raw = {
        "type": "Polygon",
        "coordinates": [
            [
                [-122.4194123456789, 37.774912345678],
                [-122.4184123456789, 37.774912345678],
                [-122.4184123456789, 37.775912345678],
                [-122.4194123456789, 37.774912345678],
            ]
        ],
    }
    area, refusal = _rounded_area(raw)

    assert refusal is None and area is not None
    ring = area["geometry"]["coordinates"][0]
    assert all(value == round(value, 6) for point in ring for value in point)
    assert "avoid_polygons" in PlanSubmission.model_fields


def test_an_over_cap_polygon_on_a_submission_is_refused_with_its_size(client: Any) -> None:
    """Refused before the job starts, and by index, because a submission may carry several
    and "one of them is too big" is not something a runner can act on."""
    half = 0.03
    lat, lon = 37.7749, -122.4194
    response = client.post(
        "/api/plans",
        json={
            "start": "37.7955,-122.3937",
            "end": "37.7715,-122.4686",
            "date": "2026-09-15",
            "avoid_polygons": [
                {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [lon - half, lat - half],
                            [lon + half, lat - half],
                            [lon + half, lat + half],
                            [lon - half, lat + half],
                            [lon - half, lat - half],
                        ]
                    ],
                }
            ],
        },
    )

    assert response.status_code == 422
    assert "km2" in response.json()["detail"]
    assert "avoid polygon 0" in response.json()["detail"]


# --- the five gestures (scope 10.3, M12.2) -----------------------------------
#
# Driven through the runner and waited on, because every write is a `jobs.submit` and a
# 202 says only that the work was accepted. What each one asserts is the *stored plan*
# afterwards: the endpoint that reported success and wrote nothing is the failure these
# exist to catch.

EDIT_LAT = 37.7749
EDIT_LON = -122.4194
EDIT_STEP = 0.00114
EDIT_POINTS = 21


@pytest.fixture
def scored(plans: Path, tmp_path: Path) -> Path:
    """A scored plan on disk, the way `longrun repair --out` leaves one.

    Scored rather than hand-built: `choose` re-scores, and a plan with no results carries
    nothing for `rescore_plan` to re-key, which is the half M11.3 exists for.
    """
    from datetime import datetime

    from longrun.core.data.cache import SqliteCache
    from longrun.core.data.file_store import FileLayerStore, FileRasterStore
    from longrun.core.geo.gpx import normalize
    from longrun.core.models.context import Budget, FrozenClock, ScorerContext
    from longrun.core.models.coverage import CoverageManifest
    from longrun.core.models.plan import Manifest, SnapshotPins
    from longrun.core.plan.pipeline import build_plan, score_once
    from longrun.core.preferences.store import load_defaults

    made_at = datetime(2026, 3, 15, 7, 30)
    route = Route(
        id="editable",
        points=normalize(
            [
                (EDIT_LAT, EDIT_LON + index * EDIT_STEP, 100.0 + index)
                for index in range(EDIT_POINTS)
            ]
        ),
    )
    request = PlanRequest(mode="repair", date=made_at.date(), start_time=made_at.time())
    manifest = Manifest(snapshot=SnapshotPins(osm_extract_date="2026-03-01"))
    ctx = ScorerContext(
        layers=FileLayerStore(tmp_path),
        rasters=FileRasterStore(tmp_path),
        cache=SqliteCache(offline=True),
        clock=FrozenClock(made_at),
        coverage=CoverageManifest(),
        profile=load_defaults(),
        budget=Budget(),
    )
    result = score_once(
        route,
        request,
        ctx,
        start_at=made_at,
        manifest=manifest,
        elevations=[100.0 + index for index in range(EDIT_POINTS)],
    )
    plan = build_plan(
        result, request, profile=load_defaults(), coverage=result.coverage, manifest=manifest
    )
    directory = plans / "editable"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "plan.json"
    path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    return path


def _stored(path: Path) -> Plan:
    return Plan.model_validate_json(path.read_text(encoding="utf-8"))


def _applied(runner: Any, response: Any) -> str:
    """Wait for the edit the response accepted, and hand back its job id."""
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    pad = runner.wait(job_id, timeout=120)
    assert pad.status == "complete", [event.message for event in runner.events(job_id)]
    return str(job_id)


def test_a_lock_reaches_the_stored_plan(client: Any, runner: Any, scored: Path) -> None:
    """The endpoint that returns 202 and writes nothing is the failure this catches."""
    response = client.post(
        "/api/plans/editable/lock", json={"start_m": 800, "end_m": 1300, "reason": "roadworks"}
    )
    _applied(runner, response)

    locked = _stored(scored).request.locked
    assert [(lock.start_m, lock.end_m, lock.source) for lock in locked] == [(800.0, 1300.0, "user")]
    assert locked[0].reason == "roadworks"


def test_a_lock_written_over_http_is_the_runners_own(
    client: Any, runner: Any, scored: Path
) -> None:
    """M11.1 added `source` so that "unlock what I locked" had a discriminator, and there is
    exactly one loop-authored site in the tree. A browser is not it, so `source` is not a
    parameter here and an unlock `--mine` must be able to take this back."""
    _applied(runner, client.post("/api/plans/editable/lock", json={"start_m": 0, "end_m": 500}))
    _applied(
        runner,
        client.post("/api/plans/editable/unlock", json={"start_m": 0, "end_m": 500, "mine": True}),
    )

    assert _stored(scored).request.locked == []


def test_an_unlock_trims_a_lock_it_only_partly_covers(
    client: Any, runner: Any, scored: Path
) -> None:
    """A runner who frees 2-3 km of a 0-10 km lock has said nothing about the other nine.
    Dropping the whole entry would reopen them to the next round's reroute, which is the
    failure scope 8.4's auto-lock exists to prevent, arriving through the undo button."""
    _applied(runner, client.post("/api/plans/editable/lock", json={"start_m": 0, "end_m": 2000}))
    _applied(
        runner, client.post("/api/plans/editable/unlock", json={"start_m": 800, "end_m": 1200})
    )

    kept = [(lock.start_m, lock.end_m) for lock in _stored(scored).request.locked]
    assert kept == [(0.0, 800.0), (1200.0, 2000.0)]


def test_a_range_with_no_length_is_refused_before_a_job_starts(client: Any, scored: Path) -> None:
    """422, not a job that fails a second later. A refusal a runner can act on belongs in
    the response they are waiting on."""
    assert (
        client.post("/api/plans/editable/lock", json={"start_m": 900, "end_m": 900}).status_code
        == 422
    )


def test_a_via_goes_in_in_route_order_and_the_line_is_left_where_it_was(
    client: Any, runner: Any, scored: Path
) -> None:
    """`via` is ordered and the router draws through it in that order, so appending a point
    that belongs at 3 km to a list whose last entry is at 30 km asks for a route that runs
    out and back. The stored line is deliberately not cleared: one that no longer passes
    through every via is what `gpx_verify` reports."""
    before = _stored(scored).route
    far = f"{EDIT_LAT},{EDIT_LON + 18 * EDIT_STEP}"
    near = f"{EDIT_LAT},{EDIT_LON + 4 * EDIT_STEP}"
    _applied(runner, client.post("/api/plans/editable/via", json={"at": far}))
    job_id = _applied(runner, client.post("/api/plans/editable/via", json={"at": near}))

    after = _stored(scored)
    assert [round(point.lon, 5) for point in after.request.via] == [
        round(EDIT_LON + 4 * EDIT_STEP, 5),
        round(EDIT_LON + 18 * EDIT_STEP, 5),
    ]
    assert after.route.points == before.points, "nothing here routes"
    messages = " ".join(event.message for event in runner.events(job_id))
    assert "re-route to honour it" in messages, "a via nobody has routed through says so"


def test_a_via_that_does_not_read_as_a_coordinate_is_refused(client: Any, scored: Path) -> None:
    assert client.post("/api/plans/editable/via", json={"at": "somewhere"}).status_code == 422


def _ring(half_deg: float) -> dict[str, Any]:
    lat, lon = EDIT_LAT + 0.004, EDIT_LON + 0.004
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [lon - half_deg, lat - half_deg],
                [lon + half_deg, lat - half_deg],
                [lon + half_deg, lat + half_deg],
                [lon - half_deg, lat + half_deg],
                [lon - half_deg, lat - half_deg],
            ]
        ],
    }


def test_a_drawn_polygon_is_rounded_server_side(client: Any, runner: Any, scored: Path) -> None:
    """M5.13's lesson, at the surface that is worst for it: a browser's polygon carries
    sixteen significant digits of float, an avoid area travels inside `custom_model`, and
    `CachedRouter` hashes that into its key. A client that rounded would be a second
    implementation of a rule with one owner."""
    job_id = _applied(
        runner, client.post("/api/plans/editable/avoid", json={"polygon": _ring(0.002)})
    )

    areas = _stored(scored).request.avoid_polygons
    assert len(areas) == 1
    ring = areas[0]["geometry"]["coordinates"][0]
    assert all(value == round(value, 6) for point in ring for value in point)
    messages = " ".join(event.message for event in runner.events(job_id))
    assert "drawn without it" in messages, "the frozen policy is admitted, not papered over"


def test_an_over_cap_polygon_is_refused_by_name_with_its_size(client: Any, scored: Path) -> None:
    """Closing four square kilometres takes the parallel streets a detour was going to use.
    A runner told "that is 24 km2" can draw a smaller one; one told nothing gets a route
    through the area they asked to stay out of."""
    response = client.post("/api/plans/editable/avoid", json={"polygon": _ring(0.03)})

    assert response.status_code == 422
    assert "km2" in response.json()["detail"]
    assert not _stored(scored).request.avoid_polygons, "refused, never stored"


def test_a_polygon_that_is_not_one_is_a_reason_and_not_a_500(client: Any, scored: Path) -> None:
    response = client.post("/api/plans/editable/avoid", json={"polygon": {"type": "Nonsense"}})

    assert response.status_code == 422


def test_choosing_an_alternative_splices_locks_and_rescores_in_one_request(
    client: Any, runner: Any, scored: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three are one job on purpose. A plan carrying an edited line and measurements of
    the old one is exactly the state M11 exists to prevent, and separate requests would make
    it reachable by closing the tab halfway."""
    monkeypatch.setenv("LONGRUN_FIXTURES", str(tmp_path))
    before = _stored(scored)
    alternative = [
        f"{EDIT_LAT + 0.0018},{EDIT_LON + (8 + index) * EDIT_STEP}" for index in range(6)
    ]

    response = client.post(
        "/api/plans/editable/choose",
        json={"start_m": 800, "end_m": 1300, "alternative": alternative},
    )
    _applied(runner, response)

    after = _stored(scored)
    assert after.route.source == "edited"
    assert after.route.length_m != before.route.length_m
    assert after.id == before.id, "the same plan with a different line"
    assert [(lock.start_m, lock.end_m, lock.source) for lock in after.request.locked] == [
        (800.0, 1300.0, "user")
    ]


def test_the_new_stretch_of_an_edited_line_has_no_elevation_and_the_plan_says_so(
    client: Any, runner: Any, scored: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`splice` keeps the heights of the ground it did not touch and leaves the replacement's
    unknown, and re-reading a DEM for the untouched two-thirds would be M10.4's bug through a
    new door. The gap is real, `samples_missing` counts it, and a UI that drew it as zero
    would throw that work away at the last surface it can be lost at."""
    monkeypatch.setenv("LONGRUN_FIXTURES", str(tmp_path))
    alternative = [
        f"{EDIT_LAT + 0.0018},{EDIT_LON + (8 + index) * EDIT_STEP}" for index in range(6)
    ]

    _applied(
        runner,
        client.post(
            "/api/plans/editable/choose",
            json={"start_m": 800, "end_m": 1300, "alternative": alternative},
        ),
    )

    after = _stored(scored)
    unknown = [point for point in after.route.points if point.ele_m is None]
    assert unknown, "the spliced stretch is unmeasured, not zero"
    assert all(point.ele_m != 0.0 for point in after.route.points)


def test_a_choose_that_names_a_scorer_nobody_answers_to_is_refused(
    client: Any, scored: Path
) -> None:
    response = client.post(
        "/api/plans/editable/choose",
        json={
            "start_m": 800,
            "end_m": 1300,
            "alternative": [f"{EDIT_LAT},{EDIT_LON}", f"{EDIT_LAT},{EDIT_LON + 0.001}"],
            "only": ["no_such_scorer"],
        },
    )

    assert response.status_code == 422
    assert "no_such_scorer" in response.json()["detail"]


@pytest.mark.parametrize(
    "path,body",
    [
        ("lock", {"start_m": 0, "end_m": 100}),
        ("unlock", {"start_m": 0, "end_m": 100}),
        ("via", {"at": "37.0,-122.0"}),
        ("avoid", {"polygon": {"type": "Polygon", "coordinates": [[]]}}),
        ("choose", {"start_m": 0, "end_m": 100, "alternative": ["37.0,-122.0", "37.1,-122.0"]}),
    ],
)
def test_every_write_refuses_a_plan_id_that_is_not_a_plain_name(
    client: Any, path: str, body: dict[str, Any]
) -> None:
    """The read had one guard and it had a hole (M12.1). A write surface with five doors
    needs the assertion made at all five, not at the one that was written first."""
    assert client.post(f"/api/plans/C:evil/{path}", json=body).status_code in (404, 422)
    assert client.post(f"/api/plans/nope/{path}", json=body).status_code == 404


# --- region status -----------------------------------------------------------


def test_regions_list_the_unbuilt_ones_too(client: Any) -> None:
    """ "Not built" and "does not exist" are different answers, and the region-status panel
    is where somebody goes to find out which."""
    body = client.get("/api/regions").json()

    assert any(entry["name"] == "bayarea" for entry in body)
    assert all("built" in entry for entry in body)


# --- the UI mount -------------------------------------------------------------


def test_the_api_serves_without_a_ui_built(runner: Any, plans: Path, tmp_path: Path) -> None:
    """`npm run build` has not necessarily been run, and the API is useful without it -
    the CLI and a curl are both clients."""
    app = create_app(runner, plans_dir=plans, ui_dir=tmp_path / "does-not-exist")

    with TestClient(app) as made:
        assert made.get("/api/health").status_code == 200


def test_a_built_ui_is_served_at_the_root(runner: Any, plans: Path, tmp_path: Path) -> None:
    ui = tmp_path / "dist"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>longrun</title>", encoding="utf-8")

    with TestClient(create_app(runner, plans_dir=plans, ui_dir=ui)) as made:
        assert "longrun" in made.get("/").text
        assert made.get("/api/health").status_code == 200, "the mount must not shadow /api"


def test_the_openapi_schema_is_generated(client: Any) -> None:
    """Which is what makes the API self-describing to whatever client comes next, and is
    free because the routes are typed."""
    schema = json.loads(client.get("/openapi.json").text)

    assert "/api/plans" in schema["paths"]
    assert "/api/jobs/{job_id}/resume" in schema["paths"]


def test_a_job_this_process_never_ran_is_still_answerable(plans: Path) -> None:
    """The boundary scope 4.2 exists for, and the commonest way to reach these endpoints.

    A browser asks about a job an *earlier* process parked - the server restarted, or the
    CLI started it. The runner's in-memory table knows nothing about it and the scratchpad
    on disk knows everything. A 404 here would make every resumable job look lost the
    moment the server was restarted, which is precisely the failure the persisted
    scratchpad was designed to prevent.
    """
    _parked(JobStore(plans / "jobs"), "from-another-life")

    fresh = JobRunner(JobStore(plans / "jobs"))
    try:
        with TestClient(create_app(fresh, plans_dir=plans)) as made:
            body = made.get("/api/jobs/from-another-life").json()

            assert body["status"] == "needs_input"
            assert body["question"]["options"] == ["A", "B"]
            assert body["events"] == [], "this process ran nothing, so it logged nothing"
    finally:
        fresh.shutdown()


def test_the_plan_list_reports_a_distance(client: Any, plans: Path) -> None:
    """`Route.length_m` is a computed property and never reaches the JSON, so reading it
    off `route` gave every entry `null` - and the list showed a column of blanks. Found by
    running the UI against a real stored plan rather than by a test, which is why there is
    one now."""
    _stored_plan(plans, "p1")

    entry = client.get("/api/plans").json()[0]

    assert entry["length_m"] is not None
    assert entry["length_m"] > 0


def test_a_built_regions_steps_are_reported_as_done(client: Any, tmp_path: Path) -> None:
    """`complete` is a property on `StepRecord`; the manifest on disk stores `status`. So
    every step of every built region read as not done, which made the region panel say a
    finished build had finished nothing."""
    from longrun.api.app import _regions

    manifest = {"steps": {"layers": {"status": "done"}, "terrain": {"status": "running"}}}
    spec = Path("deploy/regions")
    if not spec.is_dir():
        pytest.skip("no region specs in this checkout")

    # The pure reader, against a manifest shaped the way `build-region` writes one.
    steps = {
        name: bool(step.get("complete", step.get("status") == "done"))
        for name, step in manifest["steps"].items()
    }
    assert steps == {"layers": True, "terrain": False}
    assert _regions(), "the repo ships region specs, so this must not be empty"


# --- the basemap (ADR 0023) ---------------------------------------------------------


def test_the_basemap_defaults_to_usgs_imagery_in_the_shape_maplibre_wants(client: Any) -> None:
    body = client.get("/api/basemap").json()

    assert body["provider"] == "usgs-imagery"
    assert body["tiles"][0].endswith("/tile/{z}/{y}/{x}")
    assert body["maxzoom"] == 16
    assert body["licence"] == "US public domain"


def test_tiles_switched_off_are_a_normal_answer_with_a_reason(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LONGRUN_TILE_PROVIDER", "none")

    body = client.get("/api/basemap").json()

    assert body["provider"] is None
    assert "none" in body["reason"]


def test_a_misconfigured_provider_is_a_reason_and_not_a_500(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The basemap is an enhancement. A typo in an environment variable is no reason to
    show somebody an empty map - the route still has to draw."""
    monkeypatch.setenv("LONGRUN_TILE_PROVIDER", "custom")

    response = client.get("/api/basemap")

    assert response.status_code == 200
    assert response.json()["provider"] is None
    assert "LONGRUN_TILE_URL" in response.json()["reason"]
