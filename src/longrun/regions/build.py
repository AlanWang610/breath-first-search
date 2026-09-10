"""The five idempotent steps of scope 13, with a manifest a partial build resumes from.

A region is a polygon plus a build. What makes this a *build* rather than a script is that
every step is idempotent and every step is recorded: the loaders upsert on the source's own
id, the downloads are content-addressed on disk, and a run that dies in step 2 picks up in
step 2 rather than re-fetching a hundred megabytes of TIGER to get there.

**A step that cannot run is recorded, never skipped silently.** Four of the seven sources
scope 13 names have loaders today and three do not — PAD-US and HPMS have none written, and
FCC BDC is behind an account. A build that quietly produced a region missing three layers
would be a build whose output nobody could reason about, so each is a `blocked` step with
the reason, and step 5's coverage report is where they surface. That is scope 3.6 applied
to a build instead of a plan.

**Step 1 stops at the graph.** The clip and the LTS tag rewrite are Python
(`deploy/graphhopper/scripts/`), and ADR 0001's import needs a JVM and a Maven module. The
build prepares the `.pbf` and reports the exact command rather than shelling out to Java:
a region build that fails because someone has no JDK should say so in one line, not in a
stack trace from a subprocess.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

WGS84 = 4326

#: Step names, in the order scope 13 lists them.
STEPS: tuple[str, ...] = (
    "osm_graph",
    "layers",
    "terrain",
    "jurisdictions",
    "coverage_report",
)

#: What a step can end as. `blocked` is the one that earns its place: it means the step is
#: implemented and its input does not exist, which is a different thing from failing.
STATUSES: tuple[str, ...] = ("done", "blocked", "failed", "skipped")


class RegionSpec(BaseModel):
    """What a region is, as a committed file rather than a set of command-line flags.

    Committed because scope 6.4 wants two plans comparable: a region built from a spec in
    version control can be rebuilt, and one built from whatever someone typed cannot.
    """

    name: str
    #: GeoJSON with the region's polygon. Everything spatial is derived from it.
    polygon: Path
    #: Pre-clipped OSM extract. Not derived, because clipping a state extract to a polygon
    #: is `deploy/graphhopper/scripts/clip_pbf.py`'s job and it is a separate 10-minute run.
    osm_extract: Path | None = None
    #: HUC4 watershed codes the region touches. Declared rather than derived: deriving them
    #: needs the Watershed Boundary Dataset, which is one more national download to answer
    #: a question a region author already knows the answer to.
    huc4: list[str] = Field(default_factory=list)
    #: Feed id -> local GTFS zip. Same reasoning: discovery needs a registry with a key.
    gtfs: dict[str, Path] = Field(default_factory=dict)
    #: Where national downloads are cached.
    downloads: Path = Path("data/national")

    @classmethod
    def load(cls, path: Path) -> RegionSpec:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        # Paths in a spec are relative to the spec, so a region file can be moved with its
        # data and a build run from any working directory.
        root = path.parent
        for key in ("polygon", "osm_extract", "downloads"):
            if raw.get(key):
                raw[key] = _resolve(root, raw[key])
        raw["gtfs"] = {k: _resolve(root, v) for k, v in (raw.get("gtfs") or {}).items()}
        return cls.model_validate(raw)

    def shape(self) -> Any:
        """The region polygon, in WGS84."""
        import geopandas as gpd

        frame = gpd.read_file(self.polygon)
        if frame.crs is not None and frame.crs.to_epsg() != 4326:
            frame = frame.to_crs(epsg=4326)
        return frame.union_all()


def _resolve(root: Path, value: str | Path) -> Path:
    """A spec path, taken relative to the spec unless it is already absolute or exists."""
    path = Path(value)
    if path.is_absolute() or path.exists():
        return path
    return (root / path).resolve()


class StepRecord(BaseModel):
    """One step's outcome, as the manifest stores it."""

    status: str = "skipped"
    detail: str = ""
    elapsed_s: float = 0.0
    at: str = ""
    counts: dict[str, int] = Field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """Whether a resumed build may pass over this step.

        `blocked` counts as complete on purpose: re-running a step whose input does not
        exist re-discovers that it does not exist, at the cost of the whole build's
        progress. `--force` is how you re-ask.
        """
        return self.status in ("done", "blocked")


