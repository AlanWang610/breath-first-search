"""Route regression: what the scorers say about a whole route, pinned (scope 10.1, 11).

The unit tests pin each scorer in isolation against a corridor built for it. Nothing else
pins the *pipeline*: a change to segmentation, position weighting or arbitration alters
what every scorer reports about a real route while leaving every unit test green. That
gap is what these tests close.

`expected.json` is the record of what the scorers used to say. Diffs in it are reviewed,
never blanket-regenerated — `--update-golden` exists to write a reviewed change back, not
to make a failure go away.

    uv run pytest -m golden
    uv run pytest -m golden --update-golden      # then read the diff before committing
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest
from tests.golden import harness
from tests.golden.expectation import describe_difference, digest, serialize

pytestmark = pytest.mark.golden

ROUTE_NAMES = harness.route_names()


def test_the_golden_suite_is_not_empty() -> None:
    """An empty suite passes silently, which is how M1 shipped without one.

    Every other test here is parametrized over the routes on disk, so with no routes the
    whole file reports green and says nothing. This is the assertion that made the gap
    visible, and it stays.
    """
    assert ROUTE_NAMES, (
        "no golden routes under tests/golden/routes/ - the golden suite is the only "
        "end-to-end regression record there is"
    )


@pytest.mark.parametrize("name", ROUTE_NAMES)
def test_the_route_directory_is_complete(name: str) -> None:
    """A missing pin is a run that is not reproducible, so it fails before it runs."""
    directory = harness.ROUTES_DIR / name
    missing = [f for f in harness.REQUIRED_FILES if not (directory / f).exists()]
    assert not missing, f"{name} is missing {', '.join(missing)}"


@pytest.mark.parametrize("name", ROUTE_NAMES)
def test_the_request_pins_an_absolute_date(name: str) -> None:
    """A relative date can never be re-run.

    A forecast for a past date cannot be re-fetched, so a golden's date is frozen forever
    and must be written down rather than computed at run time.
    """
    request = harness.load_request(harness.ROUTES_DIR / name)
    assert isinstance(request.get("date"), date), (
        f"{name}: pin the date as a YYYY-MM-DD literal - a string or an expression is "
        "how a golden ends up depending on when it was run"
    )
    assert re.fullmatch(r"\d{2}:\d{2}", str(request.get("start", ""))), (
        f"{name}: pin the start time as HH:MM rather than inheriting the CLI default"
    )


@pytest.mark.parametrize("name", ROUTE_NAMES)
def test_the_route_matches_its_expectation(name: str, tmp_path: Path, update_golden: bool) -> None:
    directory = harness.ROUTES_DIR / name
    run = harness.run(directory, tmp_path / "out")
    actual = digest(run.plan)
    expected_path = directory / "expected.json"

    if update_golden:
        expected_path.write_text(serialize(actual), encoding="utf-8")
        pytest.skip(f"rewrote {expected_path.relative_to(harness.ROUTES_DIR.parent.parent)}")

    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    assert actual == expected, (
        f"{name} no longer scores the way it used to.\n\n"
        f"{describe_difference(expected, actual)}\n"
        "If this change is intended, rerun with --update-golden and review the diff."
    )


@pytest.mark.parametrize("name", ROUTE_NAMES)
def test_the_sheet_reports_what_was_not_checked(name: str, tmp_path: Path) -> None:
    """Scope 3.6 in the one place a user actually reads.

    Every golden runs with scorers that do not exist yet, so the honest sheet always has
    something in its unchecked list. The day that stops being true this assertion should
    be revisited, not deleted.
    """
    run = harness.run(harness.ROUTES_DIR / name, tmp_path / "out")
    assert run.plan.coverage.unchecked(), "a plan that checked everything is not credible yet"
    assert "Coverage" in run.sheet
    for entry in run.plan.coverage.unchecked()[:3]:
        assert entry.source in run.sheet


@pytest.mark.parametrize("name", ROUTE_NAMES)
def test_the_expectation_is_machine_independent(name: str) -> None:
    """No absolute paths, no home directories, no drive letters.

    A `LayerNotFound` names the directory it looked in, and interpolating one into a
    coverage reason puts an absolute path into `expected.json` — which passes on the
    machine that wrote it and fails on every other, CI included. Found exactly that way.
    """
    text = (harness.ROUTES_DIR / name / "expected.json").read_text(encoding="utf-8")
    for pattern in (r"[A-Za-z]:\\\\", r"/home/", r"/Users/", r"\\Users\\\\"):
        assert not re.search(pattern, text), f"{name}: machine-specific path matching {pattern}"


@pytest.mark.parametrize("name", ROUTE_NAMES)
def test_no_scorer_emits_two_measurements_with_the_same_id(name: str, tmp_path: Path) -> None:
    """A duplicate `segment_id` is a measurement silently lost, and nothing else notices.

    Most scorers cannot produce one: their ids come from the segment list, which partitions
    the route. Three do not. `ROUTE_SUMMARY_ID` established that a `segment_id` may name
    something other than a segment, and `crew_points` and `start_time_optimizer` followed —
    a place beside the route, and the whole route at an hour.

    `crew_points` got it wrong on the first real corridor: three car parks beside a city
    start all project to 0.0 km along the route, so an id built from the distance was
    `meet@0.0km` three times and two of them vanished into the sheet and the content hash.
    This is the check that would have caught it, and it applies to every scorer at once
    rather than to the one that happened to break.
    """
    run = harness.run(harness.ROUTES_DIR / name, tmp_path / "out")
    for result in run.plan.results:
        ids = [m.segment_id for m in result.measurements]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        assert not duplicates, f"{name}: {result.name} repeats {duplicates}"
