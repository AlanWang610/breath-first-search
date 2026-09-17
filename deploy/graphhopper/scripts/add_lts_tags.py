#!/usr/bin/env python3
"""Write the offline ``lts=1..4`` score onto every road in an OSM .pbf, from PostGIS.

This is the transport mechanism for scope 7.1's claim that LTS is "computed offline per
way in PostGIS and written as an encoded value at import". GraphHopper's tag parsers read
OSM tags, so the way to get an externally computed number into an encoded value is to put
it on the way as a tag before import. GraphHopper never learns where the number came from.

**The score comes from `osm_lts.way_lts`, keyed on OSM way id** (M9, ADR 0025). Until then
this file carried its own ``lts_for_way`` - a crude tag-only approximation whose docstring
called itself a placeholder and asked to be replaced with exactly this lookup. It had
survived long enough to matter: on the Ozarks it and `core/routing/lts.py::lts_from_tags`
disagreed about 469 of 1,600 ways, so the level a plan *reported* was never the level its
route had been drawn to *avoid*.

There is deliberately **no fallback scoring path**. A graph silently built on the wrong
numbers is the failure this milestone exists to remove, and a fallback is how it would come
back: an unreachable database would quietly produce a plausible `.pbf` and a graph nobody
could tell from a correct one.

Usage:
    uv run longrun build-region deploy/regions/ozarks.yaml      # fills osm_lts.way_lts
    uv run python deploy/graphhopper/scripts/add_lts_tags.py \\
        data/osm/ozarks.osm.pbf data/osm/ozarks-lts.osm.pbf
"""

from __future__ import annotations

import argparse
import collections
import sys
import time
from pathlib import Path

import osmium

LTS_KEY = "lts"

#: The share of the `.pbf`'s roads that must carry a level before the output is kept.
#:
#: The floor exists because the two ways this can fail are indistinguishable in a finished
#: graph. A lookup returning nothing and a lookup returning the right answer both produce a
#: `.pbf` that imports cleanly and serves routes; the difference only shows up as routes that
#: are subtly worse, months later, with no error anywhere. `osm.ways` is loaded from this same
#: file, so anything much below 1.0 means the table was built from a different extract - which
#: is a thing to stop for, not to warn about.
MIN_HIT_RATE = 0.95


def load_lookup(dsn: str | None) -> dict[int, int]:
    """`way_id -> lts` for every scored way, through the one implementation.

    Not scoped to a region polygon: this file is a **bbox** clip and `osm.ways` holds all of
    it, so a polygon-scoped lookup would leave every way between the polygon and the bbox edge
    untagged - reaching GraphHopper as the encoded value's 0, which reads as "unknown" rather
    than as "missing" to every custom model.
    """
    import psycopg

    from longrun.core.data.postgis import dsn_from_env
    from longrun.regions.lts import lookup

    target = dsn or dsn_from_env()
    with psycopg.connect(target, connect_timeout=10) as connection:
        return lookup(connection)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("infile", type=Path)
    ap.add_argument("outfile", type=Path)
    ap.add_argument("--dsn", default=None, help="PostGIS DSN; defaults to LONGRUN_POSTGIS_DSN.")
    ap.add_argument(
        "--min-hit-rate",
        type=float,
        default=MIN_HIT_RATE,
        help=f"Refuse to write below this share of roads scored (default {MIN_HIT_RATE}).",
    )
    ap.add_argument(
        "--also-tracktype",
        action="store_true",
        help=(
            "additionally write tracktype=grade<lts>, so a stock GraphHopper can carry LTS "
            "in its existing `track_type` encoded value with no custom Java. This is the "
            "fallback of decision 0001 and it destroys the real meaning of track_type -- "
            "only use it if the import wrapper is unavailable."
        ),
    )
    args = ap.parse_args()

    table = load_lookup(args.dsn)
    print(f"{len(table):,} way(s) scored in osm_lts.way_lts", file=sys.stderr)
    if not table:
        print(
            "nothing to look up: run `longrun build-region <spec>` first, which fills "
            "osm_lts.way_lts from the loaded OSM extract",
            file=sys.stderr,
        )
        return 2

    # Written beside the target and renamed on success, so a refused run never leaves a
    # plausible-looking `.pbf` for someone to import by mistake. The `~` prefix rather than a
    # `.partial` suffix because pyosmium picks its writer from the file extension and refuses
    # a name it cannot classify.
    staging = args.outfile.with_name(f"~{args.outfile.name}")
    for path in (args.outfile, staging):
        if path.exists():
            path.unlink()

    header = osmium.io.Header()
    header.set("generator", "longrun add_lts_tags.py (pyosmium, osm_lts.way_lts)")

    hist: collections.Counter[int] = collections.Counter()
    n_ways = n_other = n_roads = n_missing = 0
    t0 = time.monotonic()

    writer = osmium.SimpleWriter(str(staging), header=header, overwrite=True)
    try:
        for obj in osmium.FileProcessor(str(args.infile)):
            if obj.is_way():
                n_ways += 1
                # The denominator is roads, not ways: a `.pbf` is mostly building outlines and
                # landuse, and counting those as misses would hide a real gap under a big
                # number. `highway` is the same test `regions.lts.score_way` uses to decide
                # whether a way gets a row at all.
                is_road = bool(obj.tags.get("highway"))
                lts = table.get(obj.id)
                if is_road:
                    n_roads += 1
                    if lts is None:
                        n_missing += 1
                if lts is None:
                    writer.add_way(obj)
                else:
                    hist[lts] += 1
                    tags = dict(obj.tags)
                    tags[LTS_KEY] = str(lts)
                    if args.also_tracktype:
                        tags["tracktype"] = f"grade{lts}"
                    writer.add_way(obj.replace(tags=tags, nodes=list(obj.nodes)))
            else:
                n_other += 1
                writer.add(obj)
    finally:
        writer.close()

    elapsed = time.monotonic() - t0
    tagged = sum(hist.values())
    hit_rate = (n_roads - n_missing) / n_roads if n_roads else 0.0

    print(
        f"tagged {tagged:,} of {n_ways:,} ways ({n_other:,} other objects copied) in "
        f"{elapsed:.1f}s",
        file=sys.stderr,
    )
    # The two numbers the old histogram alone could not separate: "no rows came back" and
    # "every row came back saying the same thing" look identical once the graph is built.
    print(
        f"  roads in this extract: {n_roads:,}; scored {n_roads - n_missing:,} "
        f"({100 * hit_rate:.2f}%); no row for {n_missing:,}",
        file=sys.stderr,
    )
    for level in (1, 2, 3, 4):
        share = 100 * hist[level] / tagged if tagged else 0
        print(f"  lts={level}: {hist[level]:>9,}  ({share:5.1f}%)", file=sys.stderr)

    if hit_rate < args.min_hit_rate:
        staging.unlink(missing_ok=True)
        print(
            f"REFUSED: {100 * hit_rate:.2f}% of roads scored, below the "
            f"{100 * args.min_hit_rate:.0f}% floor. The table was most likely built from a "
            f"different extract than {args.infile.name}; re-run build-region for this region. "
            f"No .pbf written.",
            file=sys.stderr,
        )
        return 1

    staging.rename(args.outfile)
    size_mb = args.outfile.stat().st_size / 1e6
    print(f"-> {args.outfile} ({size_mb:.1f} MB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
