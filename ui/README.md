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

**No basemap by default.** Raster tiles come from a server with a section 14 attribution
obligation and usually a key. The map renders fully on a blank ground and treats a basemap
as an enhancement — the same position M2 took for the HTML plan sheet. Set
`VITE_BASEMAP_STYLE` to a style URL you are entitled to use.

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
