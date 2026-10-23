"""Saving a fit as the runner's routing vector: the re-route check and the gates (ADR 0051).

Every ledger here is the temporary one `conftest._labels_stay_out_of_home` points at, and
every answer is recorded by the test that needs it - the label machinery under test, never a
label anybody gave.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from longrun.cli.main import app
from longrun.core.geo.gpx import haversine_m
from longrun.core.labels.adopt import fitted_entry, refusals
from longrun.core.labels.answers import record_pair_answer
from longrun.core.labels.ledger import Ledger, labels_dir_from_env
from longrun.core.labels.pairs import Candidate, PairTask
from longrun.core.labels.reroute import reroute
from longrun.core.models.geometry import LatLon, Route, RoutePoint
from longrun.core.models.profile import Provenance
from longrun.core.preferences.store import load_profile
from longrun.core.routing.base import NoRouteError, RouterUnavailable
from longrun.core.routing.custom_model import NEUTRAL, CustomModelParams
from longrun.core.routing.tuning import (
    Holdout,
    PreferencePair,
    RouteSummary,
    TuningResult,
    WayClass,
)

NOW = datetime(2026, 10, 23, 8, 0, tzinfo=UTC)
START = LatLon(lat=37.8300, lon=-122.5300)
END = LatLon(lat=37.8300, lon=-122.4900)

STREET = WayClass(lts=2)
TRAIL = WayClass(lts=None, unpaved=True, is_path=True)


def _line(route_id: str, bulge: float) -> Route:
    """START to END, bowed north by `bulge` degrees at its middle (south when negative)."""
    raw = [
        (
            START.lat + bulge * (1 - abs(2 * (i / 40) - 1)),
            START.lon + (i / 40) * (END.lon - START.lon),
        )
        for i in range(41)
    ]
    cum, points = 0.0, []
    for index, (lat, lon) in enumerate(raw):
        if index:
            cum += haversine_m(raw[index - 1][0], raw[index - 1][1], lat, lon)
        points.append(RoutePoint(lat=lat, lon=lon, cum_dist_m=cum))
    return Route(id=route_id, points=points)


#: The street line, and a trail line about 3% longer: inside a 10% detour tolerance, and
#: far enough north of the street for most of its length that the two are distinct routes.
STREET_LINE, TRAIL_LINE = _line("street", 0.0), _line("trail", 0.004)
#: Somewhere neither of them goes.
THIRD_LINE = _line("third", -0.006)
#: Long enough to be a detour nobody tolerates.
DETOUR_LINE = _line("detour", 0.02)


def _task(task_id: str) -> PairTask:
    street = Candidate.of(
        f"{task_id}-s", "neutral", STREET_LINE, RouteSummary.from_ways("s", [(STREET, 3500.0)])
    )
    trail = Candidate.of(
        f"{task_id}-t", "seek-paths", TRAIL_LINE, RouteSummary.from_ways("t", [(TRAIL, 3600.0)])
    )
    return PairTask(
        id=task_id,
        region="bayarea",
        ends=((START.lat, START.lon), (END.lat, END.lon)),
        a=street,
        b=trail,
        stratum="path_bonus",
        split=0.5,
        created_at=NOW,
    )


def _judged(count: int) -> tuple[Ledger, list[PairTask]]:
    """`count` tasks in the temporary ledger, each answered "the trail, clearly"."""
    ledger = Ledger(labels_dir_from_env())
    tasks = [_task(f"{index:012x}") for index in range(1, count + 1)]
    for task in tasks:
        ledger.add_task(task)
        record_pair_answer(
            ledger,
            task.id,
            prefer="b",
            strength="clear",
            reason=None,
            labeler="r1",
            labelled_at=NOW,
            via="cli",
        )
    return ledger, tasks


def _pairs(tasks: list[PairTask]) -> list[PreferencePair]:
    return [
        PreferencePair(
            a=task.a.summary(), b=task.b.summary(), preferred="b", source="judged", labeler="r1"
        )
        for task in tasks
    ]


class Drawing:
    """A router that draws `line` for every request, or refuses with `refuse`."""

    def __init__(self, line: Route | None = TRAIL_LINE, refuse: Exception | None = None) -> None:
        self.line, self.refuse = line, refuse
        self.models: list[Any] = []

    def route(self, waypoints: list[LatLon], **kwargs: Any) -> Route:
        self.models.append(kwargs.get("custom_model"))
        if self.refuse is not None:
            raise self.refuse
        assert self.line is not None
        return self.line


# --- the re-route check ---------------------------------------------------------------


def test_a_line_redrawn_onto_the_chosen_route_counts_for_the_fit() -> None:
    tasks = [_task(f"{i:012x}") for i in range(1, 4)]
    router = Drawing(TRAIL_LINE)

    report = reroute(_pairs(tasks), tasks, lambda task: router, {"priority": []})

    assert [row.outcome for row in report.rows] == ["chosen"] * 3
    assert router.models == [{"priority": []}] * 3, "every call carries the fitted model"
    assert report.passed, report.problems()


def test_landing_on_the_rejected_line_as_often_is_not_agreement() -> None:
    tasks = [_task(f"{i:012x}") for i in range(1, 3)]

    report = reroute(_pairs(tasks), tasks, lambda task: Drawing(STREET_LINE), None)

    assert report.count("rejected") == 2
    assert not report.passed
    assert "on the line the runner chose 0 time(s) and on the one they turned down 2" in (
        " ".join(report.problems())
    )


def test_a_third_line_is_neither_and_a_refusal_fails_the_check() -> None:
    """A refusal is the M23 failure - a vector GraphHopper turns down - and one is enough."""
    tasks = [_task(f"{i:012x}") for i in range(1, 4)]
    routers = iter(
        [Drawing(TRAIL_LINE), Drawing(THIRD_LINE), Drawing(refuse=NoRouteError(START, END))]
    )

    report = reroute(_pairs(tasks), tasks, lambda task: next(routers), None)

    assert [row.outcome for row in report.rows] == ["chosen", "neither", "refused"]
    assert not report.passed
    assert any("refused 1" in problem for problem in report.problems())


def test_a_redrawn_detour_beyond_tolerance_fails_the_check() -> None:
    tasks = [_task("000000000001")]

    report = reroute(_pairs(tasks), tasks, lambda task: Drawing(DETOUR_LINE), None)

    assert len(report.over_tolerance) == 1
    assert any("detour tolerance" in problem for problem in report.problems())


def test_a_pair_with_no_task_here_is_counted_and_not_routed() -> None:
    tasks = [_task("000000000001")]
    stranger = PreferencePair(
        a=RouteSummary.from_ways("elsewhere-a", [(STREET, 10.0)]),
        b=RouteSummary.from_ways("elsewhere-b", [(TRAIL, 10.0)]),
        preferred="b",
        source="judged",
    )
    router = Drawing(TRAIL_LINE)

    report = reroute([*_pairs(tasks), stranger], tasks, lambda task: router, None)

    assert (len(report.rows), report.unchecked, len(router.models)) == (1, 1, 1)


def test_no_pair_with_its_ends_here_is_not_a_pass() -> None:
    assert reroute([], [], lambda task: Drawing(), None).problems() == [
        "no pair has its ends in this ledger, so none could be re-drawn"
    ]


def test_a_stopped_router_is_raised_and_never_blamed_on_the_vector() -> None:
    tasks = [_task("000000000001")]

    with pytest.raises(RouterUnavailable):
        reroute(_pairs(tasks), tasks, lambda task: Drawing(refuse=RouterUnavailable("down")), None)


# --- the gates before any routing -----------------------------------------------------


SEEKS_PATHS = CustomModelParams(path_bonus=2.0)


def _result(params: CustomModelParams = SEEKS_PATHS, **kw: Any) -> Any:
    fields: dict[str, Any] = {
        "params": params,
        "agreement": 0.9,
        "penalty": 0.0,
        "objective": 0.9,
        "pairs_by_source": {"judged": 20},
    }
    fields.update(kw)
    return TuningResult(**fields)


def _holdout(floor: float = 0.6, neutral: float = 0.4) -> Holdout:
    return Holdout(
        folds=5,
        pairs=20,
        fitted=0.75,
        neutral=neutral,
        avoid_high_stress=0.5,
        interval=(floor, 0.9),
    )


def test_a_fit_that_clears_every_gate_has_no_refusals() -> None:
    assert refusals(_result(), labeler="r1", holdout=_holdout()) == []


@pytest.mark.parametrize(
    ("result", "labeler", "holdout", "says"),
    [
        (_result(), None, _holdout(), "--labeler"),
        (_result(pairs_by_source={"judged": 5}), "r1", _holdout(), "5 pair(s) from a person"),
        (_result(params=NEUTRAL), "r1", _holdout(), "nothing to save"),
        (_result(penalty=0.04), "r1", _holdout(), "detour penalty is 0.040"),
        (_result(), "r1", None, "no held-out measurement"),
        (_result(), "r1", _holdout(floor=0.35), "does not clear neutral's 40.0%"),
    ],
)
def test_each_gate_refuses_in_words(
    result: TuningResult, labeler: str | None, holdout: Holdout | None, says: str
) -> None:
    assert any(says in problem for problem in refusals(result, labeler=labeler, holdout=holdout))


def test_a_saved_fit_is_inferred_and_carries_its_record() -> None:
    tasks = [_task("000000000001")]
    report = reroute(_pairs(tasks), tasks, lambda task: Drawing(TRAIL_LINE), None)

    entry = fitted_entry(
        _result(), labeler="r1", holdout=_holdout(), report=report, today=date(2026, 10, 23)
    )

    assert entry.provenance == Provenance.INFERRED
    assert entry.updated == date(2026, 10, 23)
    record = entry.value
    assert (record.params, record.labeler, record.pairs) == (
        CustomModelParams(path_bonus=2.0),
        "r1",
        20,
    )
    assert (record.held_out, record.held_out_interval, record.neutral_held_out) == (
        0.75,
        (0.6, 0.9),
        0.4,
    )
    assert (record.rerouted, record.followed_chosen, record.followed_rejected) == (1, 1, 0)


# --- `longrun tune --save`, end to end ---------------------------------------------------

#: A runner's hand-kept profile, which a save must not rewrite.
KEPT = (
    "# My profile. Hand-written; keep my comments.\n"
    "detour_tolerance_pct:\n"
    "  value: 10.0\n"
    "  provenance: stated\n"
)


@pytest.fixture
def drawing(monkeypatch: pytest.MonkeyPatch) -> Drawing:
    router = Drawing(TRAIL_LINE)
    monkeypatch.setattr("longrun.core.routing.graphhopper.GraphHopperRouter", lambda url: router)

    class _Health:
        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: _Health())
    return router


def _tune(tmp_path: Path, ledger: Ledger, *extra: str) -> Any:
    return CliRunner().invoke(
        app,
        [
            "tune",
            "--pairs",
            str(ledger.pairs_dir),
            "--labeler",
            "r1",
            "--profile",
            str(tmp_path / "profile.yaml"),
            "--router",
            "http://router.invalid",
            *extra,
        ],
    )


def test_save_writes_the_fit_into_the_profile_and_keeps_everything_else(
    tmp_path: Path, drawing: Drawing
) -> None:
    ledger, _ = _judged(14)
    profile_file = tmp_path / "profile.yaml"
    profile_file.write_text(KEPT, encoding="utf-8")

    result = _tune(tmp_path, ledger, "--save")

    assert result.exit_code == 0, result.output
    assert "re-drawn 14 pair(s) with the fitted vector: 14 on the chosen line" in result.output
    assert "saved to" in result.output
    text = profile_file.read_text(encoding="utf-8")
    assert text.startswith(KEPT), "the runner's own lines are copied, not rewritten"
    assert "saved by\n# `longrun tune --save`" in text
    saved = load_profile(profile_file).routing
    assert saved.provenance == Provenance.INFERRED
    assert not saved.value.params.is_neutral
    assert (saved.value.labeler, saved.value.pairs, saved.value.followed_chosen) == ("r1", 14, 14)
    assert drawing.models and all(model for model in drawing.models)


def test_saving_again_replaces_the_block_rather_than_adding_one(
    tmp_path: Path, drawing: Drawing
) -> None:
    ledger, _ = _judged(14)
    (tmp_path / "profile.yaml").write_text(KEPT, encoding="utf-8")

    _tune(tmp_path, ledger, "--save")
    again = _tune(tmp_path, ledger, "--save")

    assert again.exit_code == 0, again.output
    text = (tmp_path / "profile.yaml").read_text(encoding="utf-8")
    assert text.count("\nrouting:") == 1


def test_a_router_that_lands_on_the_rejected_line_stops_the_save(
    tmp_path: Path, drawing: Drawing
) -> None:
    ledger, _ = _judged(14)
    drawing.line = STREET_LINE

    result = _tune(tmp_path, ledger, "--save")

    assert result.exit_code == 1
    assert "not saved: re-drawn, the vector lands on the line the runner chose 0" in result.output
    assert not (tmp_path / "profile.yaml").exists()


def test_too_little_evidence_is_refused_before_a_single_route_is_drawn(
    tmp_path: Path, drawing: Drawing
) -> None:
    ledger, _ = _judged(3)

    result = _tune(tmp_path, ledger, "--save")

    assert result.exit_code == 1
    assert "not saved: 3 pair(s) from a person; 12 are needed" in result.output
    assert drawing.models == []


def test_reroute_alone_reports_and_saves_nothing(tmp_path: Path, drawing: Drawing) -> None:
    ledger, _ = _judged(14)

    result = _tune(tmp_path, ledger, "--reroute")

    assert result.exit_code == 0, result.output
    assert "re-drawn 14 pair(s)" in result.output and "passes" in result.output
    assert not (tmp_path / "profile.yaml").exists()
