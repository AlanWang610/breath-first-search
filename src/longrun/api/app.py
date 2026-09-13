"""FastAPI over the job runner (scope 10.3).

> Stack: FastAPI over the async job runner (same jobs the CLI and MCP server use), React +
> MapLibre GL, **plan schema as the API contract**. Runs against the local `tools/` server.

Three rules shape every route below, and they are the reason this milestone is last:

**No capability lives here.** Scope 3.9: every control in the UI calls a tool that already
works from the CLI. So a handler's body is argument parsing, a call into `jobs/` or
`tools/`, and a JSON response - and when one starts wanting to *compute* something, the
computation belongs in `core/` where the CLI and the MCP server can reach it too. The
layering test does not police this direction, so it is a rule kept by hand.

**The plan schema is the contract.** `GET /api/plans/{id}` returns `Plan` as pydantic dumps
it, not a hand-written view model. A second schema would drift, and the first thing to drift
would be the honest parts: `coverage`, `pacing_caveats`, `metrics.detour_ratio = null`.
Those are exactly the fields a convenience view drops.

**A pause is a state, not an error.** A job that hits a same-tier conflict returns 200 with
`status: "needs_input"` and the question, because it has not failed - it is waiting. Scope
4.2's whole design is that this survives the process, so the client that resumes a job need
not be the one that started it, and `POST /api/jobs/{id}/resume` is reachable from a browser
that was closed and reopened.

`async def` appears here and at the MCP boundary and nowhere else (ADR 0016). Every handler
that reaches a scorer goes through the thread pool, without exception: `Extractor.extract`
is synchronous and pydantic-ai's `run_sync()` raises inside a running event loop, so a
handler that scored on the loop thread would work in every test and blow up the first time a
model was wired.
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import time as time_type
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

    from longrun.jobs.runner import JobRunner

#: Where stored plans are read from and written to. One directory, the same one
#: `longrun plan --out` writes into, because the UI's plan list is scope 10.3's view of
#: work the CLI may have done.
DEFAULT_PLANS_DIR = Path("plans")


class PlanSubmission(BaseModel):
    """What the map view sends when somebody asks for a route.

    Deliberately the same fields `longrun plan` takes, and no others. A parameter that
    existed here and not on the CLI would be a capability only the UI had, which is the one
    thing scope 3.9 forbids.
    """

    start: str = Field(description="Origin as 'lat,lon'.")
    end: str = Field(description="Destination as 'lat,lon'.")
    via: list[str] = Field(default_factory=list)
    date: date_type
    start_time: time_type = time_type(7, 0)
    utc_offset_hours: float | None = None
    target_km: float | None = None
    rounds: int = 5
    avoid_high_stress: bool = False


class ResumeAnswer(BaseModel):
    """The user's choice on a parked job (scope 8.1 step 6, ADR 0019)."""

    choice: str = Field(description="The label of the candidate the user picked.")


class JobView(BaseModel):
    """A job as the UI sees it. A pause is `needs_input`, which is a status, not an error."""

    job_id: str
    status: str
    events: list[dict[str, Any]] = Field(default_factory=list)
    question: dict[str, Any] | None = None
    plan_id: str | None = None


def _point(text: str) -> Any:
    from longrun.core.models.geometry import LatLon

    lat, _, lon = text.partition(",")
    return LatLon(lat=float(lat), lon=float(lon))