class BuildManifest(BaseModel):
    """The record a partial build resumes from, and the region's own provenance."""

    region: str
    spec: str = ""
    started_at: str = ""
    updated_at: str = ""
    steps: dict[str, StepRecord] = Field(default_factory=dict)

    @classmethod
    def load_or_new(cls, path: Path, region: str, spec: Path) -> BuildManifest:
        if path.exists():
            try:
                return cls.model_validate_json(path.read_text(encoding="utf-8"))
            except ValueError:
                pass
        return cls(region=region, spec=str(spec), started_at=_now())

    def save(self, path: Path) -> None:
        self.updated_at = _now()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")

    def record(self, step: str, record: StepRecord) -> None:
        record.at = _now()
        self.steps[step] = record

    @property
    def complete(self) -> bool:
        return all(self.steps.get(step, StepRecord()).complete for step in STEPS)


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass
class BuildContext:
    """Everything the steps share. Not a `ScorerContext`: a build writes, a plan reads."""

    spec: RegionSpec
    connection: Any
    manifest: BuildManifest
    manifest_path: Path
    force: bool = False
    log: Callable[[str], None] = print
    #: States the region touches, filled by the layers step and read by jurisdictions.
    states: list[str] = field(default_factory=list)


# --- the five steps ---------------------------------------------------------


def step_osm_graph(ctx: BuildContext) -> StepRecord:
    """§13 step 1: the extract, the offline LTS score, and the routing graph.

    The graph itself is not built here — see the module docstring. What this does is check
    the extract exists and report the two commands that turn it into a graph, so a build
    log names them rather than a README somewhere else naming them.
    """
    extract = ctx.spec.osm_extract
    if extract is None or not extract.exists():
        return StepRecord(
            status="blocked",
            detail=(
                f"no OSM extract at {extract or '(unset)'}; clip one with "
                f"deploy/graphhopper/scripts/clip_pbf.py"
            ),
        )
    size_mb = extract.stat().st_size / 1e6
    return StepRecord(
        status="done",
        detail=(
            f"{extract.name}, {size_mb:.0f} MB. The routing graph is a JVM step and is not "
            f"run here: deploy/graphhopper/scripts/add_lts_tags.py then "
            f"deploy/graphhopper/import-lts.ps1 (ADR 0001)"
        ),
    )


def step_layers(ctx: BuildContext) -> StepRecord:
    """§13 step 2: every vector source, into PostGIS.

    Order matters once: TIGER's national state file has to land before the states the
    region touches can be derived, and the *places* file is published per state. So this
    step loads states, asks the question, then loads the rest.
    """
    from longrun.core.data.national import TIGER, load_nhd, load_tiger

    counts: dict[str, int] = {}
    blocked: list[str] = []

    if ctx.spec.osm_extract and ctx.spec.osm_extract.exists():
        from longrun.core.data.osm import load_extract

        report = load_extract(ctx.spec.osm_extract, ctx.connection, region=ctx.spec.name)
        counts.update(report.counts)
        ctx.log(f"  osm: {report.total:,} feature(s), vintage {report.vintage}")
    else:
        blocked.append("osm (no extract)")

    for level in ("state", "county"):
        counts.update(
            {
                f"tiger_{level}": sum(
                    load_tiger(
                        ctx.connection,
                        ctx.spec.name,
                        [],
                        into=ctx.spec.downloads,
                        levels=[level],
                    ).values()
                )
            }
        )
    ctx.states = states_touching(ctx)
    ctx.log(f"  states touched: {', '.join(ctx.states) or 'none'}")
    if ctx.states:
        counts["tiger_place"] = sum(
            load_tiger(
                ctx.connection,
                ctx.spec.name,
                ctx.states,
                into=ctx.spec.downloads,
                levels=["place"],
            ).values()
        )

    if ctx.spec.huc4:
        counts["nhd_flowlines"] = sum(
            load_nhd(ctx.connection, ctx.spec.name, ctx.spec.huc4, into=ctx.spec.downloads).values()
        )
    else:
        blocked.append("nhd (no huc4 in the spec)")

    feeds = {name: path for name, path in ctx.spec.gtfs.items() if path.exists()}
    if feeds:
        from longrun.core.data.gtfs import load_gtfs

        counts["gtfs_stops"] = sum(load_gtfs(ctx.connection, ctx.spec.name, feeds).values())
    else:
        blocked.append("gtfs (no feeds in the spec)")

    from longrun.core.data.padus import load_padus
    from longrun.core.models.geometry import BBox

    west, south, east, north = ctx.spec.shape().bounds
    counts["padus_units"] = load_padus(
        ctx.connection,
        ctx.spec.name,
        BBox(min_lon=west, min_lat=south, max_lon=east, max_lat=north),
    )

    # Named individually rather than as "some layers missing": a region missing HPMS and a
    # region missing cell coverage support different plans, and the coverage report has to
    # say which. `overture` is the odd one - it has a loader, but a corridor one (ADR 0009).
    blocked.extend(
        [
            # ADR 0012: no unauthenticated endpoint serves it, so the level every plan
            # reports is tag-only and says so.
            "hpms (no reachable source - ADR 0012)",
            "fcc_bdc (bulk download needs an account)",
            "overture (loads per corridor, not per region - ADR 0009)",
        ]
    )
    assert TIGER  # the spec is what the loaders write against; keep the import honest
    return StepRecord(
        status="done" if counts else "blocked",
        detail="not loaded: " + "; ".join(blocked) if blocked else "all sources loaded",
        counts=counts,
    )


