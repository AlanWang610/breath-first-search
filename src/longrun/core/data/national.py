"""The national sources a region build slices (scope 13 step 2, ADR 0009).

OSM has its own module because a `.pbf` is its own format with its own reader. Everything
else in §13 step 2 — TIGER boundaries, NHD hydrography, PAD-US protected areas, HPMS
traffic counts — arrives as **a zipped vector file on a federal server**, and the work of
loading one is the same four things every time: fetch it, read it, rename its columns to
what a scorer expects, and write it into PostGIS with a vintage row.

So that work is written once here and each source is a `NationalSource` plus a normaliser.
Five near-identical modules would drift, and the thing they would drift on is the part
that matters: whether the table a loader writes is the table `DEFAULT_LAYER_TABLES` names.

**Nothing in `core/` imports this.** Ingest, like `osm.py` and `overture.py`: it reaches
the network and writes a database, and `conftest._block_network` fails any non-`network`
test that opens an off-host socket. Scorers read what it wrote through `LayerStore`.

Two decisions are worth stating rather than leaving in the code.

**Granularity is the smallest published unit that covers a region, not the nation.** NHD's
California download is 1,831 MB and its HUC4 for the San Francisco Bay is 121 MB, for the
same flowlines over the area a Bay Area region actually spans. §13 slices per region;
downloading a nation to throw away 99% of it is not that.

**Downloads are content-addressed and cached on disk, not through `cache.fetch`.** This is
the same argument ADR 0007 made for COGs: `tl_2025_us_county.zip` does not change with
the date, so the `(tool, args_hash, date)` key does not describe it, and an 80 MB zip is
not a cassette anyone commits. A build re-run finds the file already on disk and does not
fetch it again, which is what makes step 2 idempotent in the sense §13 asks for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

    from geopandas import GeoDataFrame

WGS84 = 4326

#: Where downloads land. Gitignored, and regenerable by definition.
DEFAULT_DOWNLOAD_DIR = "data/national"

#: How many rows go into one `COPY`. Same reasoning as `osm.BATCH_ROWS`: peak memory is a
#: property of the batch, not of the source.
BATCH_ROWS = 50_000


@dataclass(frozen=True)
class NationalSource:
    """One federal dataset, and where its rows have to end up.

    `layer` is the name a scorer asks a `LayerStore` for, and `schema`.`table` must be what
    `postgis.DEFAULT_LAYER_TABLES` maps that name to — a loader writing anywhere else loads
    into a table nothing reads. `test_national_sources_match_the_store_contract` is what
    holds the two together.
    """

    layer: str
    schema: str
    table: str
    key: str
    geometry: str
    #: Column name -> PostGIS type, geometry and key excluded. Stated rather than inferred
    #: from the downloaded frame: a source that silently changes a column's type would
    #: otherwise change our schema, and the first sign of it would be a scorer reading a
    #: number as a string.
    columns: dict[str, str]
    source: str
    licence: str
    url_template: str
    vintage: str

    def url(self, **parts: str) -> str:
        return self.url_template.format(**parts)

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"


# --- the sources ------------------------------------------------------------

#: Census TIGER/Line. The latest published year, checked 2026-09-10: 2026 is 404 and 2025
#: is current. Pinned rather than computed from today's date, so a plan built in January
#: does not silently change source when the Census publishes.
TIGER_YEAR = "2025"

#: Jurisdiction discovery (§13 step 4) needs three nesting levels, and they are three
#: separate downloads with the same schema. One table with a `level` column rather than
#: three tables: what a corridor asks is "which jurisdictions do I cross", and that is one
#: query against one GIST index, not three unioned.
TIGER = NationalSource(
    layer="boundaries",
    schema="tiger",
    table="boundaries",
    key="geoid",
    geometry="MultiPolygon",
    columns={
        "level": "text",
        "name": "text",
        "statefp": "text",
        "lsad": "text",
    },
    source="tiger",
    licence="US public domain",
    url_template=(
        "https://www2.census.gov/geo/tiger/TIGER{year}/{kind}/tl_{year}_{scope}_{lower}.zip"
    ),
    vintage=f"tiger-{TIGER_YEAR}",
)

#: TIGER level -> (kind, scope). `scope` is "us" for a national file and a state FIPS code
#: for a per-state one; places are only published per state, which is why the region build
#: has to know which states it crosses before it can fetch them.
TIGER_LEVELS: dict[str, tuple[str, str | None]] = {
    "state": ("STATE", "us"),
    "county": ("COUNTY", "us"),
    "place": ("PLACE", None),
}

#: USGS National Hydrography Dataset, staged per HUC4 watershed. `hazards` (scope 7.6)
#: wants "NHD flowlines crossing a way with no bridge tag", so flowlines are the only
#: layer taken: waterbodies and area features do not answer that question.
NHD = NationalSource(
    layer="flowlines",
    schema="nhd",
    table="flowlines",
    key="permanent_identifier",
    geometry="MultiLineString",
    columns={
        "gnis_name": "text",
        "ftype": "integer",
        "fcode": "integer",
        "huc4": "text",
    },
    source="nhd",
    licence="US public domain",
    url_template=(
        "https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/NHD/HU4/GPKG/"
        "NHD_H_{huc4}_HU4_GPKG.zip"
    ),
    vintage="nhd-hu4",
)

#: NHD feature types that are a water crossing worth flagging. Excludes 428 (pipeline),
#: 420 (underground conduit) and 566 (coastline): a runner does not ford a pipeline, and a
#: coastline crosses every waterfront path in the country.
NHD_CROSSABLE_FTYPES: frozenset[int] = frozenset({460, 558, 336, 334})

#: Layer inside the HUC4 GeoPackage. The archive carries thirty-odd tables and this is
#: the only one `hazards` has a question about.
NHD_FLOWLINE_LAYER = "NHDFlowline"

#: Where a person downloads the FCC file. Not a `url_template` anything here calls: it is a
#: page with a form on it, and `download()` would get a 403 from its CDN.
FCC_DOWNLOAD_PAGE = "https://broadbandmap.fcc.gov/data-download"

#: FCC Broadband Data Collection, mobile broadband availability — **the one source in this
#: module that nothing here fetches**, and the reason it is written anyway.
#:
#: Checked 2026-09-22 and again for M13.4: the page above publishes mobile coverage by state
#: and by provider as Shapefile or GeoPackage, needs no account to read, and is US public
#: domain. The account and API token at `bdc.fcc.gov` are for the *API*, which a build-time
#: polygon layer has no use for. What blocks automation is the host, not the licence: every
#: non-browser request, `curl` included, comes back 403 from its CDN. So the file arrives in
#: `data/` by hand, exactly as the OSM extracts do, and a region spec names it.
#:
#: This is a loader for a file nobody here has held, and that is a real limitation stated
#: plainly rather than hidden: `FCC_COLUMN_ALIASES` is read off the published field list, and
#: `normalise_fcc_bdc` **raises** rather than writing a table of NULLs if none of the spellings
#: is present. A wrong guess therefore fails on the first load, by name, with the columns the
#: file actually had — which is the one behaviour that makes writing this in advance honest.
FCC_BDC = NationalSource(
    layer="cell_coverage",
    schema="fcc_bdc",
    table="cell_coverage",
    key="coverage_id",
    geometry="MultiPolygon",
    columns={
        "provider": "text",
        "provider_id": "text",
        "technology": "text",
        "statefp": "text",
    },
    source="fcc_bdc",
    licence="US public domain",
    url_template=FCC_DOWNLOAD_PAGE,
    vintage="fcc-bdc",
)

#: Our column -> the spellings a BDC mobile-availability file might use for it, best first.
#:
#: Aliases rather than one name because the download is published as both Shapefile and
#: GeoPackage and a Shapefile truncates field names to ten characters (`provider_i`), so the
#: same dataset has two spellings depending on which format somebody chose. `statefp` is not
#: here: it comes from the region spec's key, the way `normalise_nhd` takes its `huc4`, because
#: a per-state download does not have to carry the state it is.
FCC_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "provider": ("provider", "brand_name", "brandname", "provider_name", "holding_company"),
    "provider_id": ("provider_id", "provider_i", "providerid", "frn"),
    "technology": ("technology", "tech_code", "technology_code", "tech"),
}

SOURCES: tuple[NationalSource, ...] = (TIGER, NHD, FCC_BDC)


class UnknownSourceColumns(ValueError):
    """A downloaded file does not carry a column a normaliser needs, under any known name.

    Raised rather than absorbed, and it is the whole reason a loader for an un-downloadable
    file is worth writing. `_lowercased` materialises every declared column so a missing one
    reads as NULL instead of raising — which is right for a field that is genuinely optional
    and wrong for the two that identify a carrier. A `cell_coverage` table whose `provider`
    is NULL on every row answers "is this point covered" correctly and reports the carrier
    list as empty, and nothing anywhere says why.
    """


# --- fetching ---------------------------------------------------------------


#: Seconds without a byte arriving before a fetch is treated as stalled.
#:
#: Not a total-download budget — a 121 MB file at 8 MB/s is fifteen seconds and at 200 kB/s
#: is ten minutes, and both are fine. This is the *gap* between chunks, so a connection that
#: has died fails in under a minute instead of holding a build open. Measured against the
#: failure it exists for: one NHD fetch stalled before its response headers and sat there
#: for eighteen minutes, writing nothing, with a 300 s total timeout that never fired.
STALL_TIMEOUT_S = 45.0

#: How many times to resume a partial download before giving up.
DOWNLOAD_ATTEMPTS = 3


def download(url: str, into: Path, *, timeout_s: float = STALL_TIMEOUT_S) -> Path:
    """Fetch a file to `into/<basename>`, or return it if it is already there.

    Content-addressed, so an existing file is the answer rather than a cache to validate:
    the bytes at a TIGER URL for a published year do not change. Delete the file to force a
    re-fetch; that is the deliberate act, and re-running a build is not.

    A stalled attempt is retried with a `Range` request from where it stopped, so the
    retry costs the remainder rather than the whole file. Federal endpoints are not fast
    and not reliable, and a region build fetches several hundred megabytes across them.
    """
    import httpx

    into.mkdir(parents=True, exist_ok=True)
    destination = into / url.rsplit("/", 1)[-1]
    if destination.exists() and destination.stat().st_size > 0:
        return destination

    partial = destination.with_suffix(destination.suffix + ".part")
    timeout = httpx.Timeout(timeout_s, connect=min(timeout_s, 30.0))
    last: Exception | None = None

    for _ in range(DOWNLOAD_ATTEMPTS):
        have = partial.stat().st_size if partial.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with httpx.stream(
                "GET", url, timeout=timeout, follow_redirects=True, headers=headers
            ) as response:
                response.raise_for_status()
                # A server that ignored the Range header is sending the file from the
                # start, so appending would corrupt it.
                mode = "ab" if have and response.status_code == 206 else "wb"
                with partial.open(mode) as handle:
                    for chunk in response.iter_bytes(1 << 20):
                        handle.write(chunk)
            # Renamed only on success, so an interrupted download is never mistaken for a
            # complete one by the existence check above.
            partial.replace(destination)
            return destination
        except (httpx.HTTPError, OSError) as exc:
            last = exc

    raise RuntimeError(f"could not fetch {url} in {DOWNLOAD_ATTEMPTS} attempts: {last}")


#: Zip members GDAL can open as a dataset. A shapefile is named by its `.shp`.
VECTOR_MEMBER_SUFFIXES: tuple[str, ...] = (".gpkg", ".shp", ".gdb", ".geojson")


def vector_member(path: Path) -> str | None:
    """The dataset inside a zip, or None when the archive is a bare shapefile set.

    Named explicitly rather than left to GDAL. `/vsizip/x.zip` resolves on its own only
    when the archive holds exactly one thing it recognises, and every federal download here
    ships its data beside an `.xml` metadata sidecar and a `.jpg` browse image — at which
    point the auto-detection gives up with "not recognized as being in a supported file
    format", which reads like a corrupt download and is not one.
    """
    import zipfile

    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
    for name in names:
        if name.lower().endswith(VECTOR_MEMBER_SUFFIXES):
            return name
    return None


def read_zipped_vector(path: Path, layer: str | None = None, **kwargs: Any) -> GeoDataFrame:
    """Read a vector layer out of a zip without unpacking it.

    GDAL's `/vsizip/` reads members in place, which for NHD means not writing a 318 MB
    GeoPackage to disk to keep one table out of it.
    """
    import geopandas as gpd

    member = vector_member(path)
    # Forward slashes: `/vsizip/` is a GDAL virtual path, not a Windows one, and a
    # backslash in it is a literal character rather than a separator.
    target = f"/vsizip/{path.as_posix()}"
    if member is not None:
        target = f"{target}/{member}"
    return gpd.read_file(target, layer=layer, **kwargs)


# --- writing ----------------------------------------------------------------


def create_statements(spec: NationalSource) -> list[str]:
    """DDL for one source's table. Idempotent, so a resumed build re-runs it freely.

    **`CREATE TABLE IF NOT EXISTS` alone is not enough, and the way it fails is quiet.**
    Adding a column to a `NationalSource` after its table exists does nothing: the create
    is skipped, the `COPY` names the new column, and Postgres refuses — or worse, the
    reader carries on against a table missing the field and every row of it reads as NULL.
    That happened here, between two loads of the same GTFS feed an hour apart: the second
    declared per-day service spans, the table still had the combined pair, and the frozen
    fixture came back with columns that no longer existed in the code.

    So every declared column is also an `ADD COLUMN IF NOT EXISTS`. Additive only — a
    changed *type* still needs a migration, and a dropped column stays until someone drops
    it, both of which are the right amount of ceremony for a destructive change.
    """
    columns = ", ".join(f"{name} {sql_type}" for name, sql_type in spec.columns.items())
    statements = [
        f"CREATE SCHEMA IF NOT EXISTS {spec.schema}",
        f"CREATE TABLE IF NOT EXISTS {spec.qualified} "
        f"({spec.key} text PRIMARY KEY, {columns}, "
        f"geom geometry({spec.geometry}, {WGS84}) NOT NULL)",
    ]
    statements += [
        f"ALTER TABLE {spec.qualified} ADD COLUMN IF NOT EXISTS {name} {sql_type}"
        for name, sql_type in spec.columns.items()
    ]
    statements.append(
        f"CREATE INDEX IF NOT EXISTS {spec.table}_geom_idx ON {spec.qualified} USING GIST (geom)"
    )
    return statements


def _as_multi(geometry: Any, target: str) -> Any:
    """Flatten to 2D and promote to the multi form, so one typed column holds the source.

    Two coercions, both because a `geometry(MultiLineString, 4326)` column is stricter than
    what a federal dataset publishes, and the strictness is worth keeping: it is what
    catches a polygon written into a line layer.

    **Multi-part**, because a source publishes a mix — NHD flowlines are `LineString` and
    `MultiLineString` in the same table.

    **2D**, because NHD publishes measured 3D linestrings: the z is the stream bed's
    elevation and the m is a distance measure along it. Nothing here asks a question about
    either — `hazards` asks whether a route line crosses a watercourse, which is a plan-view
    question — and carrying them would double the column for data no reader has a use for.
    Dropped deliberately rather than by declaring the column `Z`, so the discard is visible
    here instead of being an unnoticed dimension in every geometry downstream.
    """
    from shapely import force_2d
    from shapely.geometry import MultiLineString, MultiPolygon

    if geometry is None or geometry.is_empty:
        return None
    flat = force_2d(geometry)
    if flat.geom_type == target:
        return flat
    if target == "MultiPolygon" and flat.geom_type == "Polygon":
        return MultiPolygon([flat])
    if target == "MultiLineString" and flat.geom_type == "LineString":
        return MultiLineString([flat])
    return None


def load_frame(
    connection: Any,
    frame: GeoDataFrame,
    spec: NationalSource,
    *,
    region: str,
    vintage: str | None = None,
    source_url: str | None = None,
) -> int:
    """Upsert a normalised frame into its table and record the vintage.

    Upsert on the source's own stable id, for the reason ADR 0010 gives for OSM: a region
    build reloads, two regions overlap, and both are making the same claim about the same
    feature. `DISTINCT ON` because a source staged per watershed or per state repeats a
    feature that spans the boundary, and Postgres refuses to update one row twice in a
    statement rather than picking a winner.
    """
    names = [spec.key, *spec.columns, "geom"]
    types = ["text", *spec.columns.values(), "text"]
    staging = f"stage_{spec.table}"
    written = 0

    with connection.cursor() as cursor:
        for statement in create_statements(spec):
            cursor.execute(statement)

        cursor.execute(f"DROP TABLE IF EXISTS pg_temp.{staging}")
        columns = ", ".join(f"{name} {sql}" for name, sql in spec.columns.items())
        cursor.execute(
            f"CREATE TEMP TABLE {staging} ({spec.key} text, {columns}, "
            f"geom geometry({spec.geometry}, {WGS84}))"
        )

        rows = list(_rows(frame, spec))
        for start in range(0, len(rows), BATCH_ROWS):
            batch = rows[start : start + BATCH_ROWS]
            with cursor.copy(f"COPY pg_temp.{staging} ({', '.join(names)}) FROM STDIN") as copy:
                copy.set_types(types)
                for row in batch:
                    copy.write_row(row)
            written += len(batch)

        if written:
            updates = ", ".join(f"{n} = EXCLUDED.{n}" for n in names if n != spec.key)
            joined = ", ".join(names)
            cursor.execute(
                f"INSERT INTO {spec.qualified} ({joined}) "
                f"SELECT DISTINCT ON ({spec.key}) {joined} FROM pg_temp.{staging} "
                f"WHERE geom IS NOT NULL AND {spec.key} IS NOT NULL "
                f"ORDER BY {spec.key} "
                f"ON CONFLICT ({spec.key}) DO UPDATE SET {updates}"
            )
        cursor.execute(f"DROP TABLE IF EXISTS pg_temp.{staging}")

        cursor.execute(
            "INSERT INTO meta.layer_vintage (layer_schema, source, region, vintage, source_url) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (layer_schema, source, region) DO UPDATE SET "
            "vintage = EXCLUDED.vintage, source_url = EXCLUDED.source_url, loaded_at = now()",
            (spec.schema, spec.source, region, vintage or spec.vintage, source_url),
        )
    connection.commit()
    return written


def _rows(frame: GeoDataFrame, spec: NationalSource) -> Any:
    """Frame rows as COPY tuples, geometry promoted and reprojected to WGS84."""
    if len(frame) == 0:
        return
    projected = frame if frame.crs is None else frame.to_crs(epsg=WGS84)
    for _, row in projected.iterrows():
        geometry = _as_multi(row.geometry, spec.geometry)
        if geometry is None:
            continue
        values: list[Any] = [_text(row.get(spec.key))]
        for name, sql_type in spec.columns.items():
            values.append(_coerce(row.get(name), sql_type))
        values.append(geometry.wkb_hex)
        yield tuple(values)


def _text(value: Any) -> str | None:
    if value is None or value != value:  # NaN is not equal to itself
        return None
    return str(value).strip() or None


def _coerce(value: Any, sql_type: str) -> Any:
    if value is None or value != value:
        return None
    if sql_type in ("integer", "bigint"):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if sql_type in ("double precision", "real"):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return _text(value)


# --- per-source normalisers -------------------------------------------------


def _lowercased(frame: GeoDataFrame, spec: NationalSource) -> GeoDataFrame:
    """Source columns lowercased, and every declared column present.

    The second half is not tidiness. `_rows` reads each declared column with `Series.get`,
    which answers **None** for a label that is not there — so a source that stops
    publishing a column does not raise, it writes a table of NULLs, and the first sign is a
    plan sheet naming an unnamed jurisdiction. Materialising the column here means the
    frame either has the data or visibly does not, in one place, before any of it is
    written.
    """
    out = frame.rename(
        columns={c: c.lower() for c in frame.columns if c != frame.geometry.name}
    ).copy()
    for column in (spec.key, *spec.columns):
        if column not in out.columns:
            out[column] = None
    return out


def normalise_tiger(frame: GeoDataFrame, level: str) -> GeoDataFrame:
    """TIGER's columns to ours. `GEOID` is the stable national id at every level."""
    # `NAME` at state and place; at county level `NAMELSAD` is the one carrying "County".
    # Read before the declared columns are filled in, or the fallback finds its own NULL.
    lowered = {c.lower(): c for c in frame.columns}
    out = _lowercased(frame, TIGER)
    if "name" not in lowered and "namelsad" in lowered:
        out["name"] = frame[lowered["namelsad"]]
    out["level"] = level
    return out


