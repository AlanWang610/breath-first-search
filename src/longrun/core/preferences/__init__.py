"""The preference profile: the only place a sign is attached to a measurement (scope 6.3).

Scorers measure. This package maps measurements to cost with a user-supplied sign and
weight. Safety floors live here too and are not lowerable; a user may raise a floor.

Planned modules:
    schema.py        entries with value, weight, provenance (stated|inferred|default), updated
    store.py         versioned YAML at ~/.longrun/profile.yaml; per-run overrides that do
                     not persist unless confirmed
    floors.py        WBGT hard threshold, legality, hard crossings, LTS 4 - outside the profile
    elicitation.py   in-context questions only: permitted when two same-tier alternatives
                     differ mainly on one axis that is still default; cap 3 per plan
    defaults.yaml    shipped defaults from the scope 6.3 table
"""
