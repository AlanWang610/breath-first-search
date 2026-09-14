# Web UI

Scope 10.3, built in M7. React 19 + MapLibre GL over `src/longrun/api/` (FastAPI on the
same job runner the CLI and MCP server use). The plan schema in `core/models` is the API
contract.

```bash
uv sync --extra api
uv run longrun api                  # http://127.0.0.1:8000

cd ui && npm install
npm run dev                         # http://localhost:5173, proxying /api
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

## Not built

`src/longrun/api/` exposes no write path for direct manipulation yet — dragging a via
point, drawing an avoid polygon, selecting a range to lock. Scope 10.3 lists them and the
`tools/` layer already has `lock_segment` and the avoid-polygon plumbing behind it; what is
missing is the endpoints and the map interactions, not the capability. Same for the chat
pane, which wants the MCP session and the browser talking to one agent.