def create_app(
    runner: JobRunner | None = None,
    *,
    plans_dir: Path | None = None,
    ui_dir: Path | None = None,
) -> FastAPI:
    """Build the app.

    A factory rather than a module-level `app`, because a test needs its own runner and its
    own directories, and a module-level singleton would make the suite share a thread pool
    across tests. `conftest._block_network` patches `socket.connect` globally and restores
    it at teardown, so an executor that outlived a test would un-block the network for
    whatever ran next - the same trap M5.6 documented for `jobs/`.
    """
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse

    from longrun.jobs.runner import JobRunner, JobStore

    plans = plans_dir or DEFAULT_PLANS_DIR
    jobs = runner or JobRunner(JobStore(plans / "jobs"))

    app = FastAPI(
        title="longrun",
        summary="Verified routes and plan sheets for 20-100 km runs.",
        version=_version(),
    )

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        """What the UI checks before drawing anything, and what says why it cannot.

        Reports the *reasons* a plan might be thin rather than a bare ok: no router, no
        fixtures and no model are three different degraded states and the UI renders them
        differently. Scope 3.6 at the API boundary.
        """
        from longrun.tools.base import ToolSettings

        settings = ToolSettings.from_env()
        return {
            "ok": True,
            "version": _version(),
            "fixtures": str(settings.root),
            "fixtures_present": settings.root.is_dir(),
            "router": settings.router_url,
            "offline": settings.offline,
        }

    @app.get("/api/profile")
    async def read_profile() -> dict[str, Any]:
        """The scope 6.3 table with its provenance, for the preference panel.

        Provenance is the point rather than a detail: the panel shows which entries a runner
        stated, which history inferred, and which are still defaults - and scope 6.3 is
        explicit that only the third kind may be asked about.
        """
        from longrun.core.preferences.store import load_profile

        return load_profile(None).model_dump(mode="json")

    @app.get("/api/plans")
    async def list_plans() -> list[dict[str, Any]]:
        """The plan list. Reads the directory the CLI writes into."""
        import anyio

        return await anyio.to_thread.run_sync(_stored_plans, plans)

    @app.get("/api/plans/{plan_id}")
    async def read_plan(plan_id: str) -> Any:
        """One stored plan, as the plan schema.

        Returned verbatim rather than through a view model, because a second schema drifts
        and the first fields to drift are the honest ones - the coverage manifest, the
        pacing caveats, and a `detour_ratio` of `null` that means nobody measured.
        """
        path = _plan_path(plans, plan_id)
        if path is None:
            raise HTTPException(status_code=404, detail=f"no stored plan {plan_id!r}")
        return JSONResponse(content=_read_json(path))

    @app.post("/api/plans", status_code=202)
    async def submit_plan(submission: PlanSubmission) -> JobView:
        """Start a plan. Returns immediately with a job id (scope 4.4).

        202 rather than 200: nothing has been planned yet, and a UI that rendered a route
        from this response would be rendering one that does not exist.
        """

        def work(report: Any) -> Any:
            return _run_plan(submission, plans, report)

        job_id = jobs.submit(work)
        return JobView(job_id=job_id, status=jobs.status(job_id))

    @app.get("/api/jobs")
    async def list_jobs() -> list[dict[str, str]]:
        return [{"job_id": job_id, "status": jobs.status(job_id)} for job_id in jobs.ids()]

    @app.get("/api/jobs/{job_id}")
    async def read_job(job_id: str) -> JobView:
        """Status and the events so far.

        Polled rather than streamed. A websocket would be the obvious thing and it is not
        worth it here: a plan is seconds to a couple of minutes, the event list is tens of
        entries, and polling keeps the server a plain request/response surface that the CLI
        and a curl both drive. Revisit if a plan ever streams partial geometry.
        """
        # Falls back to the stored scratchpad when this runner has never heard of the job.
        # That is not a corner case, it is the design: scope 4.2 makes the pause survive
        # the process, so the commonest way to reach this endpoint is a browser asking
        # about a job some *earlier* process parked. A 404 here would make a resumable job
        # look lost the moment the server restarted.
        try:
            status = jobs.status(job_id)
        except (KeyError, LookupError):
            try:
                status = jobs.store.load(job_id).status
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=404, detail=f"no job {job_id!r}") from exc
        return _job_view(jobs, job_id, status)

    @app.post("/api/jobs/{job_id}/resume")
    async def resume_job(job_id: str, answer: ResumeAnswer) -> JobView:
        """Answer a same-tier trade-off and let the job finish (ADR 0019).

        The user chooses; the model, when there is one, only wrote the comparison. This is
        the browser half of `longrun resume --choose B`, and it works on a job this process
        did not start, because the scratchpad is on disk - which is the boundary scope 4.2
        designs for and the one a closed-and-reopened browser crosses.
        """
        try:
            pad = jobs.store.load(job_id)
        except (KeyError, OSError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=f"no job {job_id!r}") from exc
        if pad.question is None:
            raise HTTPException(
                status_code=409,
                detail=f"job {job_id!r} is {pad.status!r} and is not waiting on anything",
            )
        if answer.choice not in pad.question.options:
            raise HTTPException(
                status_code=422,
                detail=f"{answer.choice!r} is not one of {pad.question.options}",
            )

        def work(report: Any, resumed: Any) -> Any:
            return _continue_plan(resumed, plans, report)

        jobs.resume(job_id, answer.choice, work)
        return _job_view(jobs, job_id, jobs.status(job_id))

    @app.get("/api/regions")
    async def list_regions() -> list[dict[str, Any]]:
        """Which regions are built and what they loaded (scope 10.3's region status).

        Read from each region's build manifest, which `build-region` writes and step 5's
        coverage report fills. A region with no manifest is listed as unbuilt rather than
        omitted - "not built" and "does not exist" are different answers.
        """
        import anyio

        return await anyio.to_thread.run_sync(_regions)

    if ui_dir is not None and ui_dir.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")

    return app


