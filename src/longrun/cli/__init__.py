"""The longrun CLI (scope 10.1). This is the test harness: every golden test runs through it.

Commands, added here as their layers land:

    longrun plan request.yaml
    longrun repair route.gpx
    longrun build-region poly.geojson
    longrun refresh plan.json --date YYYY-MM-DD
    longrun export plan.json --fit --out course.fit
    longrun edit lock|unlock|via|avoid|choose|reroute plan.json

Both of those last two were advertised here from the first commit and built in M10. `--date`
is required on a refresh rather than defaulting to today: a command that reads the wall clock
is one the golden harness cannot freeze, which is scope 3.3's no-implicit-clock rule applied
to a CLI.

`longrun edit` is scope 10.3's five direct-manipulation gestures, and it arrived in M11
before any map control, which is scope 3.9's rule - a capability exists as a tool and a
command first. It is also the only way a headless run can exercise them: nothing can drag on
a MapLibre canvas from a test.
"""
