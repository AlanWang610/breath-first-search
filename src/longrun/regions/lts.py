"""The offline LTS score per way, in PostGIS (scope 7.1, 13 step 1).

Scope §7.1 says an LTS 1-4 score "is computed offline per way in PostGIS and written as an
encoded value at import". Until M9 only the second half was true: `add_lts_tags.py` carried a
`lts_for_way` its own body labelled `PLACEHOLDER`, and the graph was steered by a crude
tag-only approximation while `core/scorers/hostility.py` measured with
`core/routing/lts.py::lts_from_tags`. The two disagreed on ordinary streets - `primary` was 3
to one and 4 to the other, and `sidewalk=separate` moved the level in *opposite* directions -
so a plan's LTS was never the LTS its route had been drawn to avoid.

**The computation is the function the scorer calls, not a reimplementation of it** (ADR 0025).
Expressing Furth a second time in SQL would buy nothing and would guarantee the drift this
module exists to remove: agreement between the router and the scorer is the whole deliverable,
and the cheapest way to have it is to have one implementation. "In PostGIS" is honoured by
where the table lives, which is what the importer needs, not by what language computes it.

**Its own schema, `osm_lts`, and that is not fussiness.** `PostGISLayerStore.vintage()` selects
`WHERE layer_schema = %s ORDER BY loaded_at DESC` - schema only, no table and no source - so a
vintage row written under `osm` would become the vintage every plan reports for `ways`, `nodes`
*and* `amenities`, simply by being the most recent row in that schema.

This sits in `regions/` rather than `core/` for the reason `build.py` does: a build writes and
a plan reads, and `core/` may not depend on a database being there.
"""

from __future__ import annotations

import collections
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from longrun.core.routing.lts import LTS_VERSION, lts_from_tags

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable, Iterator

WGS84 = 4326

SCHEMA = "osm_lts"
TABLE = "way_lts"

#: The optional AADT conflation, left-joined when it exists. Nothing writes it today - ADR
#: 0012 found no unauthenticated HPMS endpoint - and the table below must be correct and
#: useful with `aadt IS NULL` on every row, which `lts_from_tags(tags, aadt=None)` already is.
AADT_TABLE = "osm.way_aadt"

#: Same reasoning as `osm.BATCH_ROWS`: peak memory is a property of this number rather than of
#: the region. 1.8 M ways are loaded across the five regions built so far.
BATCH_ROWS = 50_000

#: Rows pulled from the server-side cursor at a time. Distinct from `BATCH_ROWS`, which is how
#: many are held before a `COPY` flushes.
FETCH_ROWS = 10_000


def create_statements(schema: str = SCHEMA) -> list[str]:
    """DDL for the table, idempotent so a re-run is a no-op.

    No geometry column. This is an attribute table on `osm.ways`, keyed on the id OSM already
    guarantees, and a second linestring would be a second thing to drift from the first.
    """
    return [
        f"CREATE SCHEMA IF NOT EXISTS {schema}",
        f"CREATE TABLE IF NOT EXISTS {schema}.{TABLE} ("
        "way_id bigint PRIMARY KEY, "
        "lts smallint NOT NULL, "
        "confidence real NOT NULL, "
        # Stored because `LTSResult.reasons` is what makes a level arguable rather than
        # asserted, and because the way to diagnose a disagreement with the scorer is to
        # compare the two reason lists rather than the two numbers.
        "reasons text[] NOT NULL, "
        "aadt real, "
        "aadt_source text, "
        "lts_version text NOT NULL)",
        f"CREATE INDEX IF NOT EXISTS {TABLE}_version_idx ON {schema}.{TABLE} (lts_version)",
    ]


def composite_vintage(extract_vintage: str, version: int = LTS_VERSION) -> str:
    """`<extract>+lts<version>` — the graph identity a route cache keys on.

    The extract date pins the *input* and says nothing about the rules applied to it. A graph
    is built once and served for a week (scope §7), so "the router steered on rules this
    process no longer holds" is a state the system can be in, and this is the only thing that
    can make it visible.
    """
    return f"{extract_vintage}+lts{version}"


