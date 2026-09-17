"""One GraphHopper per region, addressed by port — and the configs that build them.

Until M9 there was one router URL. `core/routing/graphhopper.py` reads
`LONGRUN_GRAPHHOPPER_URL` or falls back to `http://localhost:8989`, every config in
`deploy/graphhopper/` bound that same port, and two of five built regions had a graph at all.
`tests/contract/test_graphhopper.py` already had to skip when "the server on 8989 is as likely
to be Phoenix as the Bay Area", which is the problem stated from the other end.

**A route drawn on the wrong region's graph is the worst output this project can produce**,
because it is plausible. It has real streets, a real distance and a real elevation profile, and
nothing downstream can tell that they belong to a different city. So region resolution refuses
to guess: waypoints that straddle two regions, or fall in none, name the candidates and fall
back rather than picking.

**Configs are generated, not copied.** `config-phoenix-lts.yml` was a copy of the Bay Area's
with two lines changed, and it still carried the Bay Area's extract command and bbox in its
header - stale the moment it was written, and invisible because nobody reads a config they did
not change. Generation plus `test_the_committed_configs_match_their_specs` removes the class.
They stay *committed* rather than being written at run time because `GraphHopper.load()`
compares the config's `profiles` against the string stored in the graph and refuses a
mismatch: a config that regenerated differently would take every built graph down with it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.geometry import LatLon

#: The committed registry. Relative to the repository root, like every other deploy file.
REGISTRY_PATH = Path("deploy/regions/routers.yaml")

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8989
DEFAULT_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"

#: Per-region override, checked before the registry. `LONGRUN_GRAPHHOPPER_URL_BAYAREA`.
ENV_PREFIX = "LONGRUN_GRAPHHOPPER_URL_"


@dataclass(frozen=True)
class RouterEntry:
    """Where one region's graph is served from, and what it costs to serve."""

    region: str
    port: int = DEFAULT_PORT
    xmx: str = "4g"
    host: str = DEFAULT_HOST

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def admin_port(self) -> int:
        """GraphHopper's admin connector. Always `port + 1`, which is why ports sit two apart."""
        return self.port + 1

    @property
    def config_path(self) -> Path:
        return Path(f"deploy/graphhopper/config-{self.region}-lts.yml")

    @property
    def graph_location(self) -> str:
        return f"data/graphhopper/{self.region}-lts-gh"

    @property
    def pbf(self) -> str:
        return f"data/osm/{self.region}-lts.osm.pbf"


def load_registry(path: Path | None = None) -> dict[str, RouterEntry]:
    """Read `deploy/regions/routers.yaml`.

    Returns `{}` when the file is absent rather than raising: a checkout with no deploy tree
    should still be able to plan against `LONGRUN_GRAPHHOPPER_URL`, which is what every setup
    predating this file does.
    """
    import yaml

    target = path or REGISTRY_PATH
    if not target.exists():
        return {}
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    out: dict[str, RouterEntry] = {}
    for region, entry in raw.items():
        values = entry if isinstance(entry, dict) else {}
        out[str(region)] = RouterEntry(
            region=str(region),
            port=int(values.get("port", DEFAULT_PORT)),
            xmx=str(values.get("xmx", "4g")),
            host=str(values.get("host", DEFAULT_HOST)),
        )
    return out


def router_url(
    region: str | None = None,
    *,
    explicit: str | None = None,
    env: dict[str, str] | None = None,
    registry: dict[str, RouterEntry] | None = None,
) -> str:
    """Where to send a routing request, most specific answer first.

    `--router`, then `LONGRUN_GRAPHHOPPER_URL_<REGION>`, then the registry, then
    `LONGRUN_GRAPHHOPPER_URL`, then the default. The last two rungs are what keep every
    existing setup working: a machine with one region and one env var never reaches the
    registry at all.
    """
    if explicit:
        return explicit.rstrip("/")
    source = os.environ if env is None else env
    if region:
        per_region = source.get(f"{ENV_PREFIX}{region.upper()}", "").strip()
        if per_region:
            return per_region.rstrip("/")
        entries = load_registry() if registry is None else registry
        entry = entries.get(region)
        if entry is not None:
            return entry.url
    return (source.get("LONGRUN_GRAPHHOPPER_URL", "").strip() or DEFAULT_URL).rstrip("/")


