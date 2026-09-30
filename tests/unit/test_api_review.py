"""The Review endpoints: a pair on the page, blind, with its numbers held back (ADR 0049).

In-process through `fastapi.testclient`, against a ledger under `tmp_path`. The witness for
every rule is the JSON the page receives: what it does not contain is as much the contract
as what it does.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from longrun.core.labels.ledger import Ledger, labels_dir_from_env
from longrun.core.labels.pairs import Candidate, PairTask, probe
from longrun.core.labels.pairs_io import read_pairs
from longrun.core.labels.review import route_of
from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.routing.tuning import RouteSummary, WayClass

fastapi = pytest.importorskip("fastapi", reason="the api extra is not installed")
from fastapi.testclient import TestClient  # noqa: E402

from longrun.api.app import create_app  # noqa: E402
from longrun.jobs.runner import JobRunner, JobStore  # noqa: E402

NOW = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)


def _route(route_id: str, bulge: float) -> Route:
    points = []
    for i in range(21):
        t = i / 20
        points.append(
            RoutePoint(
                lat=37.83 + bulge * (1 - abs(2 * t - 1)),
                lon=-122.53 + 0.04 * t,
                cum_dist_m=0.0,
            )
        )
    return route_of(
        Candidate(
            id=route_id,
            drawn_by="x",
            points=[(p.lat, p.lon) for p in points],
            length_m=0.0,
            metres_by_class={},
        )
    )


def _task(task_id: str = "t1") -> PairTask:
    streets = RouteSummary.from_ways("s", [(WayClass(lts=3), 3500.0)])
    trail = RouteSummary.from_ways("t", [(WayClass(unpaved=True, is_path=True), 4000.0)])
    return PairTask(
        id=task_id,
        region="bayarea",
        ends=((37.83, -122.53), (37.83, -122.49)),
        a=Candidate.of("c1", "neutral", _route("c1", 0.0), streets),
        b=Candidate.of("c2", "seek-paths", _route("c2", 0.01), trail),
        stratum="path_bonus",
        split=0.5,
        created_at=NOW,
    )


@pytest.fixture
def ledger() -> Ledger:
    made = Ledger(labels_dir_from_env())
    made.add_task(_task())
    return made


@pytest.fixture
def client(tmp_path: Path) -> Any:
    runner = JobRunner(JobStore(tmp_path / "plans" / "jobs"))
    try:
        with TestClient(create_app(runner, plans_dir=tmp_path / "plans")) as made:
            yield made
    finally:
        runner.shutdown()


@pytest.fixture
def labeler(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("LONGRUN_LABELER", "runner-1")
    return "runner-1"


def test_the_queue_names_the_next_question_and_who_is_answering(
    client: Any, ledger: Ledger
) -> None:
    body = client.get("/api/labels/queue").json()

    assert body["pending"] == 1 and body["answered"] == 0
    assert body["next"] == "t1"
    assert body["labeler"] is None


def test_a_question_is_blind_and_holds_its_numbers_back(client: Any, ledger: Ledger) -> None:
    """The two rules `review.task_view` owns, read off the wire."""
    body = client.get("/api/labels/tasks/t1").json()
    text = json.dumps(body)

    assert "neutral" not in text and "seek-paths" not in text, "never which vector drew it"
    assert "drawn_by" not in text and "metres_by_class" not in text
    assert body["shares"] is None and body["answer"] is None
    assert len(body["a"]["points"]) == 21 and body["b"]["diverges"], "where they part is shown"


@pytest.mark.parametrize("task_id", ["nope", "..", "T1", "t1.json"])
def test_a_task_that_is_not_there_is_a_404(client: Any, ledger: Ledger, task_id: str) -> None:
    assert client.get(f"/api/labels/tasks/{task_id}").status_code == 404


def test_an_answer_reveals_the_numbers_and_becomes_a_pair(
    client: Any, ledger: Ledger, labeler: str
) -> None:
    response = client.post("/api/labels/tasks/t1/answer", json={"prefer": "b", "strength": "clear"})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["answer"]["prefer"] == "b" and body["answer"]["via"] == "ui"
    assert body["shares"]["b"] == {"lts3_plus": 0.0, "unpaved": 1.0, "path": 1.0}
    assert body["shares"]["a"]["lts3_plus"] == 1.0
    (pair,) = read_pairs(ledger.pairs_dir)
    assert (pair.preferred, pair.labeler, pair.source) == ("b", labeler, "judged")
    assert client.get("/api/labels/queue").json()["next"] is None


def test_a_first_answer_stands(client: Any, ledger: Ledger, labeler: str) -> None:
    client.post("/api/labels/tasks/t1/answer", json={"prefer": "none"})

    again = client.post("/api/labels/tasks/t1/answer", json={"prefer": "a", "strength": "clear"})

    assert again.status_code == 409
    assert "a first answer stands" in again.json()["detail"]
    assert ledger.answer("t1").prefer == "none"  # type: ignore[union-attr]


def test_a_choice_without_a_strength_is_refused_in_words(
    client: Any, ledger: Ledger, labeler: str
) -> None:
    response = client.post("/api/labels/tasks/t1/answer", json={"prefer": "a"})

    assert response.status_code == 422
    assert "slight or clear" in response.json()["detail"]
    assert ledger.answers() == []


def test_answering_without_a_labeler_is_refused(client: Any, ledger: Ledger) -> None:
    response = client.post("/api/labels/tasks/t1/answer", json={"prefer": "a", "strength": "clear"})

    assert response.status_code == 409
    assert "LONGRUN_LABELER" in response.json()["detail"]


def test_an_answer_to_no_task_is_a_404(client: Any, ledger: Ledger, labeler: str) -> None:
    response = client.post("/api/labels/tasks/zzz/answer", json={"prefer": "cant"})

    assert response.status_code == 404


def test_a_probe_says_it_is_one_and_the_queue_counts_agreement(
    client: Any, ledger: Ledger, labeler: str
) -> None:
    ledger.add_task(probe(_task(), "p1", NOW))
    client.post("/api/labels/tasks/t1/answer", json={"prefer": "a", "strength": "clear"})
    client.post("/api/labels/tasks/p1/answer", json={"prefer": "b", "strength": "slight"})

    assert client.get("/api/labels/tasks/p1").json()["probe"] is True
    assert client.get("/api/labels/queue").json()["probes"] == {"agree": 1, "total": 1}
