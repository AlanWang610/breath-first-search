"""`longrun plan` — generate a route and score it (scope 6.1, 8.1, 10.1).

The other half of scope 6.1. Repair mode takes geometry from the user and never needs a
router; generate mode asks for a route between points and cannot work without one. From
the route onwards both are the same code — `repair.score_route` — because two entry points
that each scored a route their own way would drift, and the golden suite only watches one.

Three things this command deliberately does not do.

**It does not geocode.** Endpoints are `lat,lon`. A geocoder is a network dependency with
a licence and a rate limit, and putting one in the way of the first generated route would
make an M3 exit criterion depend on an M4-shaped decision. The agent (§8.1 step 1) is
where a place name becomes a coordinate.

**It does not iterate.** Scope 8.1's loop — score, arbitrate, reroute, re-score, up to five
rounds — is the agent's, in M5. What runs here is steps 2 through 5 once: route, segment,
score, verify. `--alternatives` asks the router for candidates and scores each, which is
the raw material step 6 needs and stops short of choosing between them.

**It does not shape a route to a target distance.** `--target-km` reaches the plan request,
so `gpx_verify` check 7 measures against it and reports the shortfall; what it does not do
is search for a loop of that length. Scope 8.1 step 4 is a routing search, and building one
before the arbitration that would steer it is building it blind.
"""

from __future__ import annotations

from datetime import datetime
from datetime import time as time_type
from pathlib import Path
from typing import Any

import typer

from longrun.agent.loop import MAX_ROUNDS
from longrun.cli.repair import score_route
from longrun.core.data.cache import CacheMiss, SqliteCache, cache_path_from_env, offline_from_env
from longrun.core.models.context import Budget
from longrun.core.models.geometry import LatLon
from longrun.core.models.plan import SnapshotPins
from longrun.core.models.request import PlanRequest
from longrun.core.preferences.store import load_profile
from longrun.core.routing.base import NoRouteError, RouterUnavailable
from longrun.core.routing.cached import CachedRouter
from longrun.runtime import PLAN_LATENCY_BUDGET_S

#: A custom model that keeps a runner off high-stress roads, in the terms ADR 0001's
#: encoded value made available. Not applied unless asked: M0.5 measured `avoid` against
#: `neutral` on four route pairs and found a detour of at most 0.8%, because stock
#: `foot_priority` already keeps pedestrians off arterials. Risk R1's note stands — these
#: parameters have to be **fitted against real preference pairs** (M6), not assumed to
#: help, and shipping them on by default would bake in an unmeasured assumption.
AVOID_HIGH_STRESS = {"priority": [{"if": "lts >= 3", "multiply_by": "0.2"}]}


def _point(text: str, label: str) -> LatLon:
    try:
        lat, _, lon = text.partition(",")
        return LatLon(lat=float(lat), lon=float(lon))
    except (TypeError, ValueError) as exc:
        typer.echo(f"error: could not read --{label} {text!r}; expected lat,lon", err=True)
        raise typer.Exit(code=2) from exc


