"""Plan state, arbitration, and plan sheet contents (scope 8.4, 9).

Planned modules:
    pipeline.py      one scoring pass over one route against an open context: scope 8.1
                     steps 4, 5 and 9. The loop runs it once per candidate per round
    scratchpad.py    current GPX, per-segment measurements, ETAs, locks, active profile;
                     read and written between tool calls; persisted for needs_input resume
    manifest.py      data-snapshot pins (OSM extract date, HPMS vintage, DEM resolution,
                     canopy version, GTFS feed versions, adapter versions), budgets used
    weighting.py     position weight w(d): 1.0 through 40% of distance, linear to 2.0 at
                     100%; ceiling 1.5 for heat; never applied to legality
    arbitrate.py     lexicographic tiers safety -> physiological -> comfort; weighted sum
                     within a tier; same-tier conflicts surfaced, never resolved silently
    coverage.py      which sources were checked and which were not, per jurisdiction
    diff.py          route_diff, where two lines part company; result_diff, what two
                     scorings of one line disagree about. The second half was described
                     here from M1 and written in M10, which is the milestone that needed
                     it: a refresh does not re-route, so a geometry diff of one is vacuous
    refresh.py       re-score the date-sensitive scorers against a new date and carry the
                     rest, reporting which was which (scope 7.8)
    edits.py         the two scope 10.3 gestures that change the *line* rather than the
                     request - splice an alternative into a flagged stretch, redraw through
                     the request's current waypoints. Both mark the result `source="edited"`
"""