def normalise_nhd(frame: GeoDataFrame, huc4: str) -> GeoDataFrame:
    """NHD flowlines to ours, keeping only the types a runner could have to cross."""
    out = _lowercased(frame, NHD)
    out["huc4"] = huc4
    if frame is not None and "ftype" in {c.lower() for c in frame.columns}:
        out = out[out["ftype"].isin(NHD_CROSSABLE_FTYPES)]
    return out


def normalise_fcc_bdc(frame: GeoDataFrame, statefp: str) -> GeoDataFrame:
    """One state's BDC mobile availability to ours, dissolved to one row per carrier service.

    **Dissolved, and that is the decision in this function.** A BDC state file publishes many
    polygons per provider, and the source gives no stable per-polygon id — so a key
    synthesised from a row ordinal changes the moment the file is re-downloaded in a different
    order, and `load_frame`'s upsert would then write a second copy of the state rather than
    replacing the first. Unioning by `(provider_id, technology)` produces a key that is a
    *fact about the filing* instead of an accident of row order, and it loses nothing
    `cell_coverage` asks for: the scorer's two questions are "is this point inside anybody's
    polygon" and "whose", and both survive the union.

    It is not free. The dissolve is the expensive part of loading a large state, and it is
    paid once per region build rather than per plan — the same trade `gtfs.py` makes when it
    precomputes service summaries at build time.

    Raises `UnknownSourceColumns` when a carrier-identifying column is missing under every
    known spelling, rather than letting `_lowercased` turn it into a column of NULLs.
    """
    out = _lowercased(frame, FCC_BDC)
    for column, aliases in FCC_COLUMN_ALIASES.items():
        # `notna().any()` and not merely `in out.columns`: `_lowercased` has already
        # materialised every declared column as all-None, so presence alone would let the
        # empty one it just created win over the real one the file carries.
        found = next((a for a in aliases if a in out.columns and out[a].notna().any()), None)
        if found is None:
            raise UnknownSourceColumns(
                f"no column for {column!r} in this FCC file: tried {', '.join(aliases)}, and "
                f"the file carries {', '.join(sorted(str(c) for c in frame.columns))}. "
                "Add the real spelling to FCC_COLUMN_ALIASES - it was guessed from the "
                "published field list, because the download 403s every non-browser request."
            )
        if found != column:
            out[column] = out[found]
        out[column] = out[column].astype(str)

    out["statefp"] = statefp
    dissolved = out.dissolve(by=["provider_id", "technology"], as_index=False)
    dissolved["coverage_id"] = (
        statefp + ":" + dissolved["provider_id"] + ":" + dissolved["technology"]
    )
    return dissolved


