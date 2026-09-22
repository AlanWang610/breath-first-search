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

from pydantic import BaseModel, Field, model_validator

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
    #: Scope 6.4's two must-avoid forms, reaching `PlanRequest` for the first time from
    #: here. They arrived together in M12.3 and `longrun plan` grew `--avoid-name` and
    #: `--avoid-polygon` in the same commit, which is what the docstring above requires of
    #: any field added to this model.
    #:
    #: A name is resolved by `polygons_for_names` inside the loop, where a geocoder lookup
    #: is charged to the plan's budget and a name it cannot find becomes a *note* rather
    #: than an exception. A polygon is rounded and size-checked before the job starts,
    #: because the refusal has to reach the runner who drew it.
    avoid_names: list[str] = Field(default_factory=list)
    avoid_polygons: list[dict[str, Any]] = Field(default_factory=list)


class ResumeAnswer(BaseModel):
    """The user's choice on a parked job (scope 8.1 step 6, ADR 0019)."""

    choice: str = Field(description="The label of the candidate the user picked.")


class RangeEdit(BaseModel):
    """A stretch of the stored line, in metres from its start.

    Metres rather than a segment id, and that is the same choice M11.3 made for carried
    measurements: `segment_id` is `f"s{index:05d}"` and positional, so an id the browser
    read before an edit names different ground after one. A distance names the same ground
    on both sides of a renumbering. `Segment.cum_start_m + length_m` is what the map turns
    a click into, which keeps the conversion client-side and the capability out of it.
    """

    start_m: float = Field(ge=0, description="Where the range begins, metres from the start.")
    end_m: float = Field(gt=0, description="Where it ends.")

    @model_validator(mode="after")
    def _check_order(self) -> RangeEdit:
        if self.end_m <= self.start_m:
            raise ValueError("a range must have positive length")
        return self


class LockEdit(RangeEdit):
    """Scope 10.3's lock. `source` is not on offer: an HTTP client is the runner (M11.1)."""

    reason: str | None = Field(default=None, description="Why, for the sheet.")


class UnlockEdit(RangeEdit):
    """Scope 10.3's unlock, which trims a lock it only partly covers."""

    mine: bool = Field(
        default=False,
        description="Release only locks the runner set, leaving the loop's reroute locks.",
    )


class ViaEdit(BaseModel):
    """Scope 7.8's `pin_waypoint`, arriving as a drag rather than as a command."""

    at: str = Field(description="The via point as 'lat,lon'.")
    at_m: float | None = Field(
        default=None, description="Where along the line it belongs, if not where it is."
    )


class AvoidEdit(BaseModel):
    """Scope 10.3's drawn polygon, as GeoJSON the browser has not rounded.

    It is deliberately raw here. Rounding at `AREA_PRECISION` and the `MAX_AREA_KM2` check
    are the *server's*, in `core.routing.avoid`, because a browser's polygon carries
    sixteen significant digits of float straight into a routing cache key and M5.13 was the
    milestone spent discovering what that does. A client that rounded would be a second
    implementation of a rule with one owner.
    """

    polygon: dict[str, Any] = Field(description="A GeoJSON Polygon, or a Feature wrapping one.")


