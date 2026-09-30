"""The label endpoints: a person's verdict, from the page they read the plan on (ADR 0048).

Driven in-process through `fastapi.testclient`, against a ledger under `tmp_path`
(`tests/conftest.py::_labels_stay_out_of_home`). The plan judged here is the one
`test_api.py` stores: 440 m, no scorer results - enough to have a length a bound can name
and nothing else, which is the point: every other metric is unmeasured, and a bound on one
has to be refused rather than quietly skipped.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from longrun.core.labels.ledger import Ledger, labels_dir_from_env
from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.models.plan import Plan
from longrun.core.models.request import PlanRequest

fastapi = pytest.importorskip("fastapi", reason="the api extra is not installed")
from fastapi.testclient import TestClient  # noqa: E402

from longrun.api.app import create_app  # noqa: E402
from longrun.jobs.runner import JobRunner, JobStore  # noqa: E402

GOLDENS = Path(__file__).resolve().parents[1] / "golden" / "routes"
REASON = "Under half a kilometre is not a long run by any definition I would put my name to."
ROUTE = Route(
    id="r",
    points=[RoutePoint(lat=37.77, lon=-122.41 + i * 0.001, cum_dist_m=i * 88.0) for i in range(6)],
)


@pytest.fixture
def plans(tmp_path: Path) -> Path:
    plan = Plan(id="p1", request=PlanRequest(mode="repair", date=date(2026, 9, 15)), route=ROUTE)
    directory = tmp_path / "plans" / "p1"
    directory.mkdir(parents=True)
    (directory / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    return tmp_path / "plans"


@pytest.fixture
def client(plans: Path) -> Any:
    runner = JobRunner(JobStore(plans / "jobs"))
    try:
        with TestClient(create_app(runner, plans_dir=plans, goldens_dir=GOLDENS)) as made:
            yield made
    finally:
        runner.shutdown()


@pytest.fixture
def labeler(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("LONGRUN_LABELER", "runner-1")
    return "runner-1"


def _no(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "would_run": "no",
        "reason": REASON,
        "bounds": {"length_m_min": 20000},
    }
    body.update(overrides)
    return body


# --- reading -----------------------------------------------------------------


def test_the_sheet_is_the_stored_plan_rendered(client: Any) -> None:
    response = client.get("/api/plans/p1/sheet")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<!doctype html>" in response.text


@pytest.mark.parametrize("plan_id", ["nope", "..", "C:p1"])
def test_the_sheet_of_a_plan_that_is_not_there_is_a_404(client: Any, plan_id: str) -> None:
    assert client.get(f"/api/plans/{plan_id}/sheet").status_code == 404


def test_measured_names_every_metric_and_says_which_this_plan_has(client: Any) -> None:
    body = client.get("/api/plans/p1/measured").json()

    values = {row["name"]: row["value"] for row in body["metrics"]}
    assert len(values) == 12
    assert values["length_m"] == 440.0
    assert values["fraction_lts3_plus"] is None, "unmeasured is null, never zero"
    assert "synthetic-hazards" in body["goldens"]
    assert body["labeler"] is None and body["judged"] == []


# --- writing: what is refused ------------------------------------------------


def test_a_server_that_does_not_know_who_is_judging_refuses(client: Any) -> None:
    response = client.post("/api/plans/p1/verdict", json=_no())

    assert response.status_code == 409
    assert "LONGRUN_LABELER" in response.json()["detail"]
    assert Ledger(labels_dir_from_env()).verdicts() == []


def test_a_contradiction_comes_back_as_sentences_and_writes_nothing(
    client: Any, labeler: str
) -> None:
    response = client.post("/api/plans/p1/verdict", json=_no(bounds={"length_m_max": 20000}))

    assert response.status_code == 422
    assert "the envelope you would accept" in response.json()["detail"]
    assert Ledger(labels_dir_from_env()).verdicts() == []


def test_a_bound_on_a_number_this_plan_does_not_have_is_refused(client: Any, labeler: str) -> None:
    response = client.post(
        "/api/plans/p1/verdict", json=_no(bounds={"fraction_lts3_plus_max": 0.1})
    )

    assert response.status_code == 422
    assert "could never be checked" in response.json()["detail"]


def test_a_golden_the_plan_claims_has_to_exist(client: Any, labeler: str) -> None:
    response = client.post("/api/plans/p1/verdict", json=_no(golden="../etc"))

    assert response.status_code == 422
    assert Ledger(labels_dir_from_env()).verdicts() == []


def test_a_verdict_on_a_plan_that_is_not_there_is_a_404(client: Any, labeler: str) -> None:
    assert client.post("/api/plans/nope/verdict", json=_no()).status_code == 404


# --- writing: what is recorded -----------------------------------------------


def test_a_verdict_is_recorded_as_the_persons_and_came_from_the_page(
    client: Any, labeler: str
) -> None:
    response = client.post("/api/plans/p1/verdict", json=_no())

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["publishable"] is False, "no golden: it stays in the ledger"
    (record,) = Ledger(labels_dir_from_env()).verdicts()
    assert record.id == body["id"]
    assert record.by == "human" and record.via == "ui" and record.labeler == labeler
    assert record.subject == "p1"


def test_after_judging_the_panel_is_told_this_plan_was_judged(client: Any, labeler: str) -> None:
    recorded = client.post("/api/plans/p1/verdict", json=_no()).json()

    judged = client.get("/api/plans/p1/measured").json()["judged"]

    assert [entry["id"] for entry in judged] == [recorded["id"]]


def test_an_edit_makes_it_a_plan_nobody_has_judged(client: Any, labeler: str, plans: Path) -> None:
    """A stored plan keeps its id through every gesture, and its bytes do not."""
    client.post("/api/plans/p1/verdict", json=_no())
    path = plans / "p1" / "plan.json"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    assert client.get("/api/plans/p1/measured").json()["judged"] == []


def test_a_plan_seeded_from_a_golden_is_publishable(client: Any, labeler: str) -> None:
    response = client.post("/api/plans/p1/verdict", json=_no(golden="synthetic-hazards"))

    assert response.status_code == 201
    assert response.json()["publishable"] is True
