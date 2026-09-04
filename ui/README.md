# Web UI

Scope 10.3. Not scaffolded yet — deliberately. Chat is poor at spatial review and the HTML
plan sheet is read-only; this is the editable version, and it stays small.

Stack: React + MapLibre GL against `src/longrun/api/` (FastAPI over the same job runner the
CLI and MCP server use). The plan schema in `core/models` is the API contract.

Planned surface:
- Map view: route, flagged segments colored by tier with hover reasons, alternatives as
  dashed lines with score deltas, water/toilet/bailout/crew markers, sun and hostility overlays
- Direct manipulation: click a flagged segment to choose an alternative (auto-locks), drag
  to add a via point, select a range to lock, draw an avoid polygon — each action re-runs
  only the affected scorers
- Timeline strip: elevation, ETA, temperature/WBGT, shade fraction, service gaps, daylight,
  aligned by distance
- Start-time slider driving `start_time_optimizer`
- Preference panel showing provenance; edits are `stated`
- Chat pane on the same agent, next to the map rather than instead of it
- Plan list with `refresh_plan`, `route_diff`, exports
- Region status: layers loaded, resolution, adapter coverage

Every one of these calls a tool that already exists in `tools/` and works from the CLI.
