"""Pacing model and the ETA vector every time-dependent scorer consumes (scope 6.2, 7.3).

Planned modules:
    model.py         pacing_model(gpx, profile) -> ETA at every point
    curves.py        grade-adjusted pace curve, fatigue drift, surface/descent sensitivity
    history.py       FIT / Strava bulk export / Garmin ingest; raw files discarded after
                     derivation; accepted-road set via map_match
    population.py    Minetti default curve and default drift when no history exists

Caveats belong in the output, not in the model: race efforts flagged, extrapolation
beyond the longest recorded effort labeled as a guess.
"""