def plan(
    request_file: Path | None = typer.Argument(
        None, help="Request YAML (scope 10.1). Its keys stand in for the options below."
    ),
    start_point: str | None = typer.Option(None, "--from", help="Origin as lat,lon."),
    end_point: str | None = typer.Option(None, "--to", help="Destination as lat,lon."),
    via: list[str] = typer.Option([], "--via", help="Intermediate point, repeatable."),
    date: datetime | None = typer.Option(None, "--date", formats=["%Y-%m-%d"], help="Run date."),
    start: str = typer.Option("07:00", "--start", help="Start time, HH:MM."),
    target_km: float | None = typer.Option(None, "--target-km", help="Target distance."),
    avoid_high_stress: bool = typer.Option(
        False, "--avoid-high-stress", help="Down-weight LTS 3 and 4 ways in the router."
    ),
    alternatives: int = typer.Option(
        0, "--alternatives", help="Also score N router alternatives between the same ends."
    ),
    rounds: int = typer.Option(
        MAX_ROUNDS, "--rounds", help="Scope 8.1 step 6's cap. 0 scores the route once."
    ),
    router_url: str | None = typer.Option(None, "--router", help="GraphHopper base URL."),
    profile_path: Path | None = typer.Option(None, "--profile", help="Preference profile YAML."),
    fixtures: Path | None = typer.Option(None, "--fixtures", help="Layer/raster directory."),
    snapshot_path: Path | None = typer.Option(None, "--snapshot", help="Data-snapshot pins."),
    out: Path | None = typer.Option(None, "--out", help="Directory for outputs."),
    offline: bool = typer.Option(False, "--offline", help="Fail on a cache miss."),
    cache_path: Path | None = typer.Option(None, "--cache", help="Cache/cassette file."),
    remote_rasters: bool = typer.Option(
        False, "--remote-rasters", help="Read 3DEP and canopy over the network."
    ),
    utc_offset: float | None = typer.Option(None, "--utc-offset", help="Hours from UTC."),
) -> None:
    """Route between points and score the result through the scope 8.1 loop."""
    from longrun.core.routing.graphhopper import GraphHopperRouter

    if request_file is not None:
        try:
            asked = _read_request(request_file)
        except (OSError, ValueError) as exc:
            typer.echo(f"error: could not read {request_file}: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        start_point = start_point or asked.get("from")
        end_point = end_point or asked.get("to")
        via = via or [str(v) for v in asked.get("via", [])]
        date = date or _as_datetime(asked.get("date"))
        start = asked.get("start", start) if start == "07:00" else start
        target_km = target_km if target_km is not None else asked.get("target_km")
        utc_offset = utc_offset if utc_offset is not None else asked.get("utc_offset_hours")

    if not start_point:
        typer.echo("error: give --from, or a request file with a `from` key", err=True)
        raise typer.Exit(code=2)
    if date is None:
        typer.echo("error: give --date, or a request file with a `date` key", err=True)
        raise typer.Exit(code=2)

    waypoints = [_point(start_point, "from")]
    waypoints.extend(_point(text, "via") for text in via)
    if end_point:
        waypoints.append(_point(end_point, "to"))
    elif len(waypoints) > 1:
        # A route with vias and no destination is a loop back to the start, which is the
        # commonest long-run shape and the one a `--to` would be an odd way to express.
        waypoints.append(waypoints[0])
    else:
        typer.echo("error: give --to, or --via points to loop through", err=True)
        raise typer.Exit(code=2)

    try:
        hour, _, minute = start.partition(":")
        start_at = datetime.combine(date.date(), time_type(int(hour), int(minute or 0)))
    except ValueError as exc:
        typer.echo(f"error: could not read --start {start!r}; expected HH:MM", err=True)
        raise typer.Exit(code=2) from exc

    # Either door turns no-miss mode on, as `repair` has always done and this had not:
    # golden and contract runs export the variable rather than passing the flag.
    offline = offline or offline_from_env()
    snapshot = _snapshot(snapshot_path)

    # The cache is opened *before* the router, because the router is wrapped in it. Until
    # M5.4 nothing cached a route and nothing charged one to the budget, so scope 6.4's
    # 200-call cap counted forecasts and adapters and not the calls scope 8.1 step 6 makes
    # most of - and no golden could replay a reroute, which is the loop's whole subject.
    store = cache_path or cache_path_from_env()
    with SqliteCache(store, offline=offline) as cache:
        budget = Budget(latency_budget_s=PLAN_LATENCY_BUDGET_S)
        engine = GraphHopperRouter(router_url)
        router = CachedRouter(
            engine,
            cache,
            budget,
            # A route is a function of the graph it was drawn on, and scope 13 rebuilds
            # that. Two graphs must not share a cache key.
            graph=snapshot.osm_extract_date,
        )
        model = AVOID_HIGH_STRESS if avoid_high_stress else None
        typer.echo(f"routing {len(waypoints)} point(s) through {engine.url}")

        try:
            route = router.route(waypoints, custom_model=model)
        except RouterUnavailable as exc:
            # Distinct exits, because they need different things done about them: a missing
            # router is a service to start, and a missing route is a route to change.
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=3) from exc
        except NoRouteError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=4) from exc
        except CacheMiss as exc:
            # Offline and unrecorded. Its own exit code because it is neither of the above:
            # nothing is down and no route is impossible - this cassette does not hold the
            # answer, and the thing to do is record it.
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=5) from exc

        typer.echo(f"routed {route.length_m / 1000:.2f} km over {len(route.points)} point(s)")

        # The request carries the *asked-for* geometry, not the routed geometry:
        # `route_diff` and any later re-plan need to know what was requested, and `loop` is
        # what says a route ending where it started was meant to.
        request = PlanRequest(
            mode="generate",
            date=date.date(),
            start=waypoints[0],
            end=waypoints[-1],
            via=list(waypoints[1:-1]),
            loop=waypoints[0] == waypoints[-1],
            start_time=start_at.time(),
            target_distance_km=target_km,
            utc_offset_hours=utc_offset,
        )
        shared: dict[str, Any] = {
            "start_at": start_at,
            "profile": load_profile(profile_path),
            "root": fixtures or Path("data"),
            "snapshot": snapshot,
            "offline": offline,
            "remote_rasters": remote_rasters,
            "utc_offset": utc_offset,
            "router": router,
            "cache": cache,
            "budget": budget,
        }

        if rounds > 0:
            _iterate(request, route, shared, rounds=rounds, out=out)
        else:
            score_route(route, request, out=out, **shared)

        # `max_paths` is a total, not an extra: GraphHopper returns the primary path first,
        # and in generate mode the primary path *is* the route just drawn. Asking for one
        # more and skipping it is the difference between three alternatives and two plus a
        # duplicate, which this scored - and wrote to its own directory - on every run
        # until M5.4. And no request at all when none were asked for: `--alternatives 0`
        # used to build a body, post it, and slice the answer away.
        candidates = (
            router.alternatives(route, None, k=alternatives + 1, custom_model=model)[1:]
            if alternatives > 0
            else []
        )
        for index, candidate in enumerate(candidates[:alternatives]):
            # Scored, not compared. Scope 8.1 step 6 chooses between candidates and that is
            # arbitration's job with the agent driving it (M5.5); what this produces is the
            # raw material - a scored plan per candidate, in its own directory.
            typer.echo(f"alternative {index}: {candidate.length_m / 1000:.2f} km")
            score_route(
                candidate,
                request,
                out=(out / f"alt-{index}") if out else None,
                **shared,
            )


