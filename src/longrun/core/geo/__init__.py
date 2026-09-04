"""Geometry, track I/O, and raster access.

Planned modules:
    gpx.py           gpx_read / gpx_write (GPX 1.1 with waypoints), track normalization
    segments.py      split a route into scoring segments; distance-along; locked ranges
    dem.py           3DEP windowed reads, elevation profile, grade histogram
    dsm.py           DSM = DEM + canopy height + building heights, per corridor
    svf.py           sky view factor tiles, derived on demand and cached as COGs
    raycast.py       direct-sun ray-cast (numba); resolution degrades under time budget
"""
