"""One scoring pass over one route - scope 8.1 steps 4, 5 and 9, and nothing else.

Extracted from `cli/repair.py::score_route` in M5.1, because scope 8.1 step 6 scores a
candidate route repeatedly against one open context and the CLI scores one route once.
Before this both were the same function, and that function also built the context, rendered
the sheet and wrote files - so the loop could not reuse it without inheriting a `typer.echo`
and a fresh `Budget` per candidate.

Two things stay out on purpose. **The context is passed in**, so nothing here reads the
environment or opens a connection; `longrun.runtime` owns that, above `core/`, because it
constructs an `AdapterRegistry` (ADR 0014). And **nothing here renders**: the sheet is
written by the caller, which is also where `typer` lives.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from longrun.core.data.cache import CacheMiss
from longrun.core.data.file_store import LayerNotFound
from longrun.core.geo.dem import elevation_profile, sample_elevation
from longrun.core.geo.matching import assign_way_ids
from longrun.core.geo.segments import corridor, segment_route
from longrun.core.models.context import ScorerContext
from longrun.core.models.coverage import CoverageEntry, CoverageManifest
from longrun.core.models.geometry import Route, Segment
from longrun.core.models.measurement import ScorerResult
from longrun.core.models.plan import Manifest, Plan
from longrun.core.models.profile import PreferenceProfile
from longrun.core.models.request import PlanRequest
from longrun.core.models.verification import VerifyReport
from longrun.core.pacing.model import pacing_model
from longrun.core.plan.arbitrate import residual_flags
from longrun.core.scorers.registry import run_scorers
from longrun.core.verify.runner import gpx_verify


@dataclass(frozen=True)
class ScoredRoute:
    """What one pass produced, before anyone decides what to do about it.

    `route` is returned rather than assumed to be the input: map matching answers with a
    *different* point list, and elevation is written onto the points from the terrain model
    (scope 7.1) rather than read from the GPX.

    `caveats` is carried here rather than left with the `ETAVector` because it is the only
    output of a pass that has nowhere else to go - `Plan` has no field for it, so today
    `longrun export` re-renders a stored plan without the scope 6.2 extrapolation warning.
    M5.2 moves it onto `Plan`; until then it at least survives the function boundary.

    `coverage` is per pass and not per context, for the reason `score_once` gives.
    """

    route: Route
    segments: list[Segment]
    results: list[ScorerResult]
    etas: list[datetime]
    elevation: Any = None
    verify: VerifyReport | None = None
    caveats: list[str] = field(default_factory=list)
    coverage: CoverageManifest = field(default_factory=CoverageManifest)


def score_once(
    route: Route,
    request: PlanRequest,
    ctx: ScorerContext,
    *,
    start_at: datetime,
    router: Any = None,
    manifest: Manifest | None = None,
) -> ScoredRoute:
    """Score a route once against an open context.

    `router` changes exactly one thing: where way ids come from. Repair mode has none and
    snaps geometrically (`core.geo.matching`); generate mode has the router that drew the
    route, so `POST /match` returns real `osm_way_id` values per edge - risk R4's finding,
    and what scope 6.2's accepted-road set is built from.

    **Coverage belongs to the pass, not to the context**, and that is what makes a context
    reusable across candidates. `CoverageManifest.record` is a bare append, so scoring four
    candidates against one shared manifest would put four copies of every entry into the
    block `expectation.digest` compares verbatim. It is also the wrong claim: a candidate
    that was scored and rejected is not what the sheet reports having checked.
    """
    outer = ctx.coverage
    ctx.coverage = CoverageManifest()
    external_before = len(getattr(ctx.cache, "calls", ()))
    try:
        scored = _score_pass(
            route, request, ctx, start_at=start_at, router=router, manifest=manifest
        )
        if manifest is not None:
            # The cache logs every external call; the manifest wants the ones this pass
            # made. Taken by position rather than by clearing the log, because the cache
            # belongs to the plan and outlives the pass - scope 8.1 step 6 runs several.
            for call in list(getattr(ctx.cache, "calls", ()))[external_before:]:
                manifest.record(call)
        return replace(scored, coverage=ctx.coverage)
    finally:
        ctx.coverage = outer


def _score_pass(
    route: Route,
    request: PlanRequest,
    ctx: ScorerContext,
    *,
    start_at: datetime,
    router: Any,
    manifest: Manifest | None,
) -> ScoredRoute:
    """The pass itself, with `ctx.coverage` already scoped to it by `score_once`."""
    # Repair mode has no router by design (scope 6.1), so it snaps geometrically instead;
    # without that every way-tag scorer reads `unknown` for the whole route and the
    # commonest entry mode is the least useful.
    matched = matched_way_ids(route, router) if router is not None else None
    snapped: list[float] | None
    if matched is not None:
        # The matcher returns the route **snapped to the graph**, which is a different
        # point list from the one it was given - 268 points for 228 in, on a route the same
        # router had just drawn. That snapped geometry is the answer, not a by-product: it
        # is the one that is provably on the network.
        route, way_ids = matched
        known = sum(1 for w in way_ids if w is not None)
        segments = segment_route(route, way_ids=way_ids)
        ctx.coverage.record(
            CoverageEntry(
                source="way_matching",
                kind="osm_tags",
                checked=True,
                reason=f"{known} of {len(way_ids)} points matched by the router",
                confidence=round(known / len(way_ids), 3),
            )
        )
        # Zero for **every** point, including the ones with no way id. The matched geometry
        # is edge geometry: `/match` returns the path *through the graph*, so each of its
        # points lies on a way whether or not an `osm_way_id` detail range happened to
        # cover it - the ranges are half-open and leave gaps at junctions.
        snapped = [0.0] * len(route.points)
    else:
        match = match_ways(route, ctx)
        segments = segment_route(route, way_ids=match.way_ids if match else None)
        snapped = list(match.distances_m) if match is not None else None
        if match is not None:
            ctx.coverage.record(matching_coverage(match))

    # sample_elevation already returns all-None where the DEM has no coverage, which is
    # what "unknown elevation" looks like downstream (scope 12).
    elevations = sample_elevation(route, ctx.rasters)
    elevation = elevation_profile(route, elevations)
    # Scope 7.1: elevation comes from the terrain model, never from the GPX. Writing it
    # onto the stored route makes that true of `plan.json` too, so a later `export` or
    # `route_diff` reads the same numbers this run scored - without the DEM.
    route = route.model_copy(
        update={
            "points": [
                point.model_copy(update={"ele_m": value})
                for point, value in zip(route.points, elevations, strict=True)
            ]
        }
    )

    # The profile's curves, which until M5.10 nothing passed: `pacing_model` has taken a
    # `curves` argument since M1 and every production call let it default, so every plan
    # ever produced ran on the population curve whether or not history existed.
    eta_vector = pacing_model(
        route, start_at, curves=ctx.profile.pacing.value, elevations=elevations
    )
    results = run_scorers(route, segments, ctx, eta_vector.etas, manifest)

    # Verification runs before the plan is assembled so the plan can carry its own report:
    # `plan.json` is the golden-test artifact and the scope 10.3 API contract, and a
    # verification the plan cannot report is one every later consumer has to recompute from
    # inputs it may no longer have.
    report = gpx_verify(
        route,
        request,
        segments=segments,
        results=results,
        etas=eta_vector.etas,
        elevations=elevations,
        snapped_distances_m=snapped,
    )
    if manifest is not None:
        # Scope 6.4: "budgets are recorded in the manifest". These two fields have existed
        # since M1 and were written by nothing and read by nothing, so every plan ever
        # produced reported zero external calls whether or not it made any.
        manifest.api_calls_used = ctx.budget.api_calls_used
        manifest.imagery_tiles_used = ctx.budget.imagery_tiles_used

    return ScoredRoute(
        route=route,
        segments=segments,
        results=results,
        etas=eta_vector.etas,
        elevation=elevation,
        verify=report,
        caveats=list(eta_vector.caveats),
    )


def build_plan(
    scored: ScoredRoute,
    request: PlanRequest,
    *,
    profile: PreferenceProfile,
    coverage: Any,
    manifest: Manifest,
    plan_id: str | None = None,
) -> Plan:
    """Assemble the stored plan from one pass's output."""
    return Plan(
        id=plan_id or f"{scored.route.id}-{uuid.uuid4().hex[:8]}",
        request=request,
        route=scored.route,
        segments=scored.segments,
        results=scored.results,
        etas=scored.etas,
        residual_flags=residual_flags(scored.results, scored.segments, scored.route.length_m),
        coverage=coverage,
        profile=profile,
        verify=scored.verify,
        elevation=scored.elevation,
        manifest=manifest,
        pacing_caveats=scored.caveats,
    )


