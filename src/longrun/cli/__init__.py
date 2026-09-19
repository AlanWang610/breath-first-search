"""The longrun CLI (scope 10.1). This is the test harness: every golden test runs through it.

Commands, added here as their layers land:

    longrun plan request.yaml
    longrun repair route.gpx
    longrun build-region poly.geojson
    longrun refresh plan.json --date YYYY-MM-DD
    longrun export plan.json --fit --out course.fit

Both of those last two were advertised here from the first commit and built in M10. `--date`
is required on a refresh rather than defaulting to today: a command that reads the wall clock
is one the golden harness cannot freeze, which is scope 3.3's no-implicit-clock rule applied
to a CLI.
"""