def _run_plan(submission: PlanSubmission, plans: Path, report: Any) -> Any:
    """One plan, start to finish, on a worker thread.

    Deliberately the same sequence `cli/plan.py` runs, in the same order, through the same
    functions - open the cache, wrap the router in it, route, open the context, run the
    loop. Scope 3.9 says the UI adds no capability; the way that stops being true is a
    second orchestration here that slowly drifts from the first, so this one calls the same
    pieces rather than reimplementing the middle.
    """
    from datetime import datetime

    from longrun.agent.loop import plan_route
    from longrun.core.data.cache import SqliteCache, cache_path_from_env
    from longrun.core.models.context import Budget
    from longrun.core.models.plan import SnapshotPins
    from longrun.core.models.request import PlanRequest
    from longrun.core.preferences.store import load_profile
    from longrun.core.routing.cached import CachedRouter
    from longrun.core.routing.custom_model import AVOID_HIGH_STRESS, NEUTRAL, to_custom_model
    from longrun.core.routing.graphhopper import GraphHopperRouter
    from longrun.runtime import PLAN_LATENCY_BUDGET_S, open_context
    from longrun.tools.base import ToolSettings

    settings = ToolSettings.from_env()
    waypoints = [_point(submission.start), *map(_point, submission.via), _point(submission.end)]
    start_at = datetime.combine(submission.date, submission.start_time)
    profile = load_profile(None)
    request = PlanRequest(
        mode="generate",
        date=submission.date,
        start=waypoints[0],
        end=waypoints[-1],
        via=list(waypoints[1:-1]),
        loop=waypoints[0] == waypoints[-1],
        start_time=submission.start_time,
        target_distance_km=submission.target_km,
        utc_offset_hours=submission.utc_offset_hours,
    )

    snapshot = SnapshotPins()
    with SqliteCache(
        settings.cache_path or cache_path_from_env(), offline=settings.offline
    ) as cache:
        budget = Budget(latency_budget_s=PLAN_LATENCY_BUDGET_S)
        router = CachedRouter(
            GraphHopperRouter(settings.router_url or ""),
            cache,
            budget,
            graph=snapshot.osm_extract_date,
        )
        params = AVOID_HIGH_STRESS if submission.avoid_high_stress else NEUTRAL
        report("routing")
        route = router.route(waypoints, custom_model=to_custom_model(params, profile) or None)
        report(f"routed {route.length_m / 1000:.2f} km")
        with open_context(
            route=route,
            root=settings.root,
            snapshot=snapshot,
            start_at=start_at,
            profile=profile,
            offline=settings.offline,
            utc_offset=submission.utc_offset_hours,
            cache=cache,
            budget=budget,
        ) as ctx:
            outcome = plan_route(
                request,
                ctx,
                start_at=start_at,
                route=route,
                router=router,
                max_rounds=submission.rounds,
            )
    return _finish(outcome, plans, report)


def _continue_plan(pad: Any, plans: Path, report: Any) -> Any:
    """A parked job, picked up from its scratchpad - possibly in another process.

    `jobs.resume` has already written the answer to disk before this runs, so what arrives
    here is the record rather than anything held in memory. That is the boundary scope 4.2
    exists for and the one a reopened browser crosses.
    """
    from longrun.agent.loop import plan_route
    from longrun.core.data.cache import SqliteCache, cache_path_from_env
    from longrun.core.models.context import Budget
    from longrun.core.models.plan import SnapshotPins
    from longrun.core.preferences.store import load_profile
    from longrun.core.routing.cached import CachedRouter
    from longrun.core.routing.graphhopper import GraphHopperRouter
    from longrun.runtime import PLAN_LATENCY_BUDGET_S, open_context
    from longrun.tools.base import ToolSettings

    if pad.route is None:  # pragma: no cover - a parked job has always routed
        raise ValueError("the stored job has no route to continue from")

    settings = ToolSettings.from_env()
    snapshot = SnapshotPins()
    start_at = _start_at(pad)
    report("resuming")
    with SqliteCache(
        settings.cache_path or cache_path_from_env(), offline=settings.offline
    ) as cache:
        budget = Budget(latency_budget_s=PLAN_LATENCY_BUDGET_S)
        router = CachedRouter(
            GraphHopperRouter(settings.router_url or ""),
            cache,
            budget,
            graph=snapshot.osm_extract_date,
        )
        with open_context(
            route=pad.route,
            root=settings.root,
            snapshot=snapshot,
            start_at=start_at,
            profile=load_profile(None),
            offline=settings.offline,
            cache=cache,
            budget=budget,
        ) as ctx:
            outcome = plan_route(
                pad.request, ctx, start_at=start_at, route=pad.route, router=router, resume=pad
            )
    return _finish(outcome, plans, report)


