# 0003 — Horizon profiles, tiled along the corridor, in numpy

Status: accepted, 2026-09-09. Retires spike S3 and risk R3.

## Context

Risk R3 was the reason M2 opens with a spike: *"500 points × sun positions × azimuths ×
range steps over a 2 m DSM runs to 10⁸–10⁹ operations naively."* Two questions had to be
settled before `sun.py`, `dsm.py` and `svf.py` were written against either answer:

1. **Which design** — one 360° horizon profile per point, reused across all times, or one
   ray per (point, time)?
2. **Which resolution fits the ~3-minute budget** (scope 6.4), and where are the rungs of
   the degradation ladder?

Both were measured against `core/geo/raycast.py` on this machine. The surface is synthetic
— random blocks at urban density — because the timing is dominated by gathering scattered
cells out of a large array, and a synthetic grid has the same cache behaviour as a real
one. Accuracy is a separate question, pinned by `tests/unit/test_raycast.py` against
geometry with closed-form answers.

## What the measurement said

**The ray-cast is not expensive.** 500 points, 72 azimuths, 500 m radius, 2 m steps —
the finest configuration scope 7.4 asks for, no degradation:

| route | points | rung `full` | `reduced` | `coarse` | `minimal` |
|---|---|---|---|---|---|
| 20 km | 200 | 0.09 s | 0.03 s | 0.01 s | 0.00 s |
| 50 km | 500 | 0.24 s | 0.08 s | 0.03 s | 0.00 s |
| 100 km | 1000 | 0.55 s | 0.16 s | 0.05 s | 0.01 s |

Vectorised over points and azimuths with the range march as the loop, that is a few
hundred numpy operations over an array of a few tens of thousands. The 10⁸–10⁹ figure in
R3 is right about the operation count and wrong about what it costs.

**Cost does not depend on DSM resolution.** It is `points × azimuths × steps`. Cell size
changes memory and accuracy; it does not change the time. This is why the ladder's knobs
are `azimuths` and `step_m`, and why "degrade to a 5 m DSM" — which is what §6.4 implies
— would buy almost nothing.

**The constraint is memory, and it is entirely about how the corridor is extracted:**

| route | cell | bbox (square of side L/2) | ribbon along the route | ratio |
|---|---|---|---|---|
| 20 km | 1 m | 400 MB | 144 MB | 3× |
| 50 km | 1 m | 2,500 MB | 360 MB | 7× |
| 100 km | 1 m | 10,000 MB | 720 MB | 14× |
| 100 km | 2 m | 2,500 MB | 180 MB | 14× |

And a ribbon does not have to be resident all at once. Tiled — 2 km of route plus a 500 m
margin each side, at 1 m — one tile is **1800 × 3000 cells, 22 MB**, and a 100 km route is
50 of them:

```
 20 km = 10 tiles × 9.3 ms = 0.09 s    peak resident 22 MB
 50 km = 25 tiles × 9.3 ms = 0.23 s    peak resident 22 MB
100 km = 50 tiles × 9.3 ms = 0.46 s    peak resident 22 MB
```

**The design question comes out the other way on performance.** A single ray per point is
**33× cheaper** than a full profile (4.7 ms against 157 ms for 500 points). A plan scores
one arrival time per point, so for `sun_exposure` alone the single ray is the cheaper
thing by a wide margin, and break-even against `start_time_optimizer` is at **33 start
times** — a sweep is nearer 8.

## Decision

**Compute horizon profiles, not per-time rays** — but for the reason the numbers support,
not the one the plan gave. Scope 7.4 asks for direct *and* diffuse irradiance, and the sky
view factor is an integral over the horizon profile. A single ray answers the direct half
and leaves the diffuse half needing exactly this computation. The profile being reusable
across start times and cacheable per tile (scope 5) is a bonus, not the argument.

The build plan's stated rationale — that reuse across `start_time_optimizer` makes the
profile "dramatically better" — is wrong at realistic sweep sizes. Right call, wrong
reason, and worth recording as such.

**Extract the corridor as tiles along the route, never as a bbox.** This is the actual
output of the spike. `core/geo/dsm.py` tiles at ~2 km of route with a margin equal to the
ray-cast search radius, so no ray leaves its own tile — `horizon_is_truncated` exists to
catch the case where one does, because a ray that runs off the raster reports open sky and
scope 3.6 needs that apart from a genuinely open one.

**No numba.** The `raster = ["numba>=0.60"]` extra was declared for this ray-cast and is
not needed for it. numpy does the worst case in half a second against a ~180 s budget, so
a JIT buys nothing that can be spent — and it would add a compile on first call of roughly
the same order as the whole computation. The extra stays declared and locked (numba 0.67.0
resolves fine on 3.13); nothing imports it, and this ADR is why.

**Keep the degradation ladder, and expect never to descend it.** Scope 6.4 requires the
capability and `RaycastSettings` provides it with the rung named for the manifest. But the
finest configuration fits with three orders of magnitude of headroom, so a plan that
reports a degraded rung is reporting something surprising, not something routine.

## Consequences

* `sun_exposure` can be written against 1 m data where LiDAR exists, as scope 7.4 asks,
  without a resolution caveat.
* The thing that will actually constrain shade measurement is **DSM availability** —
  canopy height and building heights — not compute. That risk is not retired and is not
  what R3 was about.
* `tests/unit/test_raycast_budget.py` keeps the measurement standing, `slow`-marked so it
  stays out of CI. Thresholds are ~10× the measured figures: it guards against a
  regression of kind (a Python loop in the march, a bbox extract replacing tiles), not
  against a few milliseconds.

## What would make us revisit

A region-wide SVF product, rather than per-corridor computation, changes the arithmetic
completely — scope 5 already rejects it at 10⁹–10¹⁰ pixels per metro, and this measurement
supports that: per-corridor is cheap enough that precomputation buys nothing.

If `start_time_optimizer` turns out to sweep more than ~33 start times, the profile wins on
performance too, and this ADR's central caveat simply stops mattering.
