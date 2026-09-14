"""Raster tiles: one configured provider, and every consumer reads it (ADR 0023).

Scope 7.9 names two tools that need one - `imagery_tile(lat, lon, zoom)`, an aerial tile for
a vision spot-check capped at ~10 per plan, and `render(gpx, layers)`, a static map - and
scope 10.3's map view wants a basemap. Until this module there was no provider anywhere,
and three consumers each said so in their own words.

**USGS The National Map is the default**, for the reason that made "no basemap" the right
call in M2 and M7 and stops making it right here: both earlier decisions rested on scope
14 - a tile server carries an attribution obligation and usually a key. USGS imagery and
topo are US federal works, **public domain, and keyless**, so neither objection applies,
and there is no credential to leak into a browser bundle.

Two measurements shaped the constants below, and both contradicted what reading would have
concluded (2026-09-14, against the live service):

* **The tile path is `{z}/{y}/{x}`** - ArcGIS's row-before-column order. A San Francisco tile
  at z12 is `1583/655`; the swapped `655/1583` is a 404.
* **Imagery stops at zoom 16.** z17 and above return 404 in San Francisco, although the
  service's own metadata advertises 23 levels of detail. A `maxzoom` read from that
  metadata would have produced a map that 404s on every tile past 16. So `maxzoom` is
  measured, and a request above it is clamped *before* fetching - because a 404 means two
  different things here ("outside the US" and "zoomed past 16"), and caching the second as
  the first would record "no imagery here" for a place that has it.

At z16 a pixel is about 1.9 m on the ground at this latitude: enough to tell a trail from a
road or a path through a park, not enough to see a sidewalk. Scope 12 already says imagery
"is unreliable for fine features; used only as a capped spot-check", and this is why.
"""

from __future__ import annotations

import base64
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from longrun.core.data.cache import STATIC_DAY, fetch

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.data.base import Cache
    from longrun.core.models.context import Budget

#: Which provider. `usgs-imagery` (default), `usgs-topo`, `none`, or `custom`.
PROVIDER_ENV_VAR = "LONGRUN_TILE_PROVIDER"
#: For `custom` only: an https template containing `{z}`, `{x}` and `{y}`.
URL_ENV_VAR = "LONGRUN_TILE_URL"
#: For `custom` only, and required: a provider nobody can attribute is one nobody may use.
ATTRIBUTION_ENV_VAR = "LONGRUN_TILE_ATTRIBUTION"
#: For `custom` only: the highest zoom the provider actually serves.
MAXZOOM_ENV_VAR = "LONGRUN_TILE_MAXZOOM"

TILE_TOOL = "tiles.fetch"
HTTP_TIMEOUT_S = 30.0

#: Web Mercator's latitude limit. Beyond it a tile row is undefined, not merely empty.
MAX_LATITUDE = 85.05112878

_USGS = "https://basemap.nationalmap.gov/arcgis/rest/services"

TileKind = Literal["imagery", "map"]


class TileConfigError(ValueError):
    """The tile provider settings are unusable, named so the message can say which."""


@dataclass(frozen=True)
class TileProvider:
    """One raster source, described well enough for MapLibre and for a fetch."""

    id: str
    template: str
    attribution: str
    licence: str
    #: The key in `attribution.LICENCES`.
    source: str
    kind: TileKind
    maxzoom: int
    tile_size: int = 256

    def url(self, z: int, x: int, y: int) -> str:
        # `replace` rather than `format`: a custom template may carry braces or a query
        # string that `str.format` would choke on or mangle.
        return self.template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))

    def as_maplibre(self) -> dict[str, Any]:
        """The raster source MapLibre wants, and the shape `GET /api/basemap` returns."""
        return {
            "provider": self.id,
            "kind": self.kind,
            "tiles": [self.template],
            "tile_size": self.tile_size,
            "maxzoom": self.maxzoom,
            "attribution": self.attribution,
            "licence": self.licence,
        }


#: Aerial imagery. What `imagery_tile` needs: a vision spot-check looks at the ground.
USGS_IMAGERY = TileProvider(
    id="usgs-imagery",
    template=f"{_USGS}/USGSImageryOnly/MapServer/tile/{{z}}/{{y}}/{{x}}",
    attribution="USDA, USGS The National Map: Orthoimagery",
    licence="US public domain",
    source="usgs_national_map",
    kind="imagery",
    maxzoom=16,
)

#: A topographic map. Easier to read a route over than imagery; useless for a spot-check.
USGS_TOPO = TileProvider(
    id="usgs-topo",
    template=f"{_USGS}/USGSTopo/MapServer/tile/{{z}}/{{y}}/{{x}}",
    attribution="USGS The National Map",
    licence="US public domain",
    source="usgs_national_map",
    kind="map",
    maxzoom=16,
)

PROVIDERS: dict[str, TileProvider] = {p.id: p for p in (USGS_IMAGERY, USGS_TOPO)}
DEFAULT_PROVIDER = USGS_IMAGERY.id


def provider_from_env(env: Mapping[str, str] | None = None) -> TileProvider | None:
    """The configured provider, or `None` when tiles are switched off.

    Read at call time, never at import - `conftest._clear_longrun_env`'s rule. Raises
    `TileConfigError` naming the variable at fault, because a misconfigured provider is a
    person's typo and the useful answer is which one.
    """
    source = os.environ if env is None else env
    name = source.get(PROVIDER_ENV_VAR, "").strip().lower() or DEFAULT_PROVIDER
    if name == "none":
        return None
    if name == "custom":
        return _custom(source)
    if name not in PROVIDERS:
        known = ", ".join([*PROVIDERS, "custom", "none"])
        raise TileConfigError(f"{PROVIDER_ENV_VAR}={name!r} is not one of: {known}")
    return PROVIDERS[name]