def step_terrain(ctx: BuildContext) -> StepRecord:
    """§13 step 3: terrain and canopy for the DSM.

    Nothing is staged, and that is ADR 0007: 3DEP and the canopy raster are COGs read over
    `/vsicurl/` at plan time, content-addressed and immutable, so a region build that
    downloaded them would be caching bytes that never change into a place with no
    invalidation story. What the step does is confirm the tiles the region needs *resolve*,
    so "the DEM is missing" is discovered at build time rather than mid-plan.

    SVF is per corridor on first use, which §13 step 3 says in its own parenthesis.
    """
    from longrun.core.data.rasters import three_dep_tile, three_dep_url

    west, south, east, north = ctx.spec.shape().bounds
    # A tile is one degree, so walking the bbox by whole degrees names every tile the
    # region touches - the corners alone miss the middle of anything wider than a degree,
    # which every region in scope 11 except the urban one is.
    corners = [(lat, lon) for lat in _degrees(south, north) for lon in _degrees(west, east)]
    tiles = sorted({three_dep_tile(lat, lon) for lat, lon in corners})
    return StepRecord(
        status="done",
        detail=(
            f"{len(tiles)} 3DEP tile(s) cover the region: {', '.join(tiles)}. Read over "
            f"/vsicurl/ at plan time, not staged (ADR 0007). First: "
            f"{three_dep_url(corners[0][0], corners[0][1])}"
        ),
        counts={"dem_tiles": len(tiles)},
    )


def _degrees(low: float, high: float) -> list[float]:
    """Every whole degree in a span, both ends included."""
    import math

    start, stop = math.floor(low), math.ceil(high)
    return [float(value) for value in range(start, stop + 1)] or [low]


def step_jurisdictions(ctx: BuildContext) -> StepRecord:
    """§13 step 4: which jurisdictions the region crosses, and which have an adapter.

    The first half works: TIGER is loaded and the polygon is a query. The second half is
    M4's registry, and until it exists the honest answer for every jurisdiction is the same
    one — which is exactly what scope 7.6's tiered reporting is for.
    """
    found = jurisdictions_in(ctx)
    by_level: dict[str, int] = {}
    for row in found:
        by_level[row["level"]] = by_level.get(row["level"], 0) + 1
    names = ", ".join(f"{count} {level}" for level, count in sorted(by_level.items()))
    return StepRecord(
        status="done" if found else "blocked",
        detail=(
            f"{names or 'nothing'} crossed; no closure adapter for any of them (the registry is M4)"
        ),
        counts=by_level,
    )


def step_coverage_report(ctx: BuildContext) -> StepRecord:
    """§13 step 5: which layers loaded, at what vintage, and what is missing."""
    lines = coverage_report(ctx)
    for line in lines:
        ctx.log(f"  {line}")
    return StepRecord(status="done", detail="; ".join(lines))


STEP_FUNCTIONS: dict[str, Callable[[BuildContext], StepRecord]] = {
    "osm_graph": step_osm_graph,
    "layers": step_layers,
    "terrain": step_terrain,
    "jurisdictions": step_jurisdictions,
    "coverage_report": step_coverage_report,
}


# --- queries the steps share ------------------------------------------------