def _as_datetime(value: Any) -> datetime | None:
    """A request file's `date`, whatever YAML decided it was.

    `yaml.safe_load` returns a `date` for an unquoted 2026-09-15 and a `str` for a quoted
    one, and a golden route's `request.yaml` is hand-written - so both arrive.
    """
    from datetime import date as date_type

    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date_type):
        return datetime(value.year, value.month, value.day)
    return datetime.strptime(str(value), "%Y-%m-%d")


def _read_request(path: Path) -> dict[str, Any]:
    """Scope 10.1's `longrun plan request.yaml`, which the CLI has never accepted.

    The same three keys a golden route's `request.yaml` already carries, plus the two
    endpoints - so a generate-mode golden needs no second schema.
    """
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("a request file is a mapping")
    return raw


def _iterate(
    request: PlanRequest,
    route: Any,
    shared: dict[str, Any],
    *,
    rounds: int,
    out: Path | None,
) -> None:
    """Scope 8.1's loop, through the same pieces `repair` scores one pass with.

    The context is opened here rather than inside `score_route` because the loop scores
    several candidates per round against **one** budget and one cache - which is what
    M5.1 took `score_route` apart for.
    """
    from longrun.agent.loop import plan_route
    from longrun.core.export.sheet_md import render_markdown
    from longrun.runtime import open_context

    with open_context(
        route=route,
        root=shared["root"],
        snapshot=shared["snapshot"],
        start_at=shared["start_at"],
        profile=shared["profile"],
        offline=shared["offline"],
        remote_rasters=shared["remote_rasters"],
        utc_offset=shared["utc_offset"],
        cache=shared["cache"],
        budget=shared["budget"],
    ) as ctx:
        outcome = plan_route(
            request,
            ctx,
            start_at=shared["start_at"],
            route=route,
            router=shared["router"],
            profile=shared["profile"],
            snapshot=shared["snapshot"],
            max_rounds=rounds,
        )

    pad = outcome.scratchpad
    typer.echo(f"loop: {pad.round} round(s), status {pad.status}")

    if out:
        out.mkdir(parents=True, exist_ok=True)
        pad.save(out / "scratchpad.json")

    if outcome.needs_input and pad.question is not None:
        # Scope 4.2: the pause is a state, not an error. The scratchpad is on disk and the
        # exit code says a person is needed - which is different from a failure.
        typer.echo(f"needs input: {pad.question.prompt}")
        for option in pad.question.options:
            typer.echo(f"  - {option}")
        raise typer.Exit(code=6)

    if outcome.plan is None:  # pragma: no cover - a failed route already exited
        typer.echo("error: no plan was produced", err=True)
        raise typer.Exit(code=4)

    sheet = render_markdown(
        outcome.plan,
        elevation=outcome.scored.elevation if outcome.scored else None,
        verify=outcome.scored.verify if outcome.scored else None,
    )
    if out:
        (out / "sheet.md").write_text(sheet, encoding="utf-8")
        (out / "plan.json").write_text(outcome.plan.model_dump_json(indent=2), encoding="utf-8")
        typer.echo(f"wrote {out / 'sheet.md'} and {out / 'plan.json'}")
    else:
        typer.echo(sheet)


def _snapshot(path: Path | None) -> SnapshotPins:
    if path is None:
        return SnapshotPins()
    return SnapshotPins.model_validate_json(path.read_text(encoding="utf-8"))


def register(app: typer.Typer) -> None:
    app.command("plan")(plan)