class ChooseEdit(RangeEdit):
    """Scope 10.3's "choose an alternative": a replacement line for a flagged stretch.

    **Geometry, not a path.** `longrun edit choose --alternative` takes a GPX file and this
    cannot: a POST that took a file path would give the `tools/` layer's file-path
    convention write semantics over HTTP, which is the hole `_plan_id_dir` closes (ADR
    0036). So the line arrives as points, in the `'lat,lon'` spelling `PlanSubmission`
    already uses for every other coordinate on this surface.
    """

    alternative: list[str] = Field(
        min_length=2, description="The replacement line, one 'lat,lon' per point."
    )
    only: list[str] = Field(
        default_factory=list,
        description="Scorers to re-run; the rest are carried. Empty means all of them.",
    )


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

    @app.get("/api/basemap")
    async def basemap() -> dict[str, Any]:
        """The raster source the map draws under the route (ADR 0023).

        Served from here rather than baked into the UI bundle, so there is one provider
        setting and every consumer reads it - `imagery_tile` included - and changing it needs
        a restart rather than a rebuild.

        A misconfigured provider is a 200 with `provider: null` and the reason, not a 500.
        The basemap is an enhancement: the route must still draw, and a typo in an
        environment variable is no reason to show somebody an empty map.
        """
        from longrun.core.data.tiles import PROVIDER_ENV_VAR, TileConfigError, provider_from_env

        try:
            provider = provider_from_env()
        except TileConfigError as exc:
            return {"provider": None, "reason": str(exc)}
        if provider is None:
            return {"provider": None, "reason": f"{PROVIDER_ENV_VAR}=none"}
        return provider.as_maplibre()

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

        Drawn polygons are rounded and size-checked before the job starts, for the same
        reason the `avoid` endpoint does it: a refusal has to reach the runner who drew the
        thing, and a browser cannot round without becoming a second implementation of
        `AREA_PRECISION`.
        """
        import anyio

        areas: list[dict[str, Any]] = []
        for index, drawn in enumerate(submission.avoid_polygons):
            area, refusal = await anyio.to_thread.run_sync(_rounded_area, drawn)
            if area is None:
                raise HTTPException(
                    status_code=422, detail=f"avoid polygon {index}: {refusal or 'not an area'}"
                )
            areas.append(area)
        rounded = submission.model_copy(update={"avoid_polygons": areas})

        def work(report: Any) -> Any:
            return _run_plan(rounded, plans, report)

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

    # --- the five gestures (scope 10.3, M12.2) --------------------------------------
    #
    # Every one of them takes an **id** and never a path (ADR 0036), and every one of them
    # is a `jobs.submit(work)` rather than work done on the handler's thread. `submit` is
    # generic over `Callable[[Reporter], Scratchpad]`, so none of this needed a runner
    # change; `resume` could not have been reused for any of it, because it is hard-wired
    # to `agent.loop.answer` and raises unless a question is pending.
    #
    # A lock is instantaneous and a `choose` is a full re-score, and they are both jobs
    # anyway. The uniformity is the point: one 202-then-poll path in the client, one event
    # log per edit, and an edit whose record survives the process that made it - which is
    # the same property scope 4.2 bought for a plan.
    #
    # What the handlers refuse *synchronously* is what can be answered without doing the
    # work: an id that resolves to nothing, a range with no length, an over-cap polygon, a
    # scorer nobody answers to. A refusal a runner can act on belongs in the response they
    # are waiting on, not in an event log they have to go and read.

    def _writable(plan_id: str) -> Path:
        path = _plan_path(plans, plan_id)
        if path is None:
            raise HTTPException(status_code=404, detail=f"no stored plan {plan_id!r}")
        return path

    def _started(plan_id: str, work: Any) -> JobView:
        job_id = jobs.submit(work)
        # `plan_id` is echoed from the request rather than read back off the scratchpad,
        # because the client needs it now: it is what the poll reloads when the job ends.
        return JobView(job_id=job_id, status=jobs.status(job_id), plan_id=plan_id)

    @app.post("/api/plans/{plan_id}/lock", status_code=202)
    async def lock_plan_range(plan_id: str, edit: LockEdit) -> JobView:
        """Exclude a range from rerouting (scope 6.4, 10.3).

        Written as the runner's own. `LockSource` is not a parameter here: M11.1 added it
        so that "unlock what I locked" had a discriminator, and there is exactly one
        loop-authored site in the tree. A browser is not it.
        """
        path = _writable(plan_id)

        def change(plan: Any) -> tuple[Any, str]:
            from longrun.core.plan.edits import lock_range

            locked = lock_range(
                plan.request.locked, edit.start_m, edit.end_m, edit.reason, source="user"
            )
            return (
                _with_request(plan, locked=locked),
                f"locked {edit.start_m:.0f}-{edit.end_m:.0f} m; {len(locked)} lock(s) on this plan",
            )

        return _started(plan_id, lambda report: _edit_request(path, plan_id, change, report))

    @app.post("/api/plans/{plan_id}/unlock", status_code=202)
    async def unlock_plan_range(plan_id: str, edit: UnlockEdit) -> JobView:
        """Release a locked range, trimming a lock it only partly covers (scope 10.3)."""
        path = _writable(plan_id)

        def change(plan: Any) -> tuple[Any, str]:
            from longrun.core.plan.edits import unlock_range

            kept, freed = unlock_range(
                plan.request.locked, edit.start_m, edit.end_m, source="user" if edit.mine else None
            )
            note = (
                "; ".join(
                    f"released {lock.start_m:.0f}-{lock.end_m:.0f} m "
                    f"({lock.source}: {lock.reason or 'no reason given'})"
                    for lock in freed
                )
                if freed
                else f"nothing locked in {edit.start_m:.0f}-{edit.end_m:.0f} m"
            )
            return _with_request(plan, locked=kept), note

        return _started(plan_id, lambda report: _edit_request(path, plan_id, change, report))

    @app.post("/api/plans/{plan_id}/via", status_code=202)
    async def add_plan_via(plan_id: str, edit: ViaEdit) -> JobView:
        """Add a via point to the stored request (scope 7.8's `pin_waypoint`).

        The line is left where it is, and the job says so. Drawing one through the new
        point is a routing call against a policy that was frozen when the plan began, and
        that is `longrun edit reroute` - see the note on `_line_was_drawn_without`.
        """
        path = _writable(plan_id)
        try:
            point = _point(edit.at)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail=f"could not read {edit.at!r}; expected 'lat,lon'"
            ) from exc

        def change(plan: Any) -> tuple[Any, str]:
            from longrun.core.plan.edits import insert_via

            via, index = insert_via(plan.request.via, point, plan.route, edit.at_m)
            return (
                _with_request(plan, via=via),
                f"via {index + 1} of {len(via)}; {_line_was_drawn_without}",
            )

        return _started(plan_id, lambda report: _edit_request(path, plan_id, change, report))

    @app.post("/api/plans/{plan_id}/avoid", status_code=202)
    async def add_plan_avoid(plan_id: str, edit: AvoidEdit) -> JobView:
        """Add a drawn avoid polygon to the stored request (scope 6.4, 10.3).

        **Rounded and size-checked here, before the job starts**, for two different
        reasons. The rounding is `detour.round_coordinates` at `AREA_PRECISION` because an
        avoid area travels inside `custom_model` and `CachedRouter` hashes that into its
        key (M5.13). The cap is checked synchronously because an over-cap area is refused
        *by name with its size* - "that is 11 km2 against a 4 km2 cap" is something a
        runner can act on by drawing a smaller one, and it belongs in the 422 they are
        waiting on rather than in a failed job's event log.
        """
        import anyio

        path = _writable(plan_id)
        area, refusal = await anyio.to_thread.run_sync(_rounded_area, edit.polygon)
        if area is None:
            raise HTTPException(status_code=422, detail=refusal or "the polygon is not an area")

        def change(plan: Any) -> tuple[Any, str]:
            polygons = [*plan.request.avoid_polygons, area]
            return (
                _with_request(plan, avoid_polygons=polygons),
                f"{len(polygons)} avoid area(s) on this plan; {_line_was_drawn_without}",
            )

        return _started(plan_id, lambda report: _edit_request(path, plan_id, change, report))

    @app.post("/api/plans/{plan_id}/choose", status_code=202)
    async def choose_plan_alternative(plan_id: str, edit: ChooseEdit) -> JobView:
        """Splice an alternative into a flagged stretch, auto-lock it, and re-score.

        One job, not three, for the reason `longrun edit choose` gives: a plan carrying an
        edited line and measurements of the old one is the state M11 exists to prevent, and
        separate steps make it reachable by stopping halfway.
        """
        from longrun.core.scorers.registry import SCORERS

        path = _writable(plan_id)
        try:
            points = [_point(text) for text in edit.alternative]
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail="every alternative point must read as 'lat,lon'"
            ) from exc
        if unknown := sorted(set(edit.only) - set(SCORERS)):
            raise HTTPException(status_code=422, detail=f"no scorer answers to {unknown}")

        return _started(
            plan_id,
            lambda report: _choose_alternative(
                path, plan_id, edit.start_m, edit.end_m, points, set(edit.only) or None, report
            ),
        )

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

    from longrun.agent.loop import call_sites_from_env, plan_route
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
        # Already rounded by the handler. `_policy` is what turns both of these into the
        # frozen `RoutingPolicy` the whole plan is then drawn under - which is the reason
        # they belong on the *submission* and not on an edit: an avoid the plan began with
        # shapes every line it draws, and one added afterwards shapes none of them.
        avoid_names=list(submission.avoid_names),
        avoid_polygons=list(submission.avoid_polygons),
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
            graph=snapshot.graph_identity,
        )
        params = AVOID_HIGH_STRESS if submission.avoid_high_stress else NEUTRAL
        model = to_custom_model(params, profile) or None
        report("routing")
        route = router.route(waypoints, custom_model=model)
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
                custom_model=model,
                # The CLI has passed this since M5 and this path passed nothing, so the
                # HTTP surface was model-free even with a key set - which made "the UI adds
                # no capability" true in one direction and false in the other (scope 3.9).
                sites=call_sites_from_env(budget),
            )
    return _finish(outcome, plans, report)


def _continue_plan(pad: Any, plans: Path, report: Any) -> Any:
    """A parked job, picked up from its scratchpad - possibly in another process.

    `jobs.resume` has already written the answer to disk before this runs, so what arrives
    here is the record rather than anything held in memory. That is the boundary scope 4.2
    exists for and the one a reopened browser crosses.
    """
    from longrun.agent.loop import call_sites_from_env, plan_route
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
            graph=snapshot.graph_identity,
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
            # No `custom_model` here on purpose: the policy was resolved when the plan began
            # and rides on the scratchpad, so a resume in a different process costs the
            # route the same way the first half of it was costed.
            outcome = plan_route(
                pad.request,
                ctx,
                start_at=start_at,
                route=pad.route,
                router=router,
                resume=pad,
                sites=call_sites_from_env(budget),
            )
    return _finish(outcome, plans, report)


#: What `via` and `avoid` say, and what the UI repeats, rather than implying a route that
#: honours a gesture nothing has re-drawn for.
#:
#: `RoutingPolicy` is frozen and resolved once, persisted so that a resume in another
#: process cannot compute a different one. Patching an avoid area into the policy the first
#: half of a line was already drawn under would cost that invariant and buy a line that is
#: half one thing and half another. The honest option is a whole re-route, which is a
#: routing call, which is `longrun edit reroute` - see the PR and `ui/README.md` for why it
#: is not an endpoint.
_line_was_drawn_without = "the stored line was drawn without it; re-route to honour it"


def _rounded_area(polygon: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """`core.routing.avoid.area_from_polygon`, on a worker thread.

    A thin wrapper so the handler can `run_sync` it: `area_from_polygon` reaches shapely and
    pyproj, and ADR 0016's rule for this module is that nothing which touches the
    geospatial stack runs on the event loop thread.
    """
    from longrun.core.routing.avoid import area_from_polygon

    return area_from_polygon(polygon, area_id="avoid-drawn")


def _with_request(plan: Any, **fields: Any) -> Any:
    """`plan` with `fields` changed on its request, and nothing else touched."""
    return plan.model_copy(update={"request": plan.request.model_copy(update=fields)})


def _edit_request(path: Path, plan_id: str, change: Any, report: Any) -> Any:
    """One request-level gesture - lock, unlock, via, avoid - applied to a stored plan.

    **The line is not touched and the measurements are not touched**, which is what makes
    these three safe to do without a router or a context. The two gestures that *do* move
    the line are `choose`, below, and `longrun edit reroute`, and both re-score in the same
    breath: a plan carrying an edited line and measurements of the old one is the state M11
    exists to prevent.

    Returns a `Scratchpad` because that is what `jobs.submit` is generic over. It is a
    receipt rather than a resume point - the loop never reads one of these - and it carries
    the plan id so that `_job_view` can tell a browser which plan to reload.
    """
    from longrun.core.models.plan import Plan
    from longrun.core.plan.scratchpad import Scratchpad

    plan = Plan.model_validate_json(path.read_text(encoding="utf-8"))
    edited, note = change(plan)
    path.write_text(edited.model_dump_json(indent=2), encoding="utf-8")
    report(note)
    return Scratchpad(
        plan_id=plan_id, request=edited.request, route=edited.route, status="complete"
    )


def _choose_alternative(
    path: Path,
    plan_id: str,
    start_m: float,
    end_m: float,
    points: list[Any],
    only: set[str] | None,
    report: Any,
) -> Any:
    """Splice a drawn alternative into a flagged stretch, auto-lock it, and re-score.

    The same sequence `longrun edit choose` runs, through the same functions and in the
    same order - `splice`, `lock_range`, `rescore_plan` - because scope 3.9's rule is not
    that the UI calls *a* tool but that it calls the *same* one. A second orchestration
    here would drift from the first, and the first thing to drift would be the hour the
    edit is scored at, which is why `edits.scored_at` now owns that.

    The lock is the runner's own (ADR 0019: they chose it), so `unlock --mine` can take it
    back and the loop's next round will not reopen it.

    **Nothing here samples elevation for the new stretch.** `splice` keeps the heights of
    the ground it did not touch and leaves the replacement's `None`, and `rescore_plan`
    reads the profile off the edited line - so the gap is real and `samples_missing`
    reports it. A UI that drew it as zero would undo that in one step, which is why the
    timeline draws it as a break.
    """
    from longrun.core.geo.gpx import normalize
    from longrun.core.models.geometry import Route
    from longrun.core.models.plan import Plan
    from longrun.core.plan.edits import lock_range, scored_at, splice
    from longrun.core.plan.refresh import rescore_plan
    from longrun.core.plan.scratchpad import Scratchpad
    from longrun.core.scorers.registry import closure
    from longrun.runtime import open_context
    from longrun.tools.base import ToolSettings

    plan = Plan.model_validate_json(path.read_text(encoding="utf-8"))
    replacement = Route(
        id="alternative",
        points=normalize([(point.lat, point.lon, None) for point in points]),
        source="edited",
    )
    line = splice(plan.route, start_m, end_m, replacement)
    locked = lock_range(
        plan.request.locked, start_m, end_m, reason="chose an alternative", source="user"
    )
    stored = _with_request(plan, locked=locked)
    report(
        f"spliced {start_m:.0f}-{end_m:.0f} m: {plan.route.length_m / 1000:.2f} -> "
        f"{line.length_m / 1000:.2f} km, and locked it"
    )

    wanted = closure(only) if only is not None else None
    if only is not None and wanted is not None and (added := sorted(set(wanted) - only)):
        # Reported rather than widened quietly, which is the rule `run_scorers` keeps by
        # refusing an unclosed set: a caller who asked for one scorer and got four should
        # be told which three read the first one's output.
        report(f"also re-scoring {', '.join(added)}, which a requested scorer reads")

    settings = ToolSettings.from_env()
    start_at = scored_at(stored)
    report("re-scoring the edited line")
    with open_context(
        route=line,
        root=settings.root,
        snapshot=stored.manifest.snapshot,
        start_at=start_at,
        profile=stored.profile,
        offline=settings.offline,
        cache_path=settings.cache_path,
        utc_offset=stored.request.utc_offset_hours,
        start_window=stored.request.start_window,
    ) as ctx:
        rescored = rescore_plan(
            stored, ctx, start_at=start_at, route=line, only=set(wanted) if wanted else None
        )
    for note in rescored.delta.lines():
        report(note)
    path.write_text(rescored.plan.model_dump_json(indent=2), encoding="utf-8")
    return Scratchpad(
        plan_id=plan_id,
        request=rescored.plan.request,
        route=rescored.plan.route,
        status="complete",
    )


def _finish(outcome: Any, plans: Path, report: Any) -> Any:
    """Write the plan where `/api/plans` can find it, and hand back the scratchpad.

    The scratchpad is the return value because `jobs/` stores scratchpads and a pause is a
    state - a job that parked has no plan to write, and returning `None` there would make
    the runner's type a lie.
    """
    pad = outcome.scratchpad
    if outcome.plan is not None:
        # **Named by the plan, not by the job**, and that is a fix rather than a preference.
        #
        # `_job_view` reports `pad.plan_id` and the browser opens `/api/plans/{that}`. The
        # directory used to be `pad.job_id`, which at this moment is neither the runner's
        # job id nor the plan id: `jobs.submit` assigns the runner's id *after* `work`
        # returns, so what was read here was the one `agent.loop._fresh` minted for the
        # scratchpad. The finished plan therefore landed under a name nothing in the API
        # ever reported, and the poll that completes a plan 404'd on the plan it had just
        # written. It was reachable only by clicking it in the list.
        #
        # `Plan.id` is `pad.plan_id` (`loop._finish` passes it), so this is stable across a
        # resume for the same reason the old name was meant to be.
        directory = plans / outcome.plan.id
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


#: Characters a plan id may not contain. `\\` and `/` are separators; `:` is the one
#: that took M12 to find, because on Windows a drive-relative name like `C:plan` has
#: neither; `\x00` is what a path API refuses with an exception rather than a `None`.
_RESERVED = ("/", "\\", ":", "\x00")


def _plan_id_dir(plans: Path, plan_id: str) -> Path | None:
    """`plans / plan_id`, or `None` if `plan_id` is not a plain name that stays inside it.

    **This is the whole of the write surface's addressing**, and it is a resolver rather
    than a parameter for one reason: the `tools/` layer is file-path-parameterised
    throughout - `lock_segment`, `gpx_verify` and the rest all take a path - and a POST
    that took a scratchpad or a GPX path would hand that convention write semantics over
    an HTTP boundary. A read of the wrong file is a disclosure; a write to one is a
    deletion. So every write endpoint below takes an id, and every id comes through here
    (ADR 0036).

    Two checks, because neither is sufficient on its own.

    The character check refuses what a URL can carry - `..`, a separator, a leading dot.
    The containment check is what catches the case the character check alone missed for
    M7-M11: on Windows `Path("plans") / "C:plan"` is `WindowsPath("C:plan")`, a
    *drive-relative* path with no separator and no leading dot that leaves the plans
    directory entirely. `":"` is now refused outright and the resolved path is checked
    against the resolved root as well, because the next escape will be one nobody has
    thought of either.
    """
    if not plan_id or plan_id.startswith(".") or any(ch in plan_id for ch in _RESERVED):
        return None
    candidate = plans / plan_id
    try:
        root = plans.resolve()
        inside = candidate.resolve()
    except (OSError, ValueError):  # pragma: no cover - a name the OS refuses to resolve
        return None
    if root not in inside.parents:
        return None
    return candidate


def _plan_path(plans: Path, plan_id: str) -> Path | None:
    """Locate a stored plan, refusing anything that is not a plain name.

    `plan_id` arrives from a URL. Without this check `../../etc/passwd` reads a file, which
    is the one way a read-only local API can still be dangerous - and since M12 it is no
    longer read-only, so see `_plan_id_dir` for what the check now is.
    """
    directory = _plan_id_dir(plans, plan_id)
    if directory is None:
        return None
    for candidate in (directory / "plan.json", plans / f"{plan_id}.json"):
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
                # Not `route.length_m`: that is a computed property on `Route` and so is
                # absent from the JSON dump. The plan list showed every route as blank
                # until this was run against a real stored plan, which is how it showed.
                "length_m": _length_of(data),
                "status": data.get("status"),
                "trade_offs": len(data.get("trade_offs") or []),
            }
        )
    return out


def _length_of(plan: dict[str, Any]) -> float | None:
    """A stored plan's distance, from the metrics or from the route's last point.

    `Route.length_m` is a property and never reaches the JSON, so both of these read
    something that does. `None` when neither is there, because a plan list that invented a
    zero would be a list of zero-kilometre runs.
    """
    metrics = plan.get("metrics") or {}
    if isinstance(metrics.get("length_m"), (int, float)):
        return float(metrics["length_m"])
    points = (plan.get("route") or {}).get("points") or []
    if points and isinstance(points[-1], dict):
        last = points[-1].get("cum_dist_m")
        if isinstance(last, (int, float)):
            return float(last)
    return None


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
                # `complete` is a property on `StepRecord` and the manifest stores
                # `status`, so reading `complete` reported every step of every built
                # region as not done. Found by looking at the panel after a real build.
                name: bool(step.get("complete", step.get("status") == "done"))
                for name, step in (data.get("steps") or {}).items()
            }
        out.append(entry)
    return out


__all__ = ["DEFAULT_PLANS_DIR", "JobView", "PlanSubmission", "ResumeAnswer", "create_app"]
