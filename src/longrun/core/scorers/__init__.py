"""Pure measurement functions. No scorer decides whether a condition is good or bad.

Each returns per-segment measurements plus a worst-N list with reasons. Severity is in
[0, 1] with a soft/hard marker; thresholds marked "(profile)" in scope 8.3 are read from
the preference profile, hard thresholds are fixed safety floors.

`registry.py` is the list itself - which scorers exist and the order scope 8.1 step 5 runs
them in - and it lives here rather than in `cli/` because the agent loop and the MCP tool
layer need the same list and may not import a CLI command.

Planned modules, by scope section:
    7.2 runnability   hostility.py, crossings.py, stop_density.py, surface.py, cue_sheet.py
    7.4 environment   sun.py, heat.py, microclimate.py, air_quality.py, lighting.py
    7.5 resupply      services.py, resupply_schedule.py
    7.6 access        closures.py, trail_status.py, access_hours.py, legality.py, hazards.py
    7.7 logistics     transit.py, bailouts.py, crew_points.py, cell_coverage.py,
                      start_time_optimizer.py

Scorers in 7.6 consume adapter output (scope 7.10) and must report which tiers answered
for each jurisdiction crossed, including when the answer is nothing.
"""
