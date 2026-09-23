# 0034 — The database contract tier runs on one CI leg, and the asymmetry is the decision

Status: **accepted**, M13, 2026-09-22.

## Context

Forty-four tests in `tests/contract/` need a live PostgreSQL with PostGIS. They have never
run anywhere except a developer's laptop, and five module docstrings said why: *"CI has no
services, by design (the `LayerStore` seam is what makes that possible)."*

That sentence conflated two true things. The `LayerStore` seam **is** what keeps the golden
suite hermetic and fast, and that is worth keeping. It is not a reason the contract tests
cannot run — it is the reason they do not have to run for the rest of the suite to mean
anything. The tests that check the seam itself are precisely the ones the argument does not
cover: `test_layer_store_equivalence` exists to make "the golden suite passes" a statement
about PostGIS, and until now nothing in CI had ever run it.

Four of the five modules seed their own scratch schemas from hand-built frames, so they need
an *empty* database rather than a loaded region. An empty database is a container image.

## Decision

**A separate `contract` job on `ubuntu-latest`, with a `postgis/postgis:17-3.5` service
container, selected by a new `postgis` marker.** It runs 44 tests: `test_postgis_schema` (8),
`test_layer_store_equivalence` (11), `test_freeze_fixture` (9), `test_national_load` (8) and
`test_osm_load` (8).

**A separate job rather than a third matrix leg, because Windows cannot host one.** GitHub's
`services:` are Docker containers and the Windows runner does not run Linux containers: a
`services:` block on `windows-latest` fails the job before a step executes. The roadmap that
scheduled this work asked for "the CI run proving the Postgres service works on both matrix
legs", and that run cannot exist. So the contract tier is covered on one platform and not the
other, permanently, and this file is where that is written down rather than left for somebody
to rediscover in a failed job.

**The init SQL is applied by `psql` after checkout, not mounted.** A service container starts
before `actions/checkout`, so there is no repository to mount `deploy/postgis/init/` from.
Running the same file afterwards executes the same statements — and it is strictly the better
test: locally that file runs *once*, against an empty data directory, so a container brought
up months ago is indistinguishable from a fresh one until a loader fails. In CI it runs
against a fresh image every time, which makes `test_postgis_schema` a check on the committed
SQL rather than on somebody's volume.

**Both markers, never one.** These modules carry `network` *and* `postgis`. `network` is what
the default gate deselects on, and dropping it would put 44 database tests into
`pytest -m "not network and not slow"`, where they would skip on every machine without a
database and quietly stop being a gate. What `postgis` adds is *which kind* of live service,
and that is the whole distinction this decision rests on: a Postgres container is a thing CI
can start; the USGS tile server, a DOT's WZDx feed and an NPS key are not.

## Consequences

**The `ingest` extra is synced here and stays off the `test` job.** `test_osm_load` needs
pyosmium, and 8 of the 44 skip without it. `pyproject.toml`'s mypy override list already
explains why it cannot simply be added everywhere: *"`osmium` in particular does ship stubs,
and they are worth having… without this, `mypy` passes on a developer machine with
`--all-extras` and fails on CI"*. A type gate whose result depends on which extras happen to
be installed is not a gate, so the two jobs deliberately have different environments and only
one of them type checks.

**Three kinds of `network` test now exist and only one runs in CI.** The second is the
internet-dependent set — `test_graphhopper`, `test_wzdx`, `test_tiles_live`,
`test_forecast_providers`, `test_keyed_adapters`, `test_agent_model` — which stays out
because a test suite that fails when a state DOT has an outage is a suite people learn to
ignore. The third is `test_way_lts_table`, which needs a **built region** rather than a
schema: `longrun build-region deploy/regions/ozarks.yaml` loads 1,600 ways, and a service
container starts empty. Its seven tests remain laptop-only, and the honest way to change that
would be to commit a tiny extract and build it in CI — which is a decision about fixture
weight, not about services, and nobody has taken it.

**44, not the 43 the roadmap predicted.** The figure is recorded here because the count is
the only thing that says whether the job is doing what it was added for.

**`-m postgis` rather than a list of paths.** A new database-backed module joins the job by
carrying the marker. A path list in a workflow file is the kind of thing that goes stale
silently — the module gets written, the workflow is not edited, and the test never runs
anywhere, which is exactly the state this ADR is undoing.

## What would make us revisit

**A Windows container runner, or a Postgres that runs natively on the Windows image.** The
second is real — GitHub's `windows-latest` image ships a PostgreSQL service that can be
started with `sc start postgresql` — and would close the asymmetry. It was not taken here
because it would mean a second, differently-configured database whose collation and PostGIS
version are whatever the image ships, and a contract test that passes against a different
server from the one the project deploys is a contract test about nothing. If somebody wants
the Windows leg, the thing to check first is whether that image's PostGIS matches 3.5.

**The contract job growing past its 15-minute timeout.** It is 42 s locally for 44 tests.
M6's rule applies here as everywhere else: the day this is hit, look at what got slow.
