#!/usr/bin/env python3
"""Write a synthetic ``lts=1..4`` tag onto every highway way in an OSM .pbf.

This is the transport mechanism for scope 7.1's claim that LTS is "computed offline per
way in PostGIS and written as an encoded value at import". GraphHopper's tag parsers read
OSM tags, so the way to get an externally computed number into an encoded value is to put
it on the way as a tag before import. GraphHopper never learns where the number came from.

**The scoring here is a stand-in, not the deliverable.** Scope 7.1 specifies Furth's
methodology (speed x lanes x separation, AADT as a modifier) computed in PostGIS against
HPMS. What is implemented below is a crude tag-only approximation whose only job in spike
S1 was to produce a plausible spread of 1-4 so the round-trip through GraphHopper could be
measured. Replace ``lts_for_way`` with a lookup against the PostGIS table keyed on OSM way
id; nothing else in this file needs to change.

Usage:
    uv run python deploy/graphhopper/scripts/add_lts_tags.py \
        data/osm/bayarea.osm.pbf data/osm/bayarea-lts.osm.pbf
"""

from __future__ import annotations

import argparse
import collections
import sys
import time
from pathlib import Path

import osmium

LTS_KEY = "lts"

# Ways carrying these highway values are separated from motor traffic, or are so slow that
# separation is moot -> LTS 1.
_SEPARATED = {
    "footway",
    "path",
    "pedestrian",
    "steps",
    "cycleway",
    "track",
    "living_street",
    "corridor",
    "bridleway",
    "platform",
}
# Baseline stress by highway class before speed/lane/sidewalk modifiers.
_BASE = {
    "residential": 1,
    "service": 1,
    "unclassified": 2,
    "road": 2,
    "tertiary": 2,
    "tertiary_link": 2,
    "secondary": 3,
    "secondary_link": 3,
    "primary": 3,
    "primary_link": 3,
    "trunk": 4,
    "trunk_link": 4,
    "motorway": 4,
    "motorway_link": 4,
    "busway": 4,
}


def _parse_speed_mph(raw: str | None) -> float | None:
    """OSM maxspeed -> mph, or None if unparseable."""
    if not raw:
        return None
    raw = raw.strip().lower()
    try:
        if raw.endswith("mph"):
            return float(raw[:-3].strip())
        if raw.endswith("km/h") or raw.endswith("kph"):
            return float(raw.rstrip("kphm/").strip()) * 0.621371
        return float(raw) * 0.621371  # bare numbers are km/h per the OSM wiki
    except ValueError:
        return None


def _parse_int(raw: str | None) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def lts_for_way(tags) -> int | None:
    """Crude LTS 1-4 for a way, or None if it is not a road at all.

    PLACEHOLDER -- see the module docstring. Replace with the PostGIS lookup.
    """
    highway = tags.get("highway")
    if highway is None:
        return None
    if highway in _SEPARATED:
        return 1

    lts = _BASE.get(highway)
    if lts is None:
        return 2  # unknown highway type: neither reward nor punish

    speed = _parse_speed_mph(tags.get("maxspeed"))
    if speed is not None:
        if speed >= 45:
            lts = max(lts, 4)
        elif speed >= 35:
            lts = max(lts, 3)
        elif speed <= 20:
            lts = min(lts, 2)

    lanes = _parse_int(tags.get("lanes"))
    if lanes is not None and lanes >= 4:
        lts = min(4, lts + 1)

    # No sidewalk on anything above a residential street is the single biggest pedestrian
    # stressor OSM actually records.
    if tags.get("sidewalk") in ("no", "none") and lts >= 2:
        lts = min(4, lts + 1)
    elif tags.get("sidewalk") in ("both", "left", "right", "separate") and lts >= 2:
        lts = max(1, lts - 1)

    return max(1, min(4, lts))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("infile", type=Path)
    ap.add_argument("outfile", type=Path)
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

    if args.outfile.exists():
        args.outfile.unlink()

    header = osmium.io.Header()
    header.set("generator", "longrun add_lts_tags.py (pyosmium)")

    hist: collections.Counter[int] = collections.Counter()
    n_ways = n_other = 0
    t0 = time.monotonic()

    writer = osmium.SimpleWriter(str(args.outfile), header=header, overwrite=True)
    try:
        for obj in osmium.FileProcessor(str(args.infile)):
            if obj.is_way():
                n_ways += 1
                lts = lts_for_way(obj.tags)
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
    size_mb = args.outfile.stat().st_size / 1e6
    print(
        f"tagged {tagged:,} of {n_ways:,} ways ({n_other:,} other objects copied) "
        f"-> {args.outfile} ({size_mb:.1f} MB, {elapsed:.1f}s)",
        file=sys.stderr,
    )
    for level in (1, 2, 3, 4):
        share = 100 * hist[level] / tagged if tagged else 0
        print(f"  lts={level}: {hist[level]:>9,}  ({share:5.1f}%)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
