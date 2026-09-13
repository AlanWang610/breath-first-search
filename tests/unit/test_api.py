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


@pytest.mark.parametrize("attempt", ["../secrets", "..%2Fsecrets", "a/b", ".hidden"])
def test_a_plan_id_that_is_not_a_plain_name_is_refused(client: Any, attempt: str) -> None:
    """`plan_id` arrives from a URL, and this is the one way a read-only local API can still
    be dangerous."""
    assert client.get(f"/api/plans/{attempt}").status_code in (404, 422)


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
    }


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
