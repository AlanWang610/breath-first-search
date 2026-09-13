"""Output formats (scope 7.8, 9).

Planned modules:
    gpx.py           GPX 1.1 track plus waypoints (water, toilets, bailouts, hazards,
                     gates, distance markers)
    fit.py           FIT course file with course points for Garmin turn-by-turn
    tcx.py           TCX
    sheet_md.py      plan sheet as markdown
    sheet_html.py    self-contained HTML: embedded MapLibre map, elevation profile,
                     flagged segments, alternatives as dashed lines, no server required
    render.py        static map image per segment for human review
    attribution.py   per-source attribution required by scope 14, rendered into every sheet
"""
