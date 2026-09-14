#!/usr/bin/env python3
"""Clip an OSM .pbf to a bounding box with complete-ways semantics.

A naive per-node bbox filter shreds way geometry: a way with one node outside the
box loses that node and the segment silently shortens. This reproduces
`osmium extract --strategy=complete_ways`:

  1. mark every node inside the bbox
  2. mark every way with at least one marked node
  3. mark every relation with at least one marked member
  4. back-complete: pull in *all* nodes referenced by marked ways (including the
     ones outside the bbox) and all ways/nodes referenced by marked relations
  5. copy the marked object ids through to the output

Uses pyosmium's native ``IdTracker`` (a dense id set, so step 4 costs bits per
id, not a Python set of 60M ints).

Usage:
    uv run python deploy/graphhopper/scripts/clip_pbf.py \
        data/osm/norcal-latest.osm.pbf data/osm/bayarea.osm.pbf \
        --bbox -123.2,36.9,-121.5,38.5
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import osmium


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("infile", type=Path)
    ap.add_argument("outfile", type=Path)
    ap.add_argument(
        "--bbox",
        required=True,
        help="minlon,minlat,maxlon,maxlat in WGS84 degrees",
    )
    ap.add_argument(
        "--relation-depth",
        type=int,
        default=0,
        help="how deep to complete nested relations (0 = no nesting)",
    )
    args = ap.parse_args()

    minlon, minlat, maxlon, maxlat = (float(v) for v in args.bbox.split(","))
    # pyosmium stores coordinates as int32 in 1e-7 degrees; compare in that space
    # so the hot loop does integer, not float, comparisons.
    x0, y0 = int(minlon * 1e7), int(minlat * 1e7)
    x1, y1 = int(maxlon * 1e7), int(maxlat * 1e7)

    if args.outfile.exists():
        args.outfile.unlink()

    tracker = osmium.IdTracker()

    t0 = time.monotonic()
    n_nodes_in = n_ways_in = n_rels_in = 0
    for obj in osmium.FileProcessor(str(args.infile)):
        if obj.is_node():
            loc = obj.location
            if loc.valid() and x0 <= loc.x <= x1 and y0 <= loc.y <= y1:
                tracker.add_node(obj.id)
                n_nodes_in += 1
        elif obj.is_way():
            if tracker.contains_any_references(obj):
                tracker.add_way(obj.id)
                n_ways_in += 1
        else:
            if tracker.contains_any_references(obj):
                tracker.add_relation(obj.id)
                n_rels_in += 1
    t_mark = time.monotonic() - t0
    print(
        f"[1/3] marked  {n_nodes_in:>10,} nodes in bbox, "
        f"{n_ways_in:>9,} ways, {n_rels_in:>8,} relations  ({t_mark:.1f}s)",
        file=sys.stderr,
    )

    t0 = time.monotonic()
    tracker.complete_backward_references(str(args.infile), relation_depth=args.relation_depth)
    t_complete = time.monotonic() - t0
    print(f"[2/3] back-completed references ({t_complete:.1f}s)", file=sys.stderr)

    header = osmium.io.Header()
    header.add_box(osmium.osm.Box(minlon, minlat, maxlon, maxlat))
    header.set("generator", "longrun clip_pbf.py (pyosmium)")

    t0 = time.monotonic()
    counts = {"n": 0, "w": 0, "r": 0}
    writer = osmium.SimpleWriter(str(args.outfile), header=header, overwrite=True)
    try:
        for obj in osmium.FileProcessor(str(args.infile)).with_filter(tracker.id_filter()):
            if obj.is_node():
                counts["n"] += 1
            elif obj.is_way():
                counts["w"] += 1
            else:
                counts["r"] += 1
            writer.add(obj)
    finally:
        writer.close()
    t_write = time.monotonic() - t0

    size_mb = args.outfile.stat().st_size / 1e6
    print(
        f"[3/3] wrote {counts['n']:,} nodes, {counts['w']:,} ways, "
        f"{counts['r']:,} relations -> {args.outfile} ({size_mb:.1f} MB, {t_write:.1f}s)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