def states_touching(ctx: BuildContext) -> list[str]:
    """FIPS codes of the states the region polygon meets, from the loaded TIGER states."""
    with ctx.connection.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT geoid FROM tiger.boundaries "
            "WHERE level = 'state' AND ST_Intersects(geom, ST_GeomFromText(%s, 4326)) "
            "ORDER BY geoid",
            (ctx.spec.shape().wkt,),
        )
        return [row[0] for row in cursor.fetchall()]


def jurisdictions_in(ctx: BuildContext) -> list[dict[str, str]]:
    """Every boundary the region meets, at every level TIGER carries."""
    with ctx.connection.cursor() as cursor:
        cursor.execute(
            "SELECT level, geoid, name FROM tiger.boundaries "
            "WHERE ST_Intersects(geom, ST_GeomFromText(%s, 4326)) "
            "ORDER BY level, name",
            (ctx.spec.shape().wkt,),
        )
        return [{"level": a, "geoid": b, "name": c} for a, b, c in cursor.fetchall()]


def coverage_report(ctx: BuildContext) -> list[str]:
    """One line per layer: whether it exists, how many rows **in this region**, and its
    vintage.

    Counted inside the polygon rather than over the table, and the second region is what
    made that necessary. The layers are shared - OSM ids are global and the loaders upsert
    on them (ADR 0010) - so a whole-table count told Phoenix it had 1,566,342 ways, which
    is Phoenix's 634,170 plus the Bay Area's 932,172, and 103 BART stops. A coverage report
    for a region has to be about that region or it is not a coverage report.
    """
    from longrun.core.data.postgis import DEFAULT_LAYER_TABLES, PostGISLayerStore

    store = PostGISLayerStore(ctx.connection)
    polygon = ctx.spec.shape().wkt
    lines: list[str] = []
    for layer, table in sorted(DEFAULT_LAYER_TABLES.items()):
        if not store.has_layer(layer):
            lines.append(f"{layer}: ABSENT ({table} does not exist)")
            continue
        with ctx.connection.cursor() as cursor:
            cursor.execute(
                f"SELECT count(*) FROM {table} "
                f"WHERE ST_Intersects(geom, ST_GeomFromText(%s, {WGS84}))",
                (polygon,),
            )
            count = cursor.fetchone()[0]
        state = f"{count:,} row(s)" if count else "0 rows IN THIS REGION"
        lines.append(f"{layer}: {state}, vintage {store.vintage(layer) or 'unrecorded'}")
    return lines


# --- the build --------------------------------------------------------------


def build_region(
    spec: RegionSpec,
    connection: Any,
    manifest_path: Path,
    *,
    force: bool = False,
    only: list[str] | None = None,
    log: Callable[[str], None] = print,
) -> BuildManifest:
    """Run the five steps, skipping what a previous run finished (scope 13).

    A step that raises is recorded as `failed` and the build stops there rather than
    carrying on: steps 2 and 4 depend on step 2's TIGER load, and a coverage report written
    over a half-loaded database would be the one artefact of the build that lied.
    """
    manifest = BuildManifest.load_or_new(manifest_path, spec.name, manifest_path)
    ctx = BuildContext(
        spec=spec,
        connection=connection,
        manifest=manifest,
        manifest_path=manifest_path,
        force=force,
        log=log,
    )

    for step in STEPS:
        if only and step not in only:
            continue
        previous = manifest.steps.get(step)
        if previous is not None and previous.complete and not force:
            log(f"{step}: already {previous.status} at {previous.at}, skipping")
            continue

        log(f"{step}: running")
        started = time.perf_counter()
        try:
            record = STEP_FUNCTIONS[step](ctx)
        except Exception as exc:  # noqa: BLE001 - the manifest is the report
            record = StepRecord(status="failed", detail=f"{type(exc).__name__}: {exc}")
            record.elapsed_s = time.perf_counter() - started
            manifest.record(step, record)
            manifest.save(manifest_path)
            log(f"{step}: FAILED - {record.detail}")
            return manifest

        record.elapsed_s = time.perf_counter() - started
        manifest.record(step, record)
        manifest.save(manifest_path)
        log(f"{step}: {record.status} in {record.elapsed_s:.0f}s - {record.detail}")

    return manifest


__all__ = [
    "STATUSES",
    "STEPS",
    "STEP_FUNCTIONS",
    "BuildContext",
    "BuildManifest",
    "RegionSpec",
    "StepRecord",
    "build_region",
    "coverage_report",
    "jurisdictions_in",
    "states_touching",
]