@dataclass
class LtsReport:
    """What one computation did, for the build manifest and for a human reading a log."""

    vintage: str = ""
    scored: int = 0
    #: Ways in scope that now carry a row at this version, whether this run wrote them or a
    #: previous one did. Reported separately from `scored` because the computation is
    #: incremental, so a fully-covered region legitimately scores nothing — and "0 scored"
    #: would otherwise read exactly like "this region has no ways", which is the failure mode
    #: this milestone exists to stop being invisible.
    covered: int = 0
    #: Ways in scope with no `highway` tag, which get no row. `osm.ways` is filtered on
    #: `highway|railway|footway|cycleway`, so a railway with no highway tag is in the table -
    #: and `lts_from_tags` would score it 2 through its unknown-class branch, which would put
    #: a mainline railway in the graph at the same stress as a residential street.
    not_highway: int = 0
    with_aadt: int = 0
    levels: collections.Counter[int] = field(default_factory=collections.Counter)
    elapsed_s: float = 0.0

    def summary(self) -> str:
        shares = "  ".join(
            f"lts{level}={self.levels[level]:,} ({100 * self.levels[level] / self.scored:.0f}%)"
            if self.scored
            else f"lts{level}=0"
            for level in (1, 2, 3, 4)
        )
        scored = (
            f"{self.scored:,} newly scored; {shares}" if self.scored else "nothing new to score"
        )
        return (
            f"{self.covered:,} way(s) at {self.vintage} ({scored}), "
            f"{self.not_highway:,} without a highway tag skipped, "
            f"{self.with_aadt:,} with a volume"
        )


