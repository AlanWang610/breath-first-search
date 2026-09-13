"""Route geometry (scope 6.1, 7.1).

Storage is WGS84 lon/lat and metric SI throughout; every length, buffer and area
computation happens in a per-route local UTM projection chosen in `core.geo.projections`,
never in degrees. Models here are pure data with validation: anything requiring geodesy
lives in `core.geo`, so the plan schema stays serializable as the API contract (scope 10.3).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]
Meters = Annotated[float, Field(ge=0)]


class LatLon(BaseModel):
    """A WGS84 position."""

    model_config = ConfigDict(frozen=True)

    lat: Latitude
    lon: Longitude


class RoutePoint(BaseModel):
    """One point on a route, carrying its distance from the start.

    `cum_dist_m` is computed once by `core.geo` and then relied on everywhere: the
    pacing model, position weighting (scope 8.2) and distance markers all key off it.
    """

    model_config = ConfigDict(frozen=True)

    lat: Latitude
    lon: Longitude
    ele_m: float | None = None
    cum_dist_m: Meters = 0.0


class Route(BaseModel):
    """An ordered polyline. The unit of input in repair mode and of output always."""

    id: str
    points: list[RoutePoint]
    name: str | None = None
    source: Literal["imported", "generated", "edited"] = "imported"

    @model_validator(mode="after")
    def _check_points(self) -> Route:
        if len(self.points) < 2:
            raise ValueError("a route needs at least two points")
        prev = self.points[0].cum_dist_m
        for i, p in enumerate(self.points[1:], start=1):
            if p.cum_dist_m < prev:
                raise ValueError(
                    f"cum_dist_m must be non-decreasing; point {i} goes backwards "
                    f"({p.cum_dist_m} < {prev})"
                )
            prev = p.cum_dist_m
        return self

    @property
    def length_m(self) -> float:
        return self.points[-1].cum_dist_m


class Segment(BaseModel):
    """A scoring unit: a run of route points sharing an OSM way.

    Identity is stable across reruns and is what every scorer, every flag and every
    golden `expected.json` keys off. Segments partition the route with no gaps or
    overlaps; `core.geo.segments` splits at way changes and subdivides so that no
    segment exceeds a maximum length, which keeps position weighting (scope 8.2)
    meaningful on long uniform stretches.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    index: int = Field(ge=0)
    start_idx: int = Field(ge=0)
    end_idx: int = Field(ge=0)
    cum_start_m: Meters
    length_m: Meters
    way_id: int | None = None

    @model_validator(mode="after")
    def _check_span(self) -> Segment:
        if self.end_idx <= self.start_idx:
            raise ValueError(f"segment {self.id}: end_idx must exceed start_idx")
        return self

    @property
    def cum_end_m(self) -> float:
        return self.cum_start_m + self.length_m


class BBox(BaseModel):
    """A WGS84 bounding box, used to scope raster and layer reads."""

    model_config = ConfigDict(frozen=True)

    min_lon: Longitude
    min_lat: Latitude
    max_lon: Longitude
    max_lat: Latitude

    @model_validator(mode="after")
    def _check_order(self) -> BBox:
        if self.min_lon > self.max_lon or self.min_lat > self.max_lat:
            raise ValueError("bbox minimums must not exceed maximums")
        return self


class Corridor(BaseModel):
    """A buffered route: the query window every scorer opens with (scope 5).

    Held as route id + buffer + bbox rather than a materialized polygon so the model
    stays serializable; `core.geo` builds the shapely geometry when a store needs it.
    """

    model_config = ConfigDict(frozen=True)

    route_id: str
    buffer_m: Meters
    bbox: BBox
