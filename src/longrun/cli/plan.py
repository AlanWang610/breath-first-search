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

from longrun.cli.repair import score_route
from longrun.core.models.geometry import LatLon
from longrun.core.models.plan import SnapshotPins
from longrun.core.models.request import PlanRequest
from longrun.core.preferences.store import load_profile
from longrun.core.routing.base import NoRouteError, RouterUnavailable

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
    start_point: str = typer.Option(..., "--from", help="Origin as lat,lon."),
    end_point: str | None = typer.Option(None, "--to", help="Destination as lat,lon."),
    via: list[str] = typer.Option([], "--via", help="Intermediate point, repeatable."),
    date: datetime = typer.Option(..., "--date", formats=["%Y-%m-%d"], help="Run date."),
    start: str = typer.Option("07:00", "--start", help="Start time, HH:MM."),
    target_km: float | None = typer.Option(None, "--target-km", help="Target distance."),
    avoid_high_stress: bool = typer.Option(
        False, "--avoid-high-stress", help="Down-weight LTS 3 and 4 ways in the router."
    ),
    alternatives: int = typer.Option(
        0, "--alternatives", help="Also score N router alternatives between the same ends."
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
    """Route between points and score the result (scope 6.1 generate mode)."""
    from longrun.core.routing.graphhopper import GraphHopperRouter

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

    router = GraphHopperRouter(router_url)
    model = AVOID_HIGH_STRESS if avoid_high_stress else None
    typer.echo(f"routing {len(waypoints)} point(s) through {router.url}")

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

    typer.echo(f"routed {route.length_m / 1000:.2f} km over {len(route.points)} point(s)")

    # The request carries the *asked-for* geometry, not the routed geometry: `route_diff`
    # and any later re-plan need to know what was requested, and `loop` is what says a
    # route ending where it started was meant to.
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
        "snapshot": _snapshot(snapshot_path),
        "offline": offline,
        "cache_path": cache_path,
        "remote_rasters": remote_rasters,
        "utc_offset": utc_offset,
        "router": router,
    }

    score_route(route, request, out=out, **shared)

    for index, candidate in enumerate(router.alternatives(route, 0, k=alternatives)[:alternatives]):
        # Scored, not compared. Scope 8.1 step 6 chooses between candidates and that is
        # arbitration's job with the agent driving it (M5); what this produces is the raw
        # material — a scored plan per candidate, in its own directory.
        typer.echo(f"alternative {index}: {candidate.length_m / 1000:.2f} km")
        score_route(
            candidate,
            request,
            out=(out / f"alt-{index}") if out else None,
            **shared,
        )


def _snapshot(path: Path | None) -> SnapshotPins:
    if path is None:
        return SnapshotPins()
    return SnapshotPins.model_validate_json(path.read_text(encoding="utf-8"))


def register(app: typer.Typer) -> None:
    app.command("plan")(plan)
