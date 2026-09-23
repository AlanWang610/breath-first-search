# Web UI

Scope 10.3, built in M7. React 19 + MapLibre GL over `src/longrun/api/` (FastAPI on the
same job runner the CLI and MCP server use). The plan schema in `core/models` is the API
contract.

```bash
uv sync --extra api
uv run longrun api                  # http://127.0.0.1:8000

cd ui && npm install
npm run dev                         # http://localhost:5173, proxying /api
npm run typecheck                   # tsc --noEmit
npm test                            # vitest run — the pure functions in src/lib
npm run build                       # -> ui/dist
uv run longrun api --ui ui/dist     # one server, API and pages
```

## What it is for

Chat is poor at spatial review and the HTML plan sheet is read-only. This is the editable
version, and it stays small: **every control calls a tool that already works from the CLI**
(scope 3.9), which is why this milestone was last and why it adds no capability.

## What it draws

- **Map**: the route, each segment coloured by its worst flag's **tier**, hover for the
  reason in prose. Tier and not severity — scope 8.4 makes the tiers lexicographic, so a
  gradient over severity would paint a 0.9 comfort flag and a 0.9 safety flag the same
  colour, which is the comparison arbitration exists to refuse.
- **Timeline**: elevation and flagged segments against distance, and a line naming the
  series it *could not* draw.
- **Panels**: acceptance metrics, choices left to you, warnings, coverage, preferences with
  provenance, region status.
- **Jobs**: submit a plan, watch its events, and answer a same-tier trade-off when the loop
  parks on one. The user chooses; the model only ever wrote the comparison (ADR 0019).

## What it edits (M12)

Click a segment to select a stretch — the selection is a pair of **distances**, never a
segment id, because `segment_id` is positional and an id read before an edit names
different ground after one (ADR 0032). Then:

- **Lock** / **Unlock mine** the selected stretch. "Mine" leaves the loop's own reroute
  locks standing, which is what `LockedRange.source` was added for.
- **Replace this stretch**: click along the line you want instead, and the server splices
  it in, auto-locks it as your choice (ADR 0019), and re-scores in one job.
- **Add a via**: one click.
- **Draw an avoid area**: click the corners. The polygon goes up **unrounded** — rounding
  at `AREA_PRECISION` and the 4 km² cap are the server's, in `core.routing.avoid`, because
  an avoid area travels inside `custom_model` and the router's cache key is hashed from it.
  An area over the cap comes back refused with its size, which is something you can act on
  by drawing a smaller one.

Every one of those is a `POST /api/plans/{id}/...` that wraps the same `core.plan.edits`
function `longrun edit` calls, and every one takes an **id and never a path** (ADR 0036).
Each returns a job, so an edit polls exactly the way a plan does.

**A via and an avoid area do not move the line, and the panel says so.** `RoutingPolicy` is
frozen and resolved once, persisted so a resume in another process cannot compute a
different one; patching an area into the policy the first half of a line was drawn under
would buy a line that is half one thing and half another. The honest option is a whole
re-route, which is a routing call — `longrun edit reroute`, from a shell with a router.

## Two decisions worth knowing

**The basemap is USGS The National Map, drawn as a layer under the route** (ADR 0023).
Public domain and keyless, so there is no scope 14 obligation to manage and no key to leak
into the bundle. The provider comes from the API (`GET /api/basemap`), so there is one
setting for the map and for `imagery_tile`:

```bash
LONGRUN_TILE_PROVIDER=usgs-topo uv run longrun api --ui ui/dist   # or usgs-imagery (default), none
```

It is added as a raster *layer* on top of an inline blank style, never loaded as the style
itself. A style that cannot be fetched leaves MapLibre with nothing, route included; a layer
that cannot be fetched leaves the dark ground with the route still drawn on it. Coverage is
the US, and tiles stop at zoom 16 — past that MapLibre scales the last real tile up.

A keyed street-map provider works through `LONGRUN_TILE_PROVIDER=custom` with
`LONGRUN_TILE_URL` and `LONGRUN_TILE_ATTRIBUTION`. Its key reaches the browser, so restrict
it to your own domain.

**Absence is rendered as absence.** `detour_ratio: null` reads "not measured", never `0`
and never `1.0`. A coverage entry that was not checked is listed with its reason. The
preference table shows which axes a runner stated, which history inferred, and which are
still defaults — scope 6.3 permits an in-context question only about the third kind. The
backend spends real effort keeping unknown and absent apart, and a UI is the last place
that work can be quietly thrown away.

## What is verified, and what is not

`npm test` covers the functions in `src/lib/plan.ts` — everything that is a function of a
plan and returns a value: flag ordering, tier colour, segment→`(start_m, end_m)`, the
elevation runs that keep an unmeasured stretch from being drawn as flat ground, and the
shape of a drawn polygon. That is the first automated check this directory has ever had.

**Nothing verifies a gesture.** A click that selects a segment, a click that places a
polygon corner, a drag on the MapLibre canvas, the popup on hover, the camera staying put
across a write — none of those have automated coverage, here or anywhere, and a headless
run cannot give them any. They were exercised by hand against a stored plan with
`uv run longrun api --ui ui/dist`. Mounting a component to assert that it rendered would
have raised the number without changing that sentence, so there is none.

## Not built

**The chat pane**, which wants the MCP session and the browser talking to one agent.
`api/` reaches `tools/` only by importing the same `core/` functions those tools wrap,
never through `MCPServer` itself — that is a second integration, not a panel.

**A re-route endpoint.** `longrun edit reroute` needs a GraphHopper server, which the
hermetic suite does not have and must not reach, so an endpoint for it would be an
untested write path. The UI says what to run instead rather than offering a button that
only works on a developer's machine.

The earlier version of this section said `api/` exposed no write path and that "what is
missing is the endpoints and the map interactions, not the capability". **That sentence
was optimistic when it was written.** It held for lock and mostly for avoid polygons; the
rest had no capability behind them — unlocking a range did not exist, a via point had no
owner that could edit a stored request, and *"each action re-runs only the affected
scorers"* could not be done honestly at all, because `segment_id` is positional and every
carried measurement downstream of an edit silently re-pointed at different ground (ADR
0032). M11 made it true from a command and M12 is the endpoints and the interactions it
promised.