def match_ways(route: Route, ctx: ScorerContext) -> Any:
    """Snap the route to OSM ways, or None when there is no ways layer to snap to."""
    try:
        ways = ctx.layers.ways_in_corridor(corridor(route))
    except (LayerNotFound, FileNotFoundError):
        ctx.coverage.record(
            CoverageEntry(
                source="way_matching",
                kind="osm_tags",
                checked=False,
                reason="no ways layer available to match the route against",
            )
        )
        return None
    return assign_way_ids(route, ways)


def matching_coverage(match: Any) -> CoverageEntry:
    """Report how much of the route carries way tags at all (scope 3.6, 12).

    Recorded on **every** run, and that is a correction rather than a tidy-up. Until the
    OSM loader landed there was no real corridor to notice it on, and the entry was written
    only when `is_usable` was False - below 50%. The first real corridor snapped 62% of its
    points, so 38% of the route had no tags, every tag-driven scorer quietly described the
    other 62%, and the sheet said nothing at all.

    That is precisely the failure mode scope 12 names: a partial answer presented as a whole
    one. A silence at 62% and a warning at 49% is a cliff no reader can see.
    """
    rate = match.match_rate
    if rate >= 1.0:
        return CoverageEntry(
            source="way_matching",
            kind="osm_tags",
            checked=True,
            reason=None,
            confidence=1.0,
        )
    unmatched = len(match.way_ids) - match.matched
    tail = (
        "tag-driven scorers cover a minority of this route"
        if not match.is_usable
        else f"{unmatched} point(s) carry no way tags"
    )
    return CoverageEntry(
        source="way_matching",
        kind="osm_tags",
        checked=match.is_usable,
        reason=(
            f"{rate:.0%} of route points snapped to a way within {match.tolerance_m:.0f} m; {tail}"
        ),
        confidence=round(rate, 3),
    )


def matched_way_ids(route: Route, router: Any) -> tuple[Route, list[int | None]] | None:
    """The snapped route and its per-point way ids, or None when the matcher could not say.

    Returns both, because the matcher's answer is a *different route*: it snaps each point
    onto the graph and returns the resulting geometry, which has its own point count. Using
    the ids against the original points would put every tag on the wrong point.

    None rather than a list of Nones, so the caller falls back to geometric snapping: a
    matcher that returned nothing is a reason to use the fallback, not a reason to score a
    route with no tags at all.
    """
    try:
        matched, way_ids = router.map_match(route)
    except CacheMiss:
        # Re-raised for the reason `map_match` gives: an offline miss must be loud, and
        # this handler is the second place it would otherwise become a silent fall-back.
        raise
    except Exception:  # noqa: BLE001 - matching is optional; the route is not
        return None
    if not way_ids or len(way_ids) != len(matched.points) or not any(way_ids):
        return None
    return matched, list(way_ids)


__all__ = ["ScoredRoute", "build_plan", "score_once"]
