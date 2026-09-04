#!/usr/bin/env python3
"""Prove that the custom `lts` encoded value actually steers pedestrian routing.

For each origin/destination pair this sends three POST /route requests that differ only in
the query-time `custom_model`:

    neutral   no lts term at all
    avoid     priority x0.02 on lts >= 3   (what scope 7.1 actually wants)
    seek      priority x0.02 on lts <= 2   (the inverse, as a control)

and reports, per route, the length-weighted share of distance at each LTS level, plus how
much of `avoid`'s geometry is shared with `seek`'s. If `lts` were being ignored the three
routes would be identical.

Requires a GraphHopper server on --url serving a graph built with `lts`
(deploy/graphhopper/import-lts.ps1).

Usage:
    uv run python deploy/graphhopper/scripts/verify_lts_routing.py
    uv run python deploy/graphhopper/scripts/verify_lts_routing.py --ev track_type
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.error
import urllib.request

PAIRS = [
    ("SF: Ferry Building -> de Young", (-122.3937, 37.7955), (-122.4686, 37.7715)),
    ("Peninsula: Stanford -> Mountain View", (-122.1430, 37.4419), (-122.0808, 37.3894)),
    ("East Bay: Lake Merritt -> UC Berkeley", (-122.2585, 37.8020), (-122.2585, 37.8719)),
    ("South Bay: San Jose State -> Santana Row", (-121.8811, 37.3352), (-121.9482, 37.3210)),
]

# Two ways of carrying LTS, per decision 0001:
#   lts        -- the custom encoded value built by the import wrapper (the chosen approach)
#   track_type -- the no-Java fallback, LTS smuggled into a stock enum as grade1..grade4
CARRIERS = {
    "lts": {
        "detail": "lts",
        "models": {
            "neutral": [],
            "avoid": [{"if": "lts >= 3", "multiply_by": "0.02"}],
            "seek": [{"if": "lts <= 2", "multiply_by": "0.02"}],
        },
        # detail values come back as ints 0-4
        "decode": lambda v: v if isinstance(v, int) else 0,
    },
    "track_type": {
        "detail": "track_type",
        "models": {
            "neutral": [],
            "avoid": [{"if": "track_type == GRADE3 || track_type == GRADE4",
                       "multiply_by": "0.02"}],
            "seek": [{"if": "track_type == GRADE1 || track_type == GRADE2",
                      "multiply_by": "0.02"}],
        },
        # NB: custom-model expressions spell enums UPPERCASE (`track_type == GRADE3`) but
        # path details come back lowercase ("grade3" / "missing"). Compare case-insensitively.
        "decode": lambda v: (
            int(v[5:]) if isinstance(v, str) and v.lower().startswith("grade") else 0
        ),
    },
}


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = a
    lon2, lat2 = b
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def route(url: str, start, end, priority, detail: str) -> dict:
    body = {
        "points": [list(start), list(end)],
        "profile": "foot",
        "points_encoded": False,
        "instructions": False,
        "details": [detail, "osm_way_id"],
        "custom_model": {"priority": priority} if priority else {},
        "ch.disable": True,
    }
    req = urllib.request.Request(
        f"{url}/route",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def lts_profile(path: dict, carrier: dict) -> tuple[float, dict[int, float]]:
    """(total metres, {lts level -> metres}) computed from the carrier's path detail."""
    coords = path["points"]["coordinates"]
    seg = [haversine(coords[i][:2], coords[i + 1][:2]) for i in range(len(coords) - 1)]
    by_level: dict[int, float] = {}
    for lo, hi, value in path["details"][carrier["detail"]]:
        level = carrier["decode"](value)
        by_level[level] = by_level.get(level, 0.0) + sum(seg[lo:hi])
    return sum(seg), by_level


def geometry_overlap(a: dict, b: dict) -> float:
    """Fraction of a's length whose way ids also appear in b."""
    coords = a["points"]["coordinates"]
    seg = [haversine(coords[i][:2], coords[i + 1][:2]) for i in range(len(coords) - 1)]
    b_ways = {w[2] for w in b["details"]["osm_way_id"]}
    shared = sum(sum(seg[lo:hi]) for lo, hi, w in a["details"]["osm_way_id"] if w in b_ways)
    total = sum(seg)
    return shared / total if total else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://localhost:8989")
    ap.add_argument("--ev", choices=sorted(CARRIERS), default="lts",
                    help="which encoded value carries LTS (see decision 0001)")
    args = ap.parse_args()

    carrier = CARRIERS[args.ev]
    print(f"carrier encoded value: {args.ev}   server: {args.url}")
    failures = 0
    for label, start, end in PAIRS:
        print(f"\n=== {label} ===")
        paths = {}
        for name, priority in carrier["models"].items():
            r = route(args.url, start, end, priority, carrier["detail"])
            if "paths" not in r:
                print(f"  {name:8s} ERROR {json.dumps(r)[:300]}")
                failures += 1
                continue
            paths[name] = r["paths"][0]

        if len(paths) != len(carrier["models"]):
            continue

        print(f"  {'model':8s} {'dist_m':>9s}  {'lts1':>6s} {'lts2':>6s} {'lts3':>6s} {'lts4':>6s} {'lts0':>6s}")
        for name, p in paths.items():
            total, by = lts_profile(p, carrier)
            pct = lambda lvl: 100 * by.get(lvl, 0.0) / total if total else 0.0
            print(
                f"  {name:8s} {total:9.0f}  "
                f"{pct(1):5.1f}% {pct(2):5.1f}% {pct(3):5.1f}% {pct(4):5.1f}% {pct(0):5.1f}%"
            )

        avoid_total, avoid_by = lts_profile(paths["avoid"], carrier)
        seek_total, seek_by = lts_profile(paths["seek"], carrier)
        neutral_total, _ = lts_profile(paths["neutral"], carrier)
        hi_avoid = sum(avoid_by.get(l, 0.0) for l in (3, 4)) / avoid_total * 100
        hi_seek = sum(seek_by.get(l, 0.0) for l in (3, 4)) / seek_total * 100
        overlap = geometry_overlap(paths["avoid"], paths["seek"])
        print(
            f"  -> lts>=3 share: avoid {hi_avoid:.1f}%  vs  seek {hi_seek:.1f}%   "
            f"| detour vs neutral: {100 * (avoid_total / neutral_total - 1):+.1f}%  "
            f"| avoid/seek way overlap: {100 * overlap:.1f}%"
        )
        if hi_avoid >= hi_seek:
            print(f"  !! NO SEPARATION -- {args.ev} is not steering this pair")
            failures += 1

    summary = f"FAILURES: {failures}" if failures else f"OK: {args.ev} steers every pair"
    print(f"\n{summary}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
