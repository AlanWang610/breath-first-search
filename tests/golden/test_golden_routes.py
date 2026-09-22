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


#: Total bytes the committed golden fixtures may occupy.
#:
#: The number was in the build plan from M0 and enforced by **nothing** - not a test, not a CI
#: step. What stood in for it was M0's 15-minute CI timeout, on the reasoning that "keeping CI
#: short is itself the enforcement mechanism for small fixtures"; M6 raised that to 30 when the
#: fifth golden pushed the Windows job over. So the only mechanism was relaxed by the milestone
#: that discovered it, and M9 finally made the cap a test - at which point the suite was already
#: at 48.50 MB of 50, with 1.50 MB of headroom and no room for a seventh route.
#:
#: **Raised to 64 MB in M10, deliberately and by decision rather than by pressure** (ADR 0032).
#: Three things about that are worth stating where somebody will read them:
#:
#: * M10 spends almost none of it. Waypoints added ~2 kB across six `expected.json` files, and
#:   the cue sheet records no fixture at all. The raise is provision for M11-M13, not a need of
#:   the milestone that made it.
#: * 64 rather than a round 75 because `boston-winter` at 8.0 MB is the model for a dense new
#:   route, so this leaves room for about two more - and past that the CI job binds first.
#: * **Disk is not the constraint that bites; CI wall clock is.** The Windows job ran 26m2s
#:   against a 30-minute per-job timeout after M9, and every golden test reads these
#:   GeoPackages. Raising the byte cap does nothing about that, and a future milestone that
#:   finds itself here again should look at the clock before it looks at this number.
MAX_FIXTURE_BYTES = 64 * 1024 * 1024


def test_the_committed_fixtures_stay_under_the_cap() -> None:
    """The cap the plan has claimed since M0, asserted for the first time.

    Fails with the per-route breakdown rather than one number, because the answer to being over
    is always "which route, and which layer in it".
    """
    routes = Path(__file__).parent / "routes"
    sizes = {
        directory.name: sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())
        for directory in sorted(routes.iterdir())
        if directory.is_dir()
    }
    total = sum(sizes.values())
    breakdown = "\n".join(f"  {n:<20} {b / 1024 / 1024:6.1f} MB" for n, b in sorted(sizes.items()))
    assert total <= MAX_FIXTURE_BYTES, (
        f"golden fixtures are {total / 1024 / 1024:.1f} MB, over the "
        f"{MAX_FIXTURE_BYTES / 1024 / 1024:.0f} MB cap:\n{breakdown}\n"
        "Drop a layer a route does not exercise and record the choice in its README, or "
        "VACUUM the cassettes — do not raise the cap without deciding to."
    )


@pytest.mark.parametrize("name", ROUTE_NAMES)
def test_the_route_directory_is_complete(name: str) -> None:
    """A missing pin is a run that is not reproducible, so it fails before it runs."""
    directory = harness.ROUTES_DIR / name
    missing = [f for f in harness.required_files(directory) if not (directory / f).exists()]
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
def test_the_route_matches_its_expectation(
    name: str, golden_run: harness.GoldenRunner, update_golden: bool
) -> None:
    directory = harness.ROUTES_DIR / name
    run = golden_run(name)
    actual = digest(run.plan, run.scratchpad)
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

    # Folded in here rather than given its own parametrized test. There are already six
    # route-parametrized tests over six routes, so each new one is another six full pipeline
    # runs - and the Windows CI job was at 26m2s of a 30-minute cap after M9.
    #
    # The GPX is invisible to `digest`, which reads `plan.json` alone. So this is the only
    # thing standing between a plan that carries waypoints and a GPX that drops them, which
    # is precisely the failure scope 9's first output has had since M1: `gpx_write` took a
    # `waypoints` argument for nine milestones and no caller ever passed one.
    assert run.gpx, f"{name} wrote no course.gpx"
    assert run.gpx.count("<wpt") == len(run.plan.waypoints), (
        f"{name} carries {len(run.plan.waypoints)} waypoints and its GPX has "
        f"{run.gpx.count('<wpt')}"
    )