@dataclass(frozen=True)
class RegionMatch:
    """Which region a set of waypoints belongs to, and why that answer is what it is."""

    region: str | None
    candidates: list[str]
    reason: str

    @property
    def ambiguous(self) -> bool:
        return self.region is None and len(self.candidates) > 1


def region_for(points: list[LatLon], specs: dict[str, Any]) -> RegionMatch:
    """The one region containing every waypoint, or no answer and the reason.

    `specs` maps a region name to anything with a `shape()` returning its polygon — a
    `RegionSpec`, in practice. Kept as a parameter rather than loaded here so this is testable
    against hand-built rectangles whose answer is known by construction.

    **Every waypoint must be in the same region.** A route whose start is in one graph and
    whose finish is in another cannot be drawn at all, and picking the start's region would
    produce a `NoRouteError` that names the wrong cause.
    """
    from shapely.geometry import Point

    if not points:
        return RegionMatch(None, [], "no waypoints to place")

    containing: list[str] = []
    for name, spec in sorted(specs.items()):
        shape = spec.shape()
        if all(shape.contains(Point(p.lon, p.lat)) for p in points):
            containing.append(name)

    if len(containing) == 1:
        return RegionMatch(containing[0], containing, f"every waypoint is inside {containing[0]}")
    if not containing:
        partial = sorted(
            name
            for name, spec in specs.items()
            if any(spec.shape().contains(Point(p.lon, p.lat)) for p in points)
        )
        if partial:
            return RegionMatch(
                None,
                partial,
                "the waypoints straddle "
                + ", ".join(partial)
                + "; no single graph covers the route",
            )
        return RegionMatch(None, [], "no built region contains these waypoints")
    return RegionMatch(
        None,
        containing,
        "the waypoints are inside more than one region ("
        + ", ".join(containing)
        + "); say which with --region",
    )


# --- config generation ------------------------------------------------------

#: The body every region's config shares, verbatim.
#:
#: Kept byte-identical to what `config-bayarea-lts.yml` held before M9 generated it, and that
#: is load-bearing rather than conservative: `GraphHopper.load()` compares the `profiles`
#: string stored in a built graph against the configured one and refuses a mismatch, so a
#: reflowed profiles block would take every existing graph offline.
_BODY = """  # Keep footways, paths, steps and cycleways: this graph is primarily pedestrian.
  # (The shipped example excludes them, which is only right for motor-vehicle-only graphs.)
  import.osm.ignored_highways: ""

  graph.dataaccess.default_type: RAM_STORE

  profiles:
    # Pedestrian profile. The base model here is deliberately thin: everything that varies
    # per user or per request (surface aversion, hill aversion, LTS multipliers) is sent at
    # query time in `custom_model`, which is only possible because CH is off.
    - name: foot
      weighting: custom
      custom_model:
        distance_influence: 70
        priority:
          # scope 7.1 hard excludes: foot=no, access=private, motorway, and anything
          # GraphHopper's foot access already rejects (motorway, railway ROW, ...).
          - if: "!foot_access"
            multiply_by: "0"
          - if: "foot_road_access == PRIVATE || foot_road_access == NO"
            multiply_by: "0"
          - if: "road_access == PRIVATE"
            multiply_by: "0"
          - if: "road_class == MOTORWAY"
            multiply_by: "0"
          - if: "hike_rating >= 2"
            multiply_by: "0"
          - else: ""
            multiply_by: "foot_priority"
        speed:
          - if: "true"
            limit_to: "foot_average_speed"

    # Car profile exists only for crew_points drive times (scope 7.7). Turn costs are off:
    # drive-time estimates at this resolution do not need them and they cost import time.
    - name: car
      weighting: custom
      custom_model:
        distance_influence: 90
        priority:
          - if: "!car_access"
            multiply_by: "0"
        speed:
          - if: "true"
            limit_to: "car_average_speed"

  # CH off: a contraction hierarchy would freeze the custom model into the preparation.
  profiles_ch: []
  # LM on: hybrid mode keeps the query-time custom model and still beats plain Dijkstra.
  profiles_lm:
    - profile: foot
    - profile: car

  # Everything a custom model or a path detail may reference must be listed here at import.
  #
  #  - foot_* / car_*      : the two profiles
  #  - osm_way_id          : scope 6.2 accepted-road set == map-matched OSM ways (risk R4)
  #  - surface, smoothness,
  #    track_type          : scope 7.2 surface_profile
  #  - max_speed, lanes,
  #    road_class,
  #    road_environment    : scope 7.2 segment_hostility inputs / LTS cross-checks
  #  - crossing            : scope 7.2 crossings
  #  - toll, road_access   : legality (scope 7.6)
  graph.encoded_values: >
    foot_access, foot_priority, foot_average_speed, foot_road_access,
    car_access, car_average_speed,
    road_access, road_class, road_class_link, road_environment,
    surface, smoothness, track_type, max_speed, lanes, crossing, footway, toll,
    hike_rating, mtb_rating, osm_way_id,
    lts

  prepare.min_network_size: 200
  prepare.subnetworks.threads: 4
  routing.snap_preventions_default: tunnel, bridge, ferry
  routing.non_ch.max_waypoint_distance: 1000000

  # Elevation is off (scope 7.1 wants the region DEM here). Turning it on changes nothing
  # about how `lts` behaves; it only adds import time.
  # graph.elevation.provider: srtm
  # graph.elevation.cache_dir: data/srtm/
"""


