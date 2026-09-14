"""Plans as jobs: progress, parking, and coming back to a job in another process.

Scope 4.2's bet, tested where it is actually risky. The loop's own pause and resume are
covered in `test_loop.py` against a function call; what is covered here is the part that
crosses a boundary - a scratchpad written by one worker and read by another.

The runner is always shut down. `conftest._block_network` patches `socket.connect` and
restores it at teardown, so a worker that outlives its test runs with the network
unblocked - which would make a later failure look like a flake in whatever ran next.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from longrun.core.models.request import PlanRequest
from longrun.core.plan.scratchpad import Scratchpad
from longrun.jobs.runner import JobRunner, JobStore, Reporter


def _pad(job_id: str | None = None, status: str = "complete") -> Scratchpad:
    return Scratchpad(
        plan_id="p1",
        job_id=job_id,
        request=PlanRequest(mode="repair", date=date(2026, 3, 15)),
        status=status,  # type: ignore[arg-type]
    )


@pytest.fixture
def runner(tmp_path: Path):  # type: ignore[no-untyped-def]
    with JobRunner(JobStore(tmp_path / "jobs")) as started:
        yield started


def test_a_job_runs_and_is_waited_on(runner: JobRunner) -> None:
    job_id = runner.submit(lambda report: _pad())

    pad = runner.wait(job_id, timeout=10)

    assert pad.status == "complete"
    assert runner.status(job_id) == "complete"


def test_progress_is_reported_in_order(runner: JobRunner) -> None:
    """Scope 4.4: "async jobs with progress events". A job that only says whether it is
    finished is a job nobody can watch."""

    def work(report: Reporter) -> Scratchpad:
        report("scoring round 1")
        report("scoring round 2")
        return _pad()

    job_id = runner.submit(work)
    runner.wait(job_id, timeout=10)

    kinds = [event.kind for event in runner.events(job_id)]
    messages = [event.message for event in runner.events(job_id)]
    assert kinds == ["started", "progress", "progress", "complete"]
    assert messages[1:3] == ["scoring round 1", "scoring round 2"]


def test_a_job_that_raises_is_a_state_and_not_a_crash(runner: JobRunner) -> None:
    """And the event names the exception type, because "failed" alone is not actionable -
    the rule `unavailable` applies to a missing data source, applied to a worker."""

    def work(report: Reporter) -> Scratchpad:
        raise RuntimeError("the router went away")

    job_id = runner.submit(work)
    with pytest.raises(RuntimeError):
        runner.wait(job_id, timeout=10)

    assert runner.status(job_id) == "failed"
    assert runner.events(job_id)[-1].kind == "failed"
    assert "RuntimeError: the router went away" in runner.events(job_id)[-1].message


def test_a_parked_job_is_stored_where_another_process_can_find_it(
    runner: JobRunner, tmp_path: Path
) -> None:
    """The whole of scope 4.2: the pause is a persisted scratchpad, not a special
    control flow, and "persisted" has to mean on disk or the claim is empty."""
    job_id = runner.submit(lambda report: _pad(status="needs_input"))
    runner.wait(job_id, timeout=10)

    assert runner.status(job_id) == "needs_input"
    assert runner.events(job_id)[-1].kind == "needs_input"

    # Read back through a *different* store object, which is the closest a test gets to a
    # different process without being one.
    reread = JobStore(tmp_path / "jobs").load(job_id)
    assert reread.status == "needs_input"
    assert reread.job_id == job_id


def test_a_job_id_is_stamped_onto_what_is_stored(runner: JobRunner, tmp_path: Path) -> None:
    """Otherwise the file on disk cannot say which job wrote it, and `JobStore.ids()`
    returns names nothing can be matched back to."""
    job_id = runner.submit(lambda report: _pad())
    runner.wait(job_id, timeout=10)

    assert JobStore(tmp_path / "jobs").ids() == [job_id]


def test_a_scratchpad_with_no_job_id_is_refused_rather_than_filed_under_none(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "jobs")
    with pytest.raises(ValueError, match="job_id"):
        store.save(_pad())


def test_asking_about_a_job_that_does_not_exist_says_so(runner: JobRunner) -> None:
    with pytest.raises(KeyError, match="no such job"):
        runner.status("nope")


def test_two_jobs_run_without_sharing_anything(runner: JobRunner) -> None:
    """Concurrency belongs *between* jobs. Within one, candidates are sequential, because
    `Budget.spend_api_call` is load-add-store and `SqliteCache` shares one connection."""
    first = runner.submit(lambda report: _pad())
    second = runner.submit(lambda report: _pad())

    assert runner.wait(first, timeout=10).status == "complete"
    assert runner.wait(second, timeout=10).status == "complete"
    assert first != second
    assert sorted(runner.ids()) == sorted([first, second])
