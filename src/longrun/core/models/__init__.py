"""Pydantic types shared by every layer. The plan schema is also the API contract.

Planned modules:
    geometry.py      Route, Segment, Waypoint, corridor buffers
    measurement.py   SegmentMeasurements, Severity, Flag (soft/hard), worst-N lists
    profile.py       PreferenceProfile entries: value, weight, provenance, updated
    request.py       PlanRequest: entry mode, endpoints, date/window, constraints
    plan.py          Plan, PlanSheet sections, coverage manifest, data-snapshot pins
    features.py      Adapter output schema: geometry, start/end, category, confidence, url
"""