def _aadt_available(connection: Any) -> bool:
    """Whether the optional conflation table exists — `build.py`'s `to_regclass` idiom."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s)", (AADT_TABLE,))
        row = cursor.fetchone()
    return bool(row and row[0])


def resolve_aadt(tags: dict[str, Any], conflated: float | None) -> tuple[float | None, str | None]:
    """Which volume to score with, and where it came from.

    Precedence is conflated table, then OSM's own `aadt` tag, then nothing. The second rung
    matters more than it looks: `core/scorers/hostility.py` reads `aadt_of(tags)` already, so
    a table that ignored the tag would disagree with the scorer on **precisely the ways that
    carry a volume** — the ones where the answer is least likely to be a guess.

    The forward hazard, recorded in ADR 0025: the day something writes `osm.way_aadt`, the
    scorer has to read it too, or the two part company again on every conflated way.
    """
    from longrun.core.scorers._common import aadt_of

    if conflated is not None:
        return conflated, "conflated"
    tagged = aadt_of(tags)
    return (tagged, "osm_tag") if tagged is not None else (None, None)


@dataclass(frozen=True)
class WayLts:
    """One row of `osm_lts.way_lts`."""

    way_id: int
    lts: int
    confidence: float
    reasons: list[str]
    aadt: float | None
    aadt_source: str | None


def score_way(way_id: int, tags: dict[str, Any], conflated: float | None = None) -> WayLts | None:
    """The row for one way, or `None` when the way is not a road.

    Pure, and separate from `compute` so the rule below is testable without a database — it
    is the one decision here that fails silently rather than loudly.

    **A way with no `highway` tag gets no row.** `osm.ways` is filtered on
    `highway|railway|footway|cycleway` (`core/data/osm.py::WAY_KEYS`), so a mainline railway is
    in that table, and `lts_from_tags` answers for *any* dict: an absent `highway` falls into
    its `unknown_highway_class` branch and comes back level 2 at reduced confidence. Writing
    that row would tag a railway `lts=2` in the graph — the same stress as a residential
    street — and the importer would have no way to tell it from a real answer. The placeholder
    this replaces returned `None` for exactly this case, which is the behaviour to keep.
    """
    if not tags.get("highway"):
        return None
    aadt, source = resolve_aadt(tags, conflated)
    result = lts_from_tags(tags, aadt=aadt)
    return WayLts(
        way_id=way_id,
        lts=result.level,
        confidence=result.confidence,
        reasons=result.reasons,
        aadt=aadt,
        aadt_source=source,
    )


def _rows(
    connection: Any,
    *,
    polygon_wkt: str | None,
    force: bool,
    version: int,
    with_aadt: bool,
) -> Iterator[tuple[int, dict[str, Any], float | None]]:
    """Ways to score, streamed through a server-side cursor.

    Incremental by default: a way already carrying a row at this `lts_version` is skipped, so
    a second region's build costs only the ways the first did not cover, and a re-run after a
    `LTS_VERSION` bump costs everything. `force` re-scores regardless, which is what a change
    to the tag vocabulary needs.
    """
    select = ["SELECT w.way_id, w.tags"]
    joins = ["FROM osm.ways w"]
    where: list[str] = []
    params: list[Any] = []

    if with_aadt:
        select.append(", a.aadt")
        joins.append(f"LEFT JOIN {AADT_TABLE} a ON a.way_id = w.way_id")
    else:
        select.append(", NULL::real AS aadt")

    if not force:
        joins.append(f"LEFT JOIN {SCHEMA}.{TABLE} l ON l.way_id = w.way_id AND l.lts_version = %s")
        params.append(f"{version}")
        where.append("l.way_id IS NULL")

    if polygon_wkt is not None:
        where.append(f"ST_Intersects(w.geom, ST_GeomFromText(%s, {WGS84}))")
        params.append(polygon_wkt)

    sql = " ".join([*select, *joins])
    if where:
        sql += " WHERE " + " AND ".join(where)

    # Named, so Postgres streams rather than materialising 1.8 M rows of jsonb in the client.
    with connection.cursor(name="way_lts_scan") as cursor:
        cursor.itersize = FETCH_ROWS
        cursor.execute(sql, params)
        yield from cursor


def compute(
    connection: Any,
    *,
    region: str,
    extract_vintage: str,
    region_wkt: str | None = None,
    force: bool = False,
    version: int = LTS_VERSION,
    log: Callable[[str], None] = lambda _: None,
) -> LtsReport:
    """Score every unscored way and write `osm_lts.way_lts`.

    **Scoring is global; only the count is per region.** `region_wkt` narrows `covered`, which
    is what a build log should report, and narrows nothing else. Scoping the *scoring* to the
    region polygon is the obvious economy and it opens a coverage gap that would surface as a
    silently worse graph: `load_extract` loads the whole `.pbf`, which is a **bbox** clip, so
    `osm.ways` holds ways outside the region polygon — and `add_lts_tags.py` rewrites that same
    bbox. Every way between the polygon and the bbox edge would reach the importer with no row,
    get no `lts` tag, and land in the graph as the encoded value's 0, which reads as "unknown"
    to every custom model. Scoring everything costs one pass, once: the computation is
    incremental by `lts_version`, so the second region pays only for ways the first did not
    reach.

    `meta.layer_vintage` is written last, so a row saying a region's LTS is computed is only
    there when it is — `load_extract`'s rule, for the same reason.
    """
    started = time.perf_counter()
    vintage = composite_vintage(extract_vintage, version)
    report = LtsReport(vintage=vintage)

    with connection.cursor() as cursor:
        for statement in create_statements():
            cursor.execute(statement)
    connection.commit()

    with_aadt = _aadt_available(connection)
    log(
        f"  aadt: {AADT_TABLE} present" if with_aadt else f"  aadt: no {AADT_TABLE} (ADR 0012)",
    )

    staging = "stage_way_lts"
    with connection.cursor() as writer:
        writer.execute(f"DROP TABLE IF EXISTS pg_temp.{staging}")
        writer.execute(
            f"CREATE TEMP TABLE {staging} (way_id bigint, lts smallint, confidence real, "
            "reasons text[], aadt real, aadt_source text, lts_version text)"
        )

        buffer: list[tuple[Any, ...]] = []

        def flush() -> None:
            if not buffer:
                return
            statement = (
                f"COPY pg_temp.{staging} "
                "(way_id, lts, confidence, reasons, aadt, aadt_source, lts_version) FROM STDIN"
            )
            with writer.copy(statement) as copy:
                copy.set_types(["bigint", "smallint", "real", "text[]", "real", "text", "text"])
                for row in buffer:
                    copy.write_row(row)
            buffer.clear()

        for way_id, tags, conflated in _rows(
            connection,
            polygon_wkt=None,
            force=force,
            version=version,
            with_aadt=with_aadt,
        ):
            row = score_way(way_id, tags, conflated)
            if row is None:
                report.not_highway += 1
                continue
            report.scored += 1
            report.levels[row.lts] += 1
            if row.aadt is not None:
                report.with_aadt += 1
            buffer.append(
                (
                    row.way_id,
                    row.lts,
                    row.confidence,
                    row.reasons,
                    row.aadt,
                    row.aadt_source,
                    f"{version}",
                )
            )
            if len(buffer) >= BATCH_ROWS:
                flush()
                log(f"  scored {report.scored:,}...")
        flush()

        if report.scored:
            writer.execute(
                f"INSERT INTO {SCHEMA}.{TABLE} "
                "(way_id, lts, confidence, reasons, aadt, aadt_source, lts_version) "
                "SELECT DISTINCT ON (way_id) way_id, lts, confidence, reasons, aadt, "
                f"aadt_source, lts_version FROM pg_temp.{staging} ORDER BY way_id "
                "ON CONFLICT (way_id) DO UPDATE SET lts = EXCLUDED.lts, "
                "confidence = EXCLUDED.confidence, reasons = EXCLUDED.reasons, "
                "aadt = EXCLUDED.aadt, aadt_source = EXCLUDED.aadt_source, "
                "lts_version = EXCLUDED.lts_version"
            )
        writer.execute(f"DROP TABLE IF EXISTS pg_temp.{staging}")

        # Counted after the write rather than derived from it: what the graph build needs to
        # know is how many ways in this region carry a level *now*, not how many this process
        # happened to compute.
        count_sql = (
            f"SELECT count(*) FROM {SCHEMA}.{TABLE} l WHERE l.lts_version = %s"
            if region_wkt is None
            else f"SELECT count(*) FROM {SCHEMA}.{TABLE} l JOIN osm.ways w USING (way_id) "
            f"WHERE l.lts_version = %s AND ST_Intersects(w.geom, ST_GeomFromText(%s, {WGS84}))"
        )
        count_args: list[Any] = [f"{version}"]
        if region_wkt is not None:
            count_args.append(region_wkt)
        writer.execute(count_sql, count_args)
        counted = writer.fetchone()
        report.covered = int(counted[0]) if counted else 0

        writer.execute(
            "INSERT INTO meta.layer_vintage (layer_schema, source, region, vintage, source_url) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (layer_schema, source, region) DO UPDATE SET "
            "vintage = EXCLUDED.vintage, source_url = EXCLUDED.source_url, loaded_at = now()",
            (SCHEMA, "lts", region, vintage, None),
        )
    connection.commit()

    report.elapsed_s = time.perf_counter() - started
    return report


def lookup(connection: Any, *, polygon_wkt: str | None = None) -> dict[int, int]:
    """`way_id -> lts`, for the importer that writes the synthetic tag into a `.pbf`.

    Returned whole rather than streamed: `add_lts_tags.py` walks a `.pbf` in OSM id order and
    needs random access, and 1.8 M int keys is ~110 MB against a pbf rewrite that already
    holds every node in Python.
    """
    sql = f"SELECT l.way_id, l.lts FROM {SCHEMA}.{TABLE} l"
    params: list[Any] = []
    if polygon_wkt is not None:
        sql += (
            " JOIN osm.ways w ON w.way_id = l.way_id "
            f"WHERE ST_Intersects(w.geom, ST_GeomFromText(%s, {WGS84}))"
        )
        params.append(polygon_wkt)
    with connection.cursor(name="way_lts_lookup") as cursor:
        cursor.itersize = FETCH_ROWS
        cursor.execute(sql, params)
        return {int(way_id): int(level) for way_id, level in cursor}


__all__ = [
    "AADT_TABLE",
    "BATCH_ROWS",
    "SCHEMA",
    "TABLE",
    "LtsReport",
    "WayLts",
    "composite_vintage",
    "compute",
    "create_statements",
    "lookup",
    "resolve_aadt",
    "score_way",
]
