"""Output formats (scope 7.8, 9).

Modules:
    course.py        what the two course writers share, and the rule that keeps them
                     honest: a course point sits at the ROUTE position for its
                     `cum_dist_m`, while the GPX keeps the thing's TRUE position. The
                     device cap and the short-name scheme live here too
    fit.py           FIT course file with course points for Garmin turn-by-turn, written
                     by hand against the profile `fitdecode` already ships (ADR 0028)
    tcx.py           TCX course. Where both degradation rules live, because its PointType
                     enumeration is closed and its name field caps at ten characters
    sheet_md.py      plan sheet as markdown
    sheet_html.py    self-contained HTML: elevation profile, flagged segments, an SVG map
                     drawn from our own layers. Deliberately **not** MapLibre and not a
                     basemap, for the reason that module's own docstring gives: a tile
                     fetched at view time is a server, and scope 9 asks for a file that
                     needs none
    attribution.py   per-source attribution required by scope 14, rendered into every sheet

There is no `gpx.py` here and there should not be: GPX reading and writing is
`core/geo/gpx.py`, because the same code parses the user's input route in repair mode. This
list named one from M0 until M10, which is how a planned-modules list turns into a claim that
work is outstanding when it is not.

`render.py` is likewise struck. Scope 7.9 asks for "a static map image per segment", and the
`render` MCP tool still reports itself unavailable — but the thing to build is a shared
renderer promoted out of `sheet_html._route_svg`, not a module that composites basemap tiles.
A raster costs 6-12 tiles against a 10-tile-per-plan budget that exists for the vision
spot-check, and a PNG travels without its HTML so the §14 credit would have to be burned into
the pixels.
"""