# --- the two loads --------------------------------------------------------


def load_tiger(
    connection: Any,
    region: str,
    states: list[str],
    *,
    into: Path | None = None,
    levels: list[str] | None = None,
) -> dict[str, int]:
    """Load state, county and place boundaries into `tiger.boundaries`.

    `states` are two-digit FIPS codes, and they are required rather than optional because
    places are published per state: there is no national place file, so a build has to know
    which states its polygon touches before it can ask. A region crossing a state line — §11
    names one deliberately — passes both.
    """
    from pathlib import Path as _Path

    directory = into or _Path(DEFAULT_DOWNLOAD_DIR)
    counts: dict[str, int] = {}

    for level in levels or list(TIGER_LEVELS):
        kind, scope = TIGER_LEVELS[level]
        scopes = [scope] if scope is not None else states
        for one in scopes:
            url = TIGER.url(year=TIGER_YEAR, kind=kind, scope=one, lower=level)
            frame = read_zipped_vector(download(url, directory))
            written = load_frame(
                connection,
                normalise_tiger(frame, level),
                TIGER,
                region=region,
                source_url=url,
            )
            counts[f"{level}:{one}"] = written
    return counts


def load_nhd(
    connection: Any, region: str, huc4s: list[str], *, into: Path | None = None
) -> dict[str, int]:
    """Load flowlines for each HUC4 watershed a region touches into `nhd.flowlines`.

    Per watershed rather than per state: California's NHD download is 1,831 MB and the
    San Francisco Bay HUC4 is 121 MB for the same water a Bay Area region can reach.
    """
    from pathlib import Path as _Path

    directory = into or _Path(DEFAULT_DOWNLOAD_DIR)
    counts: dict[str, int] = {}

    for huc4 in huc4s:
        url = NHD.url(huc4=huc4)
        frame = read_zipped_vector(download(url, directory), layer=NHD_FLOWLINE_LAYER)
        counts[huc4] = load_frame(
            connection, normalise_nhd(frame, huc4), NHD, region=region, source_url=url
        )
    return counts


