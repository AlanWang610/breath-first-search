# Web UI

Scope 10.3, built in M7. React 19 + MapLibre GL over `src/longrun/api/` (FastAPI on the
same job runner the CLI and MCP server use). The plan schema in `core/models` is the API
contract.

```bash
uv sync --extra api
uv run longrun api                  # http://127.0.0.1:8000

cd ui && npm install
npm run dev                         # http://localhost:5173, proxying /api
npm run typecheck                   # tsc --noEmit, over src/ and gestures/
npm test                            # vitest run — the pure functions in src/lib
npm run test:gestures               # playwright — the gestures, in a real browser (M15)
npm run build                       # -> ui/dist
uv run longrun api --ui ui/dist     # one server, API and pages
```

`npm run test:gestures` needs a browser once: `npx playwright install chromium`. It builds
`ui/dist`, starts its own `longrun api` on port 8123 and scores its own plan from a golden,
so it needs no server running and leaves nothing behind in the tree.

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

Two suites, and they are deliberately separate (ADR 0040).

**`npm test` — the node tier (M12.8).** The functions in `src/lib/plan.ts`: everything that
is a function of a plan and returns a value. Flag ordering, tier colour, segment→`(start_m,
end_m)`, the elevation runs that keep an unmeasured stretch from being drawn as flat ground,
and the shape of a drawn polygon. `environment: "node"` and `include: ["src/**/*.test.ts"]`
are unchanged from M12, so a component still cannot reach this tier and masquerade as
gesture coverage.

**`npm run test:gestures` — the browser tier (M15).** Playwright and headless Chromium
against a real page, a real `longrun api` and a real MapLibre canvas, driving the plan it
scores for itself from `tests/golden/routes/` (ADR 0041 — nothing is committed under
`plans/` or here). It covers:

| gesture | what is checked |
|---|---|
| **click to select** | `queryRenderedFeatures` resolves the click, and the panel shows the stretch **in metres** — the range a write will carry, not just the id |
| **hover** | the popup names the scorer, kind and tier in words with a detail after them; bare ground shows nothing |
| **lock** | the clicked segment's own start and end reach `request.locked`, with `source: "user"` |
| **unlock mine** | the lock the map set is released, and the server's sentence about which one appears |
| **via** | one click, one via, inserted in route order, with the frozen-policy note |
| **avoid** | corners counted as they are placed; the polygon stored and numbered by the server; an over-cap area refused **in the server's own words with its size** |
| **choose** | a drawn line spliced in, auto-locked as the runner's, re-scored offline against the golden's fixtures, and the unmeasured middle drawn as a break rather than as flat ground |
| **timeline scrub** | the distance named, the cursor drawn, the map marker walking the route, and all of it cleared on leaving the strip |
| **the camera across a write** | a pan survives a lock, its poll and its re-render; `Fit to route` brings it back; a different plan still refits |

The last row is the one M12 flagged as the reason M12.4 exists and nothing checked.

**What is still hand-driven only.** `POST /api/plans` — the submit form. It needs a
GraphHopper server, which the hermetic suite does not have and must not reach, and driving
it against whatever a developer happens to have running is the difference between a test and
a coincidence. The map's own MapLibre controls (zoom buttons, scroll zoom, double-click
zoom) are also uncovered: they are the library's behaviour rather than this project's, and
the one thing that depends on them — that a camera the user moved stays moved — is covered
above by dragging the canvas.

**A finding worth keeping.** The first browser test that opened a real plan found that the
map drew nothing at all: `BLANK_STYLE` carried `glyphs: undefined`, MapLibre's style
validator rejected the key, `Style._load` returned early, and `load` never fired — so no
route, no basemap note, and every click and hover a silent no-op. `tsc` could not see it
(the property is optional) and the node tier could not either (nothing there constructs a
map). The second found that `Flag.tier` and `Flag.kind` are `IntEnum`s on the wire while
this client compared them to strings, so every segment on every map was painted comfort
blue. Both are fixed; neither was reachable without a browser.

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
