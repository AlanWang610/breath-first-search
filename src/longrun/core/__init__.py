"""Deterministic geospatial library. Zero LLM dependency; all tests live here.

Every scorer is a pure function (gpx, context) -> SegmentMeasurements. Golden-route
regression tests run against this package with no model in the loop.
"""
