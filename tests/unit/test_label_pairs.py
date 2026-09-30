"""Preference pairs: which questions are worth asking, and what an answer becomes (ADR 0049).

The router is a fake that draws canned lines per vector, so what is pinned is the logic a
live graph cannot isolate: identical lines dropped, a question per stratum, A and B never
decided by the vector, an answer that names no line kept but never made a pair, and a
first answer that stands. Every ledger here is under `tmp_path`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from longrun.cli.main import app
from longrun.core.labels.answers import (
    AnswerRefused,
    next_task,
    probe_agreement,
    record_pair_answer,
)
from longrun.core.labels.ledger import AlreadyAnswered, Ledger, labels_dir_from_env
from longrun.core.labels.pairs import (
    Candidate,
    PairAnswer,
    PairTask,
    answer_problems,
    class_difference_m,
    distinct,
    order,
    pair_from,
    probe,
    split_fraction,
    stratum,
)
from longrun.core.labels.pairs_io import read_pairs
from longrun.core.labels.seeding import SEED_VECTORS, seed_request, shared_fraction
from longrun.core.models.geometry import LatLon, Route, RoutePoint
from longrun.core.routing.base import RouterUnavailable
from longrun.core.routing.tuning import RouteSummary, WayClass

NOW = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)
START = LatLon(lat=37.8300, lon=-122.5300)
END = LatLon(lat=37.8300, lon=-122.4900)

PAVED_STREET = WayClass(lts=2)
TRAIL = WayClass(lts=None, unpaved=True, is_path=True)
FIRE_ROAD = WayClass(lts=None, unpaved=True)


def _line(route_id: str, bulge: float) -> Route:
    """A line from START to END, bowed north by `bulge` degrees at its middle."""
    points = []
    for i in range(41):
        t = i / 40
        points.append(
            (START.lat + bulge * (1 - abs(2 * t - 1)), START.lon + t * (END.lon - START.lon))
        )
    from longrun.core.geo.gpx import haversine_m

    cum, out = 0.0, []
    for index, (lat, lon) in enumerate(points):
        if index:
            cum += haversine_m(points[index - 1][0], points[index - 1][1], lat, lon)
        out.append(RoutePoint(lat=lat, lon=lon, cum_dist_m=cum))
    return Route(id=route_id, points=out)


def _summary(name: str, *ways: tuple[WayClass, float]) -> RouteSummary:
    return RouteSummary.from_ways(name, list(ways))


#: What each vector "draws": the neutral line on streets, a trail line far to the north, a
#: fire-road line further still, and three vectors that land on the neutral line again.
LINES: dict[str, list[tuple[Route, RouteSummary]]] = {
    "neutral": [
        (_line("neutral", 0.0), _summary("neutral", (PAVED_STREET, 3500.0))),
        # The router's own alternative: the same mixture of classes, somewhere else.
        (_line("neutral-alt1", 0.004), _summary("neutral-alt1", (PAVED_STREET, 3600.0))),
    ],
    "avoid-stress": [
        (_line("avoid-stress", 0.0), _summary("avoid-stress", (PAVED_STREET, 3500.0)))
    ],
    "seek-paths": [(_line("seek-paths", 0.01), _summary("seek-paths", (TRAIL, 4200.0)))],
    "shun-paths": [(_line("shun-paths", 0.0), _summary("shun-paths", (PAVED_STREET, 3500.0)))],
    "seek-unpaved": [(_line("seek-unpaved", 0.02), _summary("seek-unpaved", (FIRE_ROAD, 4800.0)))],
    "shun-unpaved": [
        (_line("shun-unpaved", 0.0), _summary("shun-unpaved", (PAVED_STREET, 3500.0)))
    ],
}


class FakeRouter:
    """Draws `LINES[name]` for the vector named in the call; fails the ones it is told to."""

    def __init__(self, *, fail: set[str] | None = None, lines: Any = None) -> None:
        self.fail = fail or set()
        self.lines = lines or LINES
        self.calls: list[dict[str, Any]] = []

    def route_summaries(self, waypoints: Any, **kwargs: Any) -> list[tuple[Route, RouteSummary]]:
        self.calls.append(kwargs)
        name = kwargs["name"]
        if name in self.fail:
            raise RouterUnavailable(f"{name} failed")
        return list(self.lines[name])


def _ids() -> Any:
    counter = count(1)
    return lambda: f"{next(counter):012x}"


def _seed(router: Any = None, **kwargs: Any) -> Any:
    return seed_request(
        router or FakeRouter(),
        (START, END),
        region="bayarea",
        new_id=_ids(),
        created_at=NOW,
        **kwargs,
    )


# --- what counts as two different lines -------------------------------------------


def test_a_line_shares_all_of_itself_and_none_of_a_distant_one() -> None:
    assert shared_fraction(_line("a", 0.0), _line("b", 0.0)) == 1.0
    assert shared_fraction(_line("a", 0.0), _line("b", 0.02)) < 0.2


def test_two_lines_of_one_mixture_are_not_a_question_wherever_they_go() -> None:
    a = _summary("a", (PAVED_STREET, 3500.0))
    b = _summary("b", (PAVED_STREET, 3600.0))

    assert class_difference_m(a, b) == 50.0
    assert not distinct(0.1, a, b)


def test_two_mixtures_on_one_line_are_not_a_question_either() -> None:
    a = _summary("a", (PAVED_STREET, 3500.0))
    b = _summary("b", (TRAIL, 3500.0))

    assert not distinct(0.95, a, b)
    assert distinct(0.5, a, b)


def test_the_stratum_is_the_parameter_the_lines_differ_on_most() -> None:
    streets = _summary("s", (PAVED_STREET, 3500.0))

    assert stratum(streets, _summary("t", (TRAIL, 4000.0))) in ("unpaved", "path_bonus")
    assert stratum(streets, _summary("f", (FIRE_ROAD, 4000.0))) == "unpaved"
    assert stratum(streets, _summary("h", (WayClass(lts=4), 3500.0))) in ("lts2", "lts4")


def test_the_split_of_a_pair_and_its_mirror_add_up_to_everyone() -> None:
    a = _summary("a", (PAVED_STREET, 3500.0))
    b = _summary("b", (TRAIL, 4000.0))

    assert split_fraction(a, b) + split_fraction(b, a) == pytest.approx(1.0)
    assert split_fraction(a, a) == 0.5, "a tie is half a vote"


# --- seeding one request ---------------------------------------------------------


def test_identical_lines_are_dropped_and_the_distinct_ones_become_questions() -> None:
    outcome = _seed()

    assert outcome.lines == 7
    # neutral, seek-paths, seek-unpaved: every other vector drew the neutral line again, and
    # neutral-alt1 is the same mixture of classes somewhere else.
    assert outcome.distinct_lines == 3
    assert len(outcome.tasks) == 2
    assert len({task.stratum for task in outcome.tasks}) == 2, "one question per stratum"


def test_which_line_is_a_never_follows_from_the_vector_that_drew_it() -> None:
    """Blind: A and B are ordered by candidate id, which is random at seed time."""
    for task in _seed().tasks:
        assert task.a.id < task.b.id


def test_a_task_records_where_the_router_started_not_where_the_sample_fell() -> None:
    (task, *_) = _seed().tasks

    assert task.ends[0] == (round(START.lat, 6), round(START.lon, 6))
    assert task.region == "bayarea"


def test_an_end_the_router_moved_to_another_shore_is_not_a_question() -> None:
    far = {
        name: [
            (
                _line(route.id, 0.0).model_copy(
                    update={
                        "points": [p.model_copy(update={"lat": p.lat + 0.01}) for p in route.points]
                    }
                ),
                summary,
            )
            for route, summary in lines
        ]
        for name, lines in LINES.items()
    }

    outcome = _seed(FakeRouter(lines=far))

    assert outcome.tasks == []
    assert "moved an end" in outcome.notes[0]


def test_a_vector_the_router_refuses_is_noted_and_the_rest_still_asked() -> None:
    outcome = _seed(FakeRouter(fail={"seek-unpaved"}))

    assert any("seek-unpaved" in note for note in outcome.notes)
    assert len(outcome.tasks) == 1


def test_the_neutral_line_is_asked_for_with_the_routers_own_alternatives() -> None:
    router = FakeRouter()
    _seed(router)

    by_name = {call["name"]: call for call in router.calls}
    assert set(by_name) == set(SEED_VECTORS)
    assert by_name["neutral"]["alternatives"] > 0
    assert all(call["alternatives"] == 0 for name, call in by_name.items() if name != "neutral")


# --- the queue --------------------------------------------------------------------


def _task(task_id: str, kind: str, split: float, repeat_of: str | None = None) -> PairTask:
    route = _line("r", 0.0)
    a = Candidate.of("c" + task_id + "a", "neutral", route, _summary("a", (PAVED_STREET, 3500.0)))
    b = Candidate.of("c" + task_id + "b", "seek-paths", route, _summary("b", (TRAIL, 4000.0)))
    return PairTask(
        id=task_id,
        region="bayarea",
        ends=((0.0, 0.0), (0.0, 0.0)),
        a=a,
        b=b,
        stratum=kind,
        split=split,
        repeat_of=repeat_of,
        created_at=NOW,
    )


def test_a_session_asks_across_strata_and_the_closest_split_first() -> None:
    tasks = [
        _task("t1", "unpaved", 0.9),
        _task("t2", "unpaved", 0.5),
        _task("t3", "path_bonus", 0.6),
        _task("t4", "path_bonus", 0.99),
    ]

    assert [t.id for t in order(tasks)] == ["t3", "t2", "t4", "t1"]


def test_a_probe_is_asked_after_everything_else() -> None:
    original = _task("t1", "unpaved", 0.5)
    mirror = probe(original, "p1", NOW)

    assert [t.id for t in order([mirror, original, _task("t2", "unpaved", 0.9)])] == [
        "t1",
        "t2",
        "p1",
    ]
    assert (mirror.a, mirror.b) == (original.b, original.a)
    assert mirror.split == pytest.approx(1 - original.split)


def test_what_is_shown_after_an_answer_is_shares_of_the_line() -> None:
    shares = _task("t1", "unpaved", 0.5).b.shares()

    assert shares == {"lts3_plus": 0.0, "unpaved": 1.0, "path": 1.0}


# --- answers ----------------------------------------------------------------------


def test_a_choice_needs_a_strength_and_a_non_choice_refuses_one() -> None:
    assert answer_problems("a", None) == ["say how strongly: slight or clear"]
    assert answer_problems("none", "clear") != []
    assert answer_problems("maybe", None) != []
    assert answer_problems("b", "slight") == answer_problems("cant", None) == []


def _answer(task: PairTask, prefer: str, strength: str | None = "clear") -> PairAnswer:
    return PairAnswer(
        task_id=task.id,
        prefer=prefer,  # type: ignore[arg-type]
        strength=strength,  # type: ignore[arg-type]
        a_id=task.a.id,
        b_id=task.b.id,
        labeler="runner-1",
        labelled_at=NOW,
        via="cli",
    )


def test_a_choice_becomes_a_judged_pair_weighted_by_strength() -> None:
    task = _task("t1", "unpaved", 0.5)

    clear = pair_from(task, _answer(task, "b"))
    slight = pair_from(task, _answer(task, "a", "slight"))

    assert clear is not None and slight is not None
    assert (clear.preferred, clear.source, clear.weight, clear.labeler) == (
        "b",
        "judged",
        1.0,
        "runner-1",
    )
    assert slight.weight == 0.5
    assert clear.a.name == task.a.id, "a published pair is named by id, never by vector"


def test_no_preference_is_kept_and_is_not_a_pair() -> None:
    task = _task("t1", "unpaved", 0.5)

    assert pair_from(task, _answer(task, "none", None)) is None
    assert pair_from(task, _answer(task, "cant", None)) is None


def test_an_answer_lands_in_the_ledger_and_a_choice_is_a_pair_tune_reads(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    chosen, skipped = _task("t1", "unpaved", 0.5), _task("t2", "unpaved", 0.5)
    ledger.add_task(chosen)
    ledger.add_task(skipped)

    record_pair_answer(
        ledger,
        "t1",
        prefer="b",
        strength="clear",
        reason="  ",
        labeler="r1",
        labelled_at=NOW,
        via="cli",
    )
    record_pair_answer(
        ledger,
        "t2",
        prefer="none",
        strength=None,
        reason=None,
        labeler="r1",
        labelled_at=NOW,
        via="cli",
    )

    assert [a.task_id for a in ledger.answers()] == ["t1", "t2"]
    assert ledger.answer("t1").reason is None  # type: ignore[union-attr]
    (pair,) = read_pairs(ledger.pairs_dir)
    assert (pair.preferred, pair.source, pair.labeler) == ("b", "judged", "r1")


def test_a_first_answer_stands(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    ledger.add_task(_task("t1", "unpaved", 0.5))
    record_pair_answer(
        ledger,
        "t1",
        prefer="a",
        strength="clear",
        reason=None,
        labeler="r1",
        labelled_at=NOW,
        via="cli",
    )

    with pytest.raises(AlreadyAnswered):
        record_pair_answer(
            ledger,
            "t1",
            prefer="b",
            strength="clear",
            reason=None,
            labeler="r1",
            labelled_at=NOW,
            via="cli",
        )
    assert ledger.answer("t1").prefer == "a"  # type: ignore[union-attr]


def test_an_answer_to_no_task_is_refused_in_words(tmp_path: Path) -> None:
    with pytest.raises(AnswerRefused, match="no task"):
        record_pair_answer(
            Ledger(tmp_path),
            "../t1",
            prefer="a",
            strength="clear",
            reason=None,
            labeler="r1",
            labelled_at=NOW,
            via="cli",
        )


def test_probes_agree_when_the_letter_flips(tmp_path: Path) -> None:
    original = _task("t1", "unpaved", 0.5)
    mirror = probe(original, "p1", NOW)
    tasks = [original, mirror]

    agree = [_answer(original, "a"), _answer(mirror, "b")]
    disagree = [_answer(original, "a"), _answer(mirror, "a")]

    assert probe_agreement(tasks, agree) == (1, 1)
    assert probe_agreement(tasks, disagree) == (0, 1)
    assert probe_agreement(tasks, agree[:1]) == (0, 0), "an unanswered probe is not counted"


def test_the_next_task_skips_what_is_answered(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    for task in (_task("t1", "unpaved", 0.5), _task("t2", "path_bonus", 0.9)):
        ledger.add_task(task)
    record_pair_answer(
        ledger,
        "t1",
        prefer="cant",
        strength=None,
        reason=None,
        labeler="r1",
        labelled_at=NOW,
        via="cli",
    )

    assert next_task(ledger).id == "t2"  # type: ignore[union-attr]


# --- the CLI ----------------------------------------------------------------------


@pytest.fixture
def fake_router(monkeypatch: pytest.MonkeyPatch) -> FakeRouter:
    router = FakeRouter()
    monkeypatch.setattr("longrun.core.routing.graphhopper.GraphHopperRouter", lambda url: router)

    class _Health:
        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: _Health())
    return router


def test_seed_queues_questions_and_probes_from_a_file_of_ends(
    fake_router: FakeRouter, tmp_path: Path
) -> None:
    requests = tmp_path / "ends.yaml"
    requests.write_text(
        yaml.safe_dump([{"from": f"{START.lat},{START.lon}", "to": f"{END.lat},{END.lon}"}]),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app, ["label", "seed", "--requests", str(requests), "--router", "http://x", "--repeat", "1"]
    )

    assert result.exit_code == 0, result.output
    assert "queued 2 question(s)" in result.output and "plus 1 probe(s)" in result.output
    tasks = Ledger(labels_dir_from_env()).tasks()
    assert len(tasks) == 3 and sum(1 for t in tasks if t.repeat_of) == 1


def test_seed_needs_to_be_told_where(fake_router: FakeRouter) -> None:
    result = CliRunner().invoke(app, ["label", "seed"])

    assert result.exit_code == 2
    assert "say where" in result.output


def test_pair_records_an_answer_as_the_labelers(
    fake_router: FakeRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = Ledger(labels_dir_from_env())
    ledger.add_task(_task("t1", "unpaved", 0.5))
    monkeypatch.setenv("LONGRUN_LABELER", "runner-1")

    first = CliRunner().invoke(app, ["label", "pair", "t1", "--prefer", "b", "--strength", "clear"])
    again = CliRunner().invoke(app, ["label", "pair", "t1", "--prefer", "a", "--strength", "clear"])

    assert first.exit_code == 0, first.output
    assert "a pair `tune` will read" in first.output
    assert again.exit_code == 2 and "a first answer stands" in again.output
    (answer,) = ledger.answers()
    assert (answer.prefer, answer.via, answer.labeler) == ("b", "cli", "runner-1")


def test_pair_refuses_without_a_labeler_and_without_a_strength(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Ledger(labels_dir_from_env()).add_task(_task("t1", "unpaved", 0.5))

    nobody = CliRunner().invoke(
        app, ["label", "pair", "t1", "--prefer", "a", "--strength", "clear"]
    )
    monkeypatch.setenv("LONGRUN_LABELER", "runner-1")
    weak = CliRunner().invoke(app, ["label", "pair", "t1", "--prefer", "a"])

    assert nobody.exit_code == 2 and "LONGRUN_LABELER" in nobody.output
    assert weak.exit_code == 2 and "slight or clear" in weak.output
    assert Ledger(labels_dir_from_env()).answers() == []


def test_status_counts_pairs_the_way_tune_will_see_them(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = Ledger(labels_dir_from_env())
    for task_id in ("t1", "t2"):
        ledger.add_task(_task(task_id, "unpaved", 0.5))
    record_pair_answer(
        ledger,
        "t1",
        prefer="a",
        strength="clear",
        reason=None,
        labeler="r1",
        labelled_at=NOW,
        via="cli",
    )
    record_pair_answer(
        ledger,
        "t2",
        prefer="none",
        strength=None,
        reason=None,
        labeler="r1",
        labelled_at=NOW,
        via="cli",
    )

    result = CliRunner().invoke(app, ["label", "status"])

    assert "2 answered - 1 chose a line, 1 no preference" in result.output
    assert "no-preference rate 50%" in result.output


# --- publishing, and tune reading what was published (M23.7) -------------------------


def _answered_ledger() -> Ledger:
    """A choice, a "no preference", and a probe of the choice - only the first is a pair."""
    ledger = Ledger(labels_dir_from_env())
    chosen, shrug = _task("t1", "unpaved", 0.5), _task("t2", "unpaved", 0.5)
    for task in (chosen, shrug, probe(chosen, "p1", NOW)):
        ledger.add_task(task)
    for task_id, prefer, strength in (
        ("t1", "b", "clear"),
        ("t2", "none", None),
        ("p1", "a", "clear"),
    ):
        record_pair_answer(
            ledger,
            task_id,
            prefer=prefer,
            strength=strength,
            reason=None,
            labeler="r1",
            labelled_at=NOW,
            via="cli",
        )
    return ledger


def test_a_probe_is_never_a_second_pair() -> None:
    ledger = _answered_ledger()

    assert [path.stem for path in ledger.pairs_dir.glob("*.yaml")] == ["t1"]


def test_publish_writes_a_choice_as_numbers_and_nothing_else(tmp_path: Path) -> None:
    _answered_ledger()
    pairs = tmp_path / "pairs"

    result = CliRunner().invoke(
        app, ["label", "publish", "--cases", str(tmp_path / "cases"), "--pairs", str(pairs)]
    )

    assert result.exit_code == 0, result.output
    assert "0 case(s) and 1 pair(s)" in result.output
    (published,) = pairs.glob("*.yaml")
    assert published.name == "judged-r1-t1.yaml"
    text = published.read_text(encoding="utf-8")
    for leak in ("lat", "lon", "points", "bayarea", "neutral", "seek-paths", "drawn_by"):
        assert leak not in text, f"a published pair carries {leak!r}"
    (pair,) = read_pairs(pairs)
    assert (pair.preferred, pair.source, pair.labeler) == ("b", "judged", "r1")


def test_tune_fits_one_runners_published_pairs_and_holds_some_out(tmp_path: Path) -> None:
    _answered_ledger()
    pairs = tmp_path / "pairs"
    CliRunner().invoke(
        app, ["label", "publish", "--cases", str(tmp_path / "c"), "--pairs", str(pairs)]
    )

    mine = CliRunner().invoke(
        app, ["tune", "--pairs", str(pairs), "--labeler", "r1", "--holdout", "5"]
    )
    theirs = CliRunner().invoke(app, ["tune", "--pairs", str(pairs), "--labeler", "someone-else"])

    assert "1 real pair(s)" in mine.output
    assert "evidence, metres of difference" in mine.output
    assert "too few pairs from a person to hold any out" in mine.output
    assert "0 real pair(s)" in theirs.output
