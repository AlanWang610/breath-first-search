# breath-first-search

A long-run planner: turns a natural-language request or a user-supplied GPX into a
verified route plus a plan sheet, using a real router and geospatial tools. Point-to-point
and loop runs of 20-100 km, anywhere in the US, where traffic stress, heat, water gaps,
and bailouts decide success.

Full scope: [docs/long-run-planner-scope.md](docs/long-run-planner-scope.md).

**Status: scaffold.** Directories and package boundaries only — no implementation yet.
Dependencies in `pyproject.toml` are the intended set from the scope and have not been
resolved or installed.

## Layout

```
src/longrun/
  core/        deterministic geospatial library; zero LLM dependency; all tests live here
    models/      Pydantic types shared across layers (segments, plan, manifest, profile)
    geo/         GPX I/O, geometry, DEM/DSM/SVF raster work
    routing/     router interface + GraphHopper adapter (scope 4.3, 7.1)
    pacing/      grade-adjusted pace, fatigue drift, ETA vector (scope 6.2, 7.3)
    preferences/ preference profile: schema, provenance, safety floors (scope 6.3)
    scorers/     pure (gpx, context) -> SegmentMeasurements functions (scope 7.2, 7.4-7.6)
    verify/      gpx_verify checks (scope 7.9)
    plan/        scratchpad, manifest, arbitration, plan sheet assembly (scope 8.4, 9)
    export/      GPX 1.1, FIT course, TCX, HTML plan sheet (scope 7.8, 9)
    data/        PostGIS, raster store, external-API cache clients (scope 5)
  adapters/    jurisdiction-specific sources by tier, discovered via entry points (scope 7.10)
  tools/       MCP server wrapping core; one surface for CLI, chat, orchestrator, web UI
  agent/       the scope 8 planning loop as explicit code; LLM called at fixed points
  cli/         `longrun` Typer app; the test harness (scope 10.1)
  jobs/        async job runner with progress events (scope 4.4)
  regions/     build_region steps and coverage report (scope 13)
  api/         FastAPI over the job runner, backing the web UI (scope 10.3)
ui/            React + MapLibre client (scope 10.3)
deploy/        GraphHopper and PostGIS containers, per-region config
tests/         unit, golden routes, adapter contract, human-judged eval set (scope 11)
docs/          scope, architecture decisions
data/          region builds and caches; gitignored
```

## Ground rules these boundaries enforce

- **The LLM does not draw routes.** `core/` has no model in the loop and no import from
  `agent/`. Every golden test runs through the CLI against `core/` alone.
- **Scorers measure; preferences assign cost.** Nothing in `core/scorers/` attaches a sign
  to a measurement — that is `core/preferences/` plus the arbitration in `core/plan/`.
- **Router behind an interface.** Only `core/routing/` knows GraphHopper exists.
- **Tool and CLI before UI.** A capability lands in `core/` -> `tools/` -> `cli/` before
  `api/` or `ui/` gets a control for it.
- **The only state-specific code lives in `adapters/jurisdictions/`.**

## Licensing

Code is MIT (see `LICENSE`). Some *outputs* are not: OSM-derived artifacts (the offline
LTS table, a distributed GraphHopper graph) are derivative databases under ODbL, and
several layers carry attribution requirements. See scope section 14; the plan sheet and
web UI render attribution per source.
