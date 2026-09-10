"""Budget measurements, kept standing (scope 6.4, risks R3 and R5).

Two spikes live here. **S3** asked whether the DSM ray-cast fits the ~3-minute budget;
**R5** asks whether the *whole plan* does, which is a different question that was written
before any scorer existed and went unmeasured until twelve of them existed.

A number in an ADR decays the moment someone changes the inner loop, so the measurements
live here instead: `slow`-marked, excluded from CI by the marker that exists for exactly
this, and run on demand.

    uv run pytest tests/unit/test_raycast_budget.py -m slow

The thresholds are deliberately loose — roughly ten times the measured figure. This is a
guard against a regression of *kind* (a Python loop creeping into the ray march, the tiled
extraction being replaced by a bbox), not a benchmark to tune against. A tight threshold
here would fail on a busy laptop and get deleted.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from longrun.core.geo.raycast import LADDER, horizon_profile

pytestmark = pytest.mark.slow

#: Scope 5's corridor buffer, plus the search radius the rays need beyond it. A tile
#: narrower than this truncates its own horizons at the edge.
BUFFER_M = 400.0
MARGIN_M = 500.0

#: Route covered by one tile. Small enough that peak memory stays flat with route length,
#: large enough that the per-tile overhead does not dominate.
TILE_M = 2000.0

#: A 100 km route: scope 1's upper bound, and the worst case that has to fit.
LONGEST_ROUTE_M = 100_000.0

#: Finest DSM scope 7.4 asks for ("1 m where LiDAR exists").
FINEST_CELL_M = 1.0

#: Ten times the measured 0.46 s. See the module docstring on why it is not tighter.
BUDGET_S = 5.0

#: The point of tiling. A 100 km corridor bbox at 1 m is 10 GB; the ribbon is 720 MB; one
#: tile is 22 MB, and tiles are what is ever resident.
MAX_TILE_MB = 100.0


def _tile(cell_m: float) -> tuple[np.ndarray, int, int]:
    rows = int(2 * (BUFFER_M + MARGIN_M) / cell_m)
    cols = int((TILE_M + 2 * MARGIN_M) / cell_m)
    rng = np.random.default_rng(20260909)
    return rng.uniform(0.0, 30.0, (rows, cols)).astype(np.float32), rows, cols


def test_a_hundred_kilometre_route_ray_casts_inside_the_budget() -> None:
    """Full rung, 1 m cells, 100 m point spacing — no degradation, worst case.

    Scope 6.4 allows the resolution to fall before the time budget is exceeded. This test
    is what says it does not have to: the finest configuration the scope asks for fits, so
    the ladder in `raycast.LADDER` is a capability held in reserve rather than a path the
    normal case takes.
    """
    tile, rows, cols = _tile(FINEST_CELL_M)
    tiles = int(LONGEST_ROUTE_M / TILE_M)
    points_per_tile = int(TILE_M / 100.0)

    sample_rows = np.full(points_per_tile, rows / 2.0)
    sample_cols = np.linspace(
        MARGIN_M / FINEST_CELL_M, (TILE_M + MARGIN_M) / FINEST_CELL_M, points_per_tile
    )

    start = time.perf_counter()
    for _ in range(tiles):
        horizon_profile(tile, FINEST_CELL_M, sample_rows, sample_cols, LADDER[0])
    elapsed = time.perf_counter() - start

    assert elapsed < BUDGET_S, (
        f"{tiles} tiles took {elapsed:.2f}s against a {BUDGET_S}s allowance; "
        "the ~3-minute plan budget has twelve scorers and up to five reroute rounds in it"
    )


def test_one_tile_stays_small_however_long_the_route_is() -> None:
    """Peak memory is a property of the tile, not the route.

    This is the finding the spike actually turned on: the ray-cast was never the
    constraint, the extract was. A corridor bbox for a 100 km route at 1 m is 10 GB and a
    ribbon of tiles is 22 MB at a time.
    """
    tile, _, _ = _tile(FINEST_CELL_M)
    assert tile.nbytes / 1e6 < MAX_TILE_MB


def test_the_cost_does_not_depend_on_the_resolution_of_the_surface() -> None:
    """Cost is points x azimuths x steps. Cell size changes memory and accuracy, not time.

    Worth pinning because it is counter-intuitive and it is why the ladder's `step_m` and
    `azimuths` are the knobs that would ever buy time, and DSM resolution is not.
    """
    fine, rows_f, _ = _tile(1.0)
    coarse, rows_c, _ = _tile(4.0)
    points = 20

    def run(surface: np.ndarray, cell: float, rows: int) -> float:
        cols = np.linspace(MARGIN_M / cell, (TILE_M + MARGIN_M) / cell, points)
        best = float("inf")
        for _ in range(3):
            start = time.perf_counter()
            horizon_profile(surface, cell, np.full(points, rows / 2.0), cols, LADDER[0])
            best = min(best, time.perf_counter() - start)
        return best

    fine_s = run(fine, 1.0, rows_f)
    coarse_s = run(coarse, 4.0, rows_c)
    # Sixteen times fewer cells, within a factor of three on time. The residual is cache
    # behaviour, not arithmetic.
    assert 0.33 < fine_s / coarse_s < 3.0, f"fine {fine_s:.3f}s vs coarse {coarse_s:.3f}s"


# --- R5: the whole plan, not just the ray-cast ------------------------------

#: Scope 6.4's target. The budget is for the *plan*, and scope 8.1 allows up to five
#: reroute rounds inside it, each of which re-scores.
PLAN_BUDGET_S = 180.0

#: Scope 8.1 step 9 sends verification failures back to rerouting, capped at five rounds.
REROUTE_ROUNDS = 5


def test_a_hundred_kilometre_plan_leaves_room_for_five_reroute_rounds() -> None:
    """The measurement R5 asked for, which nobody had made.

    `sun_exposure` is the whole cost: on a real 2 km corridor it was 2.00 s of a 2.01 s
    total across twelve scorers. It scales linearly at ~0.27 s per DSM tile, so a 100 km
    route is ~13.5 s — and five reroute rounds of that still fit inside the ~3-minute
    budget with the forecast cached after the first round.

    Asserting the shape rather than re-running a 100 km route: the per-tile cost is what
    stays constant, and that is what a regression would break.
    """
    tile, rows, cols = _tile(FINEST_CELL_M)
    points_per_tile = int(TILE_M / 100.0)
    sample_rows = np.full(points_per_tile, rows / 2.0)
    sample_cols = np.linspace(
        MARGIN_M / FINEST_CELL_M, (TILE_M + MARGIN_M) / FINEST_CELL_M, points_per_tile
    )

    start = time.perf_counter()
    horizon_profile(tile, FINEST_CELL_M, sample_rows, sample_cols, LADDER[0])
    per_tile = time.perf_counter() - start

    tiles_in_100km = int(LONGEST_ROUTE_M / TILE_M)
    one_pass = per_tile * tiles_in_100km
    assert one_pass * REROUTE_ROUNDS < PLAN_BUDGET_S, (
        f"{one_pass:.1f}s per scoring pass x {REROUTE_ROUNDS} rounds exceeds the "
        f"{PLAN_BUDGET_S:.0f}s plan budget"
    )


def test_the_cost_is_linear_in_route_length_not_worse() -> None:
    """Per-tile cost constant means a 100 km route is predictable from a 20 km one.

    The property that makes the budget arithmetic above valid. If tiling ever became
    superlinear — a global spatial index rebuilt per tile, say — this is what would catch
    it, and the plan budget would stop being derivable from one measurement.
    """
    tile, rows, _ = _tile(FINEST_CELL_M)

    def cost(points: int) -> float:
        sample_rows = np.full(points, rows / 2.0)
        sample_cols = np.linspace(
            MARGIN_M / FINEST_CELL_M, (TILE_M + MARGIN_M) / FINEST_CELL_M, points
        )
        best = float("inf")
        for _ in range(3):
            start = time.perf_counter()
            horizon_profile(tile, FINEST_CELL_M, sample_rows, sample_cols, LADDER[0])
            best = min(best, time.perf_counter() - start)
        return best

    ratio = cost(80) / max(cost(20), 1e-9)
    # Wide on purpose. Four times the points should be about four times the work;
    # quadratic would be sixteen. A tight band on a wall-clock ratio fails on a busy
    # laptop and then gets deleted, which is worse than a loose one that still catches
    # a change of complexity class.
    assert 1.5 < ratio < 12.0, f"four times the points cost {ratio:.1f}x, not ~4x"