@pytest.mark.parametrize("name", ROUTE_NAMES)
def test_the_sheet_reports_what_was_not_checked(
    name: str, golden_run: harness.GoldenRunner
) -> None:
    """Scope 3.6 in the one place a user actually reads.

    Every golden runs with scorers that do not exist yet, so the honest sheet always has
    something in its unchecked list. The day that stops being true this assertion should
    be revisited, not deleted.
    """
    run = golden_run(name)
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
def test_no_scorer_emits_two_measurements_with_the_same_id(
    name: str, golden_run: harness.GoldenRunner
) -> None:
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
    run = golden_run(name)
    for result in run.plan.results:
        ids = [m.segment_id for m in result.measurements]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        assert not duplicates, f"{name}: {result.name} repeats {duplicates}"


@pytest.mark.parametrize("name", ROUTE_NAMES)
def test_every_recorded_source_has_a_licence(name: str, golden_run: harness.GoldenRunner) -> None:
    """Scope 14 is an obligation, and `LICENCE NOT RECORDED` is what an unmet one looks like.

    Twice now a source has reached a real sheet without a licence row and nothing failed.
    M3 found it for OSM - scorers record the *layer* they read and attribution is owed by
    *source* - and M4 found `dem`, which has printed `LICENCE NOT RECORDED` for USGS 3DEP on
    every sheet since M2, because `_layer_sources()` derives from `DEFAULT_LAYER_TABLES` and
    that table is vector only. A golden expectation does not carry the attribution block, so
    neither incident showed up as a diff. This reads the sheet.
    """
    run = golden_run(name)

    # The positive control, and it is not ceremony. The assertion below is a bare `not in`,
    # so it passes on an empty string - found by sabotage on 2026-09-22: blanking
    # `GoldenRun.sheet` failed the other sheet-reading test on all six routes and this one
    # on none. A negative assertion with nothing establishing that there was anything to
    # search is a test that can only ever fail for the right reason by luck.
    assert "## Attribution" in run.sheet, "the sheet carries no attribution section at all"

    assert "LICENCE NOT RECORDED" not in run.sheet, (
        "a source reached the plan sheet with no scope 14 licence row; add it to "
        "attribution.LICENCES or map it in DERIVED_SOURCES"
    )


def test_the_opening_route_key_still_matches_the_recorded_cassette() -> None:
    """The strongest guard on the route cache key, and it costs nothing.

    `loop-bayarea` replays because `cli/plan.py`'s opening call hashes to a key its cassette
    holds. Asserted against the committed artifact rather than against a constant a developer
    can update, and without running the route - so a re-keying is reported *as* a re-keying
    rather than as a CacheMiss five frames down inside a CLI invocation.

    M10 is the milestone that made this necessary: adding `instructions` to the key would
    have orphaned every recorded route in the repository, and the fix was to elide the field
    in its default state rather than to re-record against a graph that no longer exists.
    """
    import sqlite3

    import yaml

    from longrun.core.data.cache import args_hash
    from longrun.core.models.geometry import LatLon
    from longrun.core.models.plan import SnapshotPins
    from longrun.core.routing.cached import route_args
    from longrun.core.routing.graphhopper import route_body

    directory = harness.ROUTES_DIR / "loop-bayarea"
    request = yaml.safe_load((directory / "request.yaml").read_text(encoding="utf-8"))
    snapshot = SnapshotPins.model_validate_json(
        (directory / "snapshot.json").read_text(encoding="utf-8")
    )
    waypoints = [
        LatLon(
            lat=float(str(request[end]).split(",")[0]), lon=float(str(request[end]).split(",")[1])
        )
        for end in ("from", "to")
    ]
    key = args_hash(route_args(route_body(waypoints), waypoints, snapshot.graph_identity))

    with sqlite3.connect(directory / "cache.sqlite") as db:
        recorded = {
            row[0]
            for row in db.execute("SELECT args_hash FROM cache WHERE tool = 'graphhopper.route'")
        }
    assert key in recorded, (
        "the route cache key no longer matches what loop-bayarea recorded, so every route in "
        "every cassette has just been orphaned. A new field belongs in `route_args` only in "
        "its non-default state - see the comment there."
    )
