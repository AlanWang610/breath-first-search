# GraphHopper config

Flexible/LM mode so the custom model is a query-time parameter (scope 4.3, 7.1).

Needs, per region:
- pedestrian and car profiles (car for `crew_points` drive times)
- encoded values including the offline LTS 1-4 score computed in PostGIS at import
- custom areas for avoid-polygons and per-polygon priority
- map matching enabled (history import and verification)
- elevation from the region DEM