def _custom(source: Mapping[str, str]) -> TileProvider:
    template = source.get(URL_ENV_VAR, "").strip()
    attribution = source.get(ATTRIBUTION_ENV_VAR, "").strip()
    if not template:
        raise TileConfigError(f"{PROVIDER_ENV_VAR}=custom needs {URL_ENV_VAR}")
    if not attribution:
        raise TileConfigError(
            f"{PROVIDER_ENV_VAR}=custom needs {ATTRIBUTION_ENV_VAR}: scope 14 makes attribution "
            "an obligation, and a provider nobody can attribute is one nobody may use"
        )
    if urlsplit(template).scheme != "https":
        raise TileConfigError(f"{URL_ENV_VAR} must be an https URL")
    missing = [part for part in ("{z}", "{x}", "{y}") if part not in template]
    if missing:
        raise TileConfigError(f"{URL_ENV_VAR} is missing {', '.join(missing)}")
    raw_zoom = source.get(MAXZOOM_ENV_VAR, "").strip() or "19"
    try:
        maxzoom = int(raw_zoom)
    except ValueError:
        raise TileConfigError(f"{MAXZOOM_ENV_VAR}={raw_zoom!r} is not a whole number") from None
    return TileProvider(
        id="custom",
        template=template,
        attribution=attribution,
        licence="as stated by the provider",
        source="custom_tiles",
        kind="map",
        maxzoom=maxzoom,
    )


def tile_for(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """The Web Mercator tile `(x, y)` containing a point.

    Checked against the live service rather than only against the formula: the de Young at
    z16 is `x=10473, y=25331`, and that tile is real imagery.
    """
    n = 2**zoom
    clamped = max(-MAX_LATITUDE, min(MAX_LATITUDE, lat))
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(clamped))) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_args(provider: TileProvider, z: int, x: int, y: int) -> dict[str, Any]:
    """The cache key for one tile.

    The template is in it, so two providers never share an answer - but **without its query
    string**, because that is where a custom provider's key usually lives, and a key in a
    cache key is a secret in a fixture. A key embedded in the *path* would still land here;
    a custom provider that does that should not be recorded into a committed cassette.
    """
    parts = urlsplit(provider.template)
    return {
        "provider": provider.id,
        "template": f"{parts.scheme}://{parts.netloc}{parts.path}",
        "z": z,
        "x": x,
        "y": y,
    }


@dataclass(frozen=True)
class Tile:
    """One tile, or the honest statement that there is none at that place."""

    provider: str
    z: int
    x: int
    y: int
    url: str
    attribution: str
    requested_zoom: int
    media_type: str | None = None
    data: bytes | None = None
    reason: str | None = None

    @property
    def found(self) -> bool:
        return self.data is not None


def fetch_tile(
    provider: TileProvider,
    lat: float,
    lon: float,
    zoom: int,
    *,
    cache: Cache,
    budget: Budget,
) -> Tile:
    """Fetch the tile at a point, through the cache, charged to the imagery budget.

    The budget is spent **inside** the producer, so a replay from a cassette costs nothing -
    ADR 0017's rule for the router, applied to the one other metered external source.

    A 404 inside the provider's zoom range is cached as "no tile here", because for USGS it
    is deterministic: the point is outside the US. Any other failure - a timeout, a 5xx, an
    HTML error page served as 200 - raises and is **not** cached, so an outage during a
    recording session cannot pin a false absence into a cassette.
    """
    z = max(0, min(zoom, provider.maxzoom))
    x, y = tile_for(lat, lon, z)
    url = provider.url(z, x, y)

    def produce() -> dict[str, Any]:
        budget.spend_imagery_tile()
        import httpx

        response = httpx.get(url, timeout=HTTP_TIMEOUT_S, follow_redirects=True)
        if response.status_code == 404:
            return {"status": 404}
        response.raise_for_status()
        media_type = response.headers.get("content-type", "").split(";")[0].strip()
        if not media_type.startswith("image/"):
            raise ValueError(f"expected an image from {provider.id}, got {media_type or 'nothing'}")
        return {
            "status": 200,
            "media_type": media_type,
            "data": base64.b64encode(response.content).decode("ascii"),
        }

    payload = fetch(cache, TILE_TOOL, tile_args(provider, z, x, y), STATIC_DAY, produce)
    where = Tile(
        provider=provider.id,
        z=z,
        x=x,
        y=y,
        url=url,
        attribution=provider.attribution,
        requested_zoom=zoom,
    )
    if payload.get("status") != 200:
        return replace(where, reason=f"{provider.id} has no tile at this location")
    return replace(
        where,
        media_type=str(payload.get("media_type")),
        data=base64.b64decode(str(payload.get("data", ""))),
    )


__all__ = [
    "ATTRIBUTION_ENV_VAR",
    "DEFAULT_PROVIDER",
    "MAXZOOM_ENV_VAR",
    "PROVIDERS",
    "PROVIDER_ENV_VAR",
    "TILE_TOOL",
    "URL_ENV_VAR",
    "USGS_IMAGERY",
    "USGS_TOPO",
    "Tile",
    "TileConfigError",
    "TileProvider",
    "fetch_tile",
    "provider_from_env",
    "tile_args",
    "tile_for",
]
