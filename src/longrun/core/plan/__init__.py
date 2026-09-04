"""Plan state, arbitration, and plan sheet contents (scope 8.4, 9).

Planned modules:
    scratchpad.py    current GPX, per-segment measurements, ETAs, locks, active profile;
                     read and written between tool calls; persisted for needs_input resume
    manifest.py      data-snapshot pins (OSM extract date, HPMS vintage, DEM resolution,
                     canopy version, GTFS feed versions, adapter versions), budgets used
    weighting.py     position weight w(d): 1.0 through 40% of distance, linear to 2.0 at
                     100%; ceiling 1.5 for heat; never applied to legality
    arbitrate.py     lexicographic tiers safety -> physiological -> comfort; weighted sum
                     within a tier; same-tier conflicts surfaced, never resolved silently
    coverage.py      which sources were checked and which were not, per jurisdiction
    diff.py          route_diff with per-difference score deltas
"""
