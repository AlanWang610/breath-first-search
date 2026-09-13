"""MCP server wrapping core (scope 4.1, 7).

Stateless, Pydantic-schema'd tools. The same layer serves the CLI, a chat client, the
orchestrator, and the web UI without rewrapping - a capability exists here before it gets
a UI control.

Planned modules:
    server.py        MCP server wiring
    routing.py       route, alternatives, elevation_profile, map_match
    runnability.py   segment_hostility, crossings, stop_density, surface_profile, cue_sheet
    environment.py   sun_exposure, heat_stress, microclimate, air_quality, lighting
    resupply.py      services_along, resupply_schedule
    access.py        closures, trail_status, access_hours, legality, hazards
    logistics.py     transit_at, bailouts, crew_points, cell_coverage, start_time_optimizer
    editing.py       import_route, lock_segment, pin_waypoint, route_diff,
                     distance_markers, refresh_plan, place_notes, export
    io.py            gpx_read, gpx_write, gpx_verify, imagery_tile, render
"""