def _finish(outcome: Any, plans: Path, report: Any) -> Any:
    """Write the plan where `/api/plans` can find it, and hand back the scratchpad.

    The scratchpad is the return value because `jobs/` stores scratchpads and a pause is a
    state - a job that parked has no plan to write, and returning `None` there would make
    the runner's type a lie.
    """
    pad = outcome.scratchpad
    if outcome.plan is not None:
        directory = plans / pad.job_id if getattr(pad, "job_id", None) else plans / outcome.plan.id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "plan.json").write_text(
            outcome.plan.model_dump_json(indent=2), encoding="utf-8"
        )
        report(f"wrote {directory / 'plan.json'}", "complete")
    elif outcome.needs_input:
        report("waiting for a choice", "needs_input")
    return pad


def _start_at(pad: Any) -> Any:
    """The plan's own simulated start, from the stored request - never the wall clock.

    `core/` may not read the clock and this module may, which makes it exactly the place
    to get this wrong. A resumed job must be scored at the time the runner asked about, or
    every time-dependent scorer answers a different question on the second half of a route
    than it did on the first.
    """
    from datetime import datetime
    from datetime import time as time_type

    request = pad.request
    return datetime.combine(request.date, request.start_time or time_type(7, 0))


def _job_view(jobs: JobRunner, job_id: str, status: str) -> JobView:
    try:
        recorded = jobs.events(job_id)
    except (KeyError, LookupError):
        # A job this process did not run has no in-memory event log, and that is a fact
        # about this process rather than about the job. An empty list, not a failure.
        recorded = []
    events = [
        {"kind": event.kind, "message": event.message, "at": event.at.isoformat()}
        for event in recorded
    ]
    question: dict[str, Any] | None = None
    plan_id: str | None = None
    try:
        pad = jobs.store.load(job_id)
    except (KeyError, FileNotFoundError):
        pad = None
    if pad is not None:
        if pad.question is not None:
            question = pad.question.model_dump(mode="json")
        plan_id = getattr(pad, "plan_id", None)
    return JobView(job_id=job_id, status=status, events=events, question=question, plan_id=plan_id)


def _version() -> str:
    from longrun import __version__

    return __version__


def _read_json(path: Path) -> Any:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def _plan_path(plans: Path, plan_id: str) -> Path | None:
    """Locate a stored plan, refusing anything that is not a plain name.

    `plan_id` arrives from a URL. Without this check `../../etc/passwd` reads a file, which
    is the one way a read-only local API can still be dangerous.
    """
    if not plan_id or "/" in plan_id or "\\" in plan_id or plan_id.startswith("."):
        return None
    for candidate in (plans / plan_id / "plan.json", plans / f"{plan_id}.json"):
        if candidate.is_file():
            return candidate
    return None


def _stored_plans(plans: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not plans.is_dir():
        return out
    for path in sorted(plans.glob("*/plan.json")) + sorted(plans.glob("*.json")):
        try:
            data = _read_json(path)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or "route" not in data:
            continue
        out.append(
            {
                "plan_id": path.parent.name if path.name == "plan.json" else path.stem,
                "id": data.get("id"),
                "date": (data.get("request") or {}).get("date"),
                "length_m": (data.get("route") or {}).get("length_m"),
                "status": data.get("status"),
                "trade_offs": len(data.get("trade_offs") or []),
            }
        )
    return out


def _regions() -> list[dict[str, Any]]:
    specs = Path("deploy/regions")
    out: list[dict[str, Any]] = []
    if not specs.is_dir():
        return out
    for spec in sorted(specs.glob("*.yaml")):
        manifest = spec.with_suffix(".build.json")
        entry: dict[str, Any] = {"name": spec.stem, "built": manifest.is_file()}
        if manifest.is_file():
            try:
                data = _read_json(manifest)
            except (OSError, ValueError):
                data = {}
            entry["steps"] = {
                name: bool(step.get("complete")) for name, step in (data.get("steps") or {}).items()
            }
        out.append(entry)
    return out


__all__ = ["DEFAULT_PLANS_DIR", "JobView", "PlanSubmission", "ResumeAnswer", "create_app"]
