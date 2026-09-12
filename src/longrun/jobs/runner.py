"""Plans run as jobs, so one execution serves the CLI, the MCP server and the web UI.

Scope 4.4 says "plans run as async jobs with progress events" and names no mechanism.
**Threads, not asyncio** (ADR 0016): `core/` is a hundred-odd files of synchronous code,
MCP 2.x already runs a sync tool through `anyio.to_thread.run_sync`, and an async
conversion would touch every call site to buy one.

**One job is one thread, and candidates are scored sequentially inside it.** Not an
implementation detail. `Budget.spend_api_call` is load-add-store and `SqliteCache` shares
one connection with `check_same_thread=False`, so two threads can both pass the cap check
and both miss the same key - the cap stops being a cap and the cache stops being a cache.
Concurrency belongs *between* jobs, where each has its own budget and its own connection.
Parallelising the candidate sweep is the obvious optimisation and it is wrong.

**A `needs_input` pause is a state, not an exception** (scope 4.2). The scratchpad is
written to disk, the worker returns, and `resume` picks it up - possibly in a different
process, which is the boundary the whole design exists for.

This module may read the wall clock, and `core/` may not: a job's own start time is not
the plan's simulated time, and `test_layering.py` guards the latter.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from longrun.core.plan.scratchpad import Scratchpad

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator

EventKind = Literal["started", "progress", "needs_input", "complete", "failed"]


@dataclass(frozen=True)
class ProgressEvent:
    """One thing that happened, in the order it happened."""

    job_id: str
    kind: EventKind
    message: str
    at: datetime


class Reporter:
    """What a worker is handed to say where it has got to.

    A plain callable rather than a queue the worker has to know about, so the thing being
    run stays testable without a runner - the same reason `arbitrate` takes candidates
    rather than a router.
    """

    def __init__(self, job_id: str, sink: Callable[[ProgressEvent], None]) -> None:
        self.job_id = job_id
        self._sink = sink

    def __call__(self, message: str, kind: EventKind = "progress") -> None:
        self._sink(ProgressEvent(job_id=self.job_id, kind=kind, message=message, at=_now()))


class JobStore:
    """Scratchpads on disk, one file per job.

    The store *is* the scratchpad directory rather than a database: scope 4.4 says the
    scratchpad plus the manifest is the stored plan, and a second place to keep job state
    would be a second thing to keep in step with it.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def path(self, job_id: str) -> Path:
        return self.root / f"{job_id}.json"

    def save(self, pad: Scratchpad) -> Path:
        if not pad.job_id:
            raise ValueError("a scratchpad needs a job_id before it can be stored")
        return pad.save(self.path(pad.job_id))

    def load(self, job_id: str) -> Scratchpad:
        return Scratchpad.load(self.path(job_id))

    def ids(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(path.stem for path in self.root.glob("*.json"))


@dataclass
class _Job:
    job_id: str
    future: Future[Scratchpad]
    events: list[ProgressEvent] = field(default_factory=list)


class JobRunner:
    """Runs plans on a thread pool and remembers what each one said.

    `max_workers` is jobs in flight, never candidates within a job.
    """

    def __init__(self, store: JobStore, *, max_workers: int = 2) -> None:
        self.store = store
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="longrun-job")
        self._jobs: dict[str, _Job] = {}
        # Reentrant, because the accessors nest: `events` takes the lock and then asks
        # `_job`, which takes it again. A plain `Lock` deadlocks there - it did, and the
        # test suite hung rather than failed, which is the worse way to find out.
        self._lock = threading.RLock()

    # -- running ---------------------------------------------------------------

    def submit(self, work: Callable[[Reporter], Scratchpad], *, job_id: str | None = None) -> str:
        """Start a plan. Returns immediately with the id to ask about."""
        job_id = job_id or uuid.uuid4().hex[:12]
        reporter = Reporter(job_id, lambda event: self._record(job_id, event))

        def run() -> Scratchpad:
            reporter("started", kind="started")
            try:
                pad = work(reporter)
            except Exception as exc:
                # A job that died is a job state, like any other. The exception type is in
                # the message because "failed" on its own tells a reader nothing they can
                # act on - the same rule `unavailable` applies to a missing data source.
                reporter(f"{type(exc).__name__}: {exc}", kind="failed")
                raise
            pad.job_id = job_id
            self.store.save(pad)
            reporter(pad.status, kind="needs_input" if pad.status == "needs_input" else "complete")
            return pad

        with self._lock:
            self._jobs[job_id] = _Job(job_id=job_id, future=self._pool.submit(run))
        return job_id

    def resume(
        self, job_id: str, choice: str, work: Callable[[Reporter, Scratchpad], Scratchpad]
    ) -> str:
        """Answer whatever a parked job asked, and set it going again.

        The answer is written to the stored scratchpad *before* the worker starts, so the
        record on disk is what a resume reads - not something held in this process, which
        is the one thing a resume cannot count on.
        """
        from longrun.agent.loop import answer

        pad = self.store.load(job_id)
        answer(pad, choice)
        self.store.save(pad)
        return self.submit(lambda reporter: work(reporter, self.store.load(job_id)), job_id=job_id)

    # -- asking ----------------------------------------------------------------

    def wait(self, job_id: str, timeout: float | None = None) -> Scratchpad:
        return self._job(job_id).future.result(timeout=timeout)

    def events(self, job_id: str) -> list[ProgressEvent]:
        with self._lock:
            return list(self._job(job_id).events)

    def status(self, job_id: str) -> str:
        job = self._job(job_id)
        if not job.future.done():
            return "running"
        if job.future.exception() is not None:
            return "failed"
        return job.future.result().status

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._jobs)

    def shutdown(self, *, wait: bool = True) -> None:
        """Always call this.

        `conftest._block_network` patches `socket.connect` and restores it at teardown, so
        a worker that outlives the test that started it runs with the network unblocked -
        and a pool that is never shut down is exactly such a worker.
        """
        self._pool.shutdown(wait=wait)

    def __enter__(self) -> JobRunner:
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()

    # -- internals -------------------------------------------------------------

    def _record(self, job_id: str, event: ProgressEvent) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.events.append(event)

    def _job(self, job_id: str) -> _Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"no such job: {job_id}")
        return job


def _now() -> datetime:
    """Wall time, which `jobs/` may read and `core/` may not."""
    return datetime.now(UTC)


def stream(runner: JobRunner, job_id: str) -> Iterator[ProgressEvent]:
    """Everything recorded so far, oldest first. A snapshot, not a subscription."""
    yield from runner.events(job_id)


__all__ = ["EventKind", "JobRunner", "JobStore", "ProgressEvent", "Reporter", "stream"]