def render_config(entry: RouterEntry, spec_path: Path | None = None) -> str:
    """The full `config-<region>-lts.yml` for one region.

    Everything that varies between regions is here and nowhere else: the extract, the graph
    location, and the two ports. That is the whole of what `config-phoenix-lts.yml` differed
    from `config-bayarea-lts.yml` by — plus a header describing the Bay Area, which is the
    part a copy gets wrong.
    """
    spec = spec_path or Path(f"deploy/regions/{entry.region}.yaml")
    return f"""# GENERATED from {spec.as_posix()} -- do not edit by hand.
#
#   uv run longrun region-config {spec.as_posix()}
#
# `tests/unit/test_region_routers.py` regenerates every committed config and fails on a diff,
# so an edit here is reverted by CI rather than silently kept. Change the region spec or
# `regions/routers.py`.
#
# GraphHopper config for '{entry.region}', WITH the custom `lts` encoded value. The graph MUST
# be built with the wrapper, which is the only thing that knows what `lts` is:
#   ./deploy/graphhopper/import-lts.ps1 -Region {entry.region}
# Once built, the stock graphhopper-web jar serves it:
#   ./deploy/graphhopper/run.ps1 -Region {entry.region}
#
# The `lts` tag on the input pbf comes from `osm_lts.way_lts` via
# deploy/graphhopper/scripts/add_lts_tags.py, which reads the same `lts_from_tags` every
# scorer uses (ADR 0025). Build the region first or that step refuses.
#
# Built for scope 4.3 / 7.1: flexible + LM mode with CH switched off, so the custom model is a
# query-time parameter and not baked into a contraction hierarchy.
#
# Paths are relative to the repository root -- start the server from there, or use
# deploy/graphhopper/run.ps1 which does the cd for you.

graphhopper:
  datareader.file: {entry.pbf}
  graph.location: {entry.graph_location}

{_BODY}
server:
  application_connectors:
    - type: http
      port: {entry.port}
      bind_host: {entry.host}
      max_request_header_size: 50k
  request_log:
    appenders: []
  admin_connectors:
    - type: http
      port: {entry.admin_port}
      bind_host: {entry.host}

logging:
  appenders:
    - type: console
      time_zone: UTC
      log_format: "%d{{HH:mm:ss.SSS}} %-5level %logger{{20}} - %msg%n"
"""


__all__ = [
    "DEFAULT_URL",
    "ENV_PREFIX",
    "REGISTRY_PATH",
    "RegionMatch",
    "RouterEntry",
    "load_registry",
    "region_for",
    "render_config",
    "router_url",
]
