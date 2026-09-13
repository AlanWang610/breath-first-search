"""gpx_verify: the ten checks in scope 7.9, pass/fail each with offending segments.

A failure sends the loop back to rerouting (scope 8.1 step 9), so checks return structured
offenders, never a bare boolean.

Planned modules:
    checks.py        one function per numbered check
    runner.py        run all checks against the cached scorer results for a plan
"""