def read_vector(path: Path, layer: str | None = None, **kwargs: Any) -> GeoDataFrame:
    """A vector file already on disk, zipped or not.

    The FCC publishes the same data as a zipped Shapefile and as a GeoPackage and does not
    say which one somebody downloaded, so the loader takes either rather than making the
    choice part of the errand.
    """
    import geopandas as gpd

    if path.suffix.lower() == ".zip":
        return read_zipped_vector(path, layer=layer, **kwargs)
    return gpd.read_file(path, layer=layer, **kwargs)


def load_fcc_bdc(
    connection: Any,
    region: str,
    files: dict[str, Path],
    *,
    vintage: str | None = None,
) -> dict[str, int]:
    """Load hand-downloaded FCC mobile coverage into `fcc_bdc.cell_coverage`.

    `files` maps a two-digit state FIPS code to a file on disk, the way `load_tiger` takes
    states and `load_nhd` takes watersheds — except that nothing here downloads anything.
    The file is an errand:

        1. open https://broadbandmap.fcc.gov/data-download in a browser
        2. Mobile Broadband -> the state -> Shapefile or GeoPackage
        3. save it under `data/` and name it in the region spec's `cell_coverage`

    No credential is needed for any of that. What makes it manual is that the CDN answers
    403 to every non-browser request, `curl` included, so no code here will ever fetch it
    unattended — which is a smaller and more useful claim than "behind an account", because
    it says the blocker is an errand rather than a gate.

    `vintage` is the "Data as of" date the download page prints beside the file. Passed in
    rather than derived: a hand-downloaded file's own name and mtime record when *somebody
    fetched it*, and scope 6.4 wants the date the carriers filed. Left unset it falls back
    to the source's constant, which pins nothing and says nothing it does not know.
    """
    counts: dict[str, int] = {}
    for statefp, path in files.items():
        if not path.exists():
            raise FileNotFoundError(
                f"no FCC coverage file for state {statefp} at {path}. It is a manual "
                f"download from {FCC_DOWNLOAD_PAGE} - see load_fcc_bdc."
            )
        counts[statefp] = load_frame(
            connection,
            normalise_fcc_bdc(read_vector(path), statefp),
            FCC_BDC,
            region=region,
            vintage=vintage or FCC_BDC.vintage,
            source_url=FCC_DOWNLOAD_PAGE,
        )
    return counts


__all__ = [
    "BATCH_ROWS",
    "DEFAULT_DOWNLOAD_DIR",
    "FCC_BDC",
    "FCC_COLUMN_ALIASES",
    "FCC_DOWNLOAD_PAGE",
    "NHD",
    "NHD_CROSSABLE_FTYPES",
    "NHD_FLOWLINE_LAYER",
    "SOURCES",
    "TIGER",
    "TIGER_LEVELS",
    "TIGER_YEAR",
    "NationalSource",
    "UnknownSourceColumns",
    "create_statements",
    "download",
    "load_fcc_bdc",
    "load_frame",
    "load_nhd",
    "load_tiger",
    "normalise_fcc_bdc",
    "normalise_nhd",
    "normalise_tiger",
    "read_vector",
    "read_zipped_vector",
    "vector_member",
]
