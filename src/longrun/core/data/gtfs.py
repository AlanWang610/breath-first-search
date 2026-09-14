"""Transit stops, with the service at each summarised (scope 7.7, 13 step 2).

`bailouts` asks "if this goes wrong at 32 km, can I get out?" and `transit` asks "what
serves the start and the finish, and when". Both are questions about **whether a stop is
useful at a time**, not about the next three departures — so what a region build stores is
a *service summary* per stop, not a timetable.

That is a real decision and not a shortcut, for two reasons.

**A scorer reaches a layer through `LayerStore`, and every method on it is spatial.** The
four corridor queries are what the whole seam is; a timetable lookup is not one of them, so
a `departures` table would sit in the database unreachable by anything a scorer can call.
Precomputing the summary onto the stop makes "which stops can get me out of here at 17:30
on a Saturday" a corridor query like every other scorer's.

**Precomputation is what a region build is for.** Scope 13 loads national sources once and
slices them per region; collapsing 50,000 BART stop times into 50 stop summaries at build
time is exactly that, and it moves the cost off the ~3-minute plan budget (§6.4).

What this gives up is `transit_at`'s exact departure board (§7.7). That is a tool rather
than a scorer, it needs GTFS-RT to be worth much, and it should be added as its own path
when something needs it — not smuggled in as a table nothing can read.

**Feeds are named by the caller, not discovered.** §13 step 2 says "all intersecting GTFS
feeds", and discovery needs a registry (the Mobility Database, transit.land) with its own
key and its own licence. `load_gtfs` takes the feeds a region declares, the same way
`load_tiger` takes the states it crosses.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from longrun.core.data.national import NationalSource

if TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

    from geopandas import GeoDataFrame

#: Where the summary lands, and the layer name a scorer asks for.
GTFS_STOPS = NationalSource(
    layer="transit_stops",
    schema="gtfs",
    table="stops",
    key="stop_key",
    geometry="Point",
    columns={
        "feed": "text",
        "stop_id": "text",
        "name": "text",
        "routes": "text",
        "modes": "text",
        # Departures per day type. Zero and NULL are different answers: zero means the
        # timetable was read and this stop has no Sunday service, NULL means the feed
        # carried no calendar to read.
        "weekday_departures": "integer",
        "saturday_departures": "integer",
        "sunday_departures": "integer",
        # Service span in seconds after midnight, **per day type**. One combined span
        # would over-claim: a stop running 05:00-24:00 on weekdays and 08:00-22:00 on
        # Sunday would report Sunday 06:00 as served. GTFS times run past 24:00:00 for
        # trips after midnight and that is kept, not wrapped - a stop whose last departure
        # is 25:10 is served at 01:10, and wrapping would make it the *first* of the day.
        "weekday_first_s": "integer",
        "weekday_last_s": "integer",
        "saturday_first_s": "integer",
        "saturday_last_s": "integer",
        "sunday_first_s": "integer",
        "sunday_last_s": "integer",
    },
    source="gtfs",
    licence="per agency",
    url_template="{url}",
    vintage="gtfs",
)

#: `route_type` values, as GTFS enumerates them. Carried as words because the plan sheet
#: reads "BART is heavy rail" better than it reads "route_type 1".
ROUTE_MODES: dict[str, str] = {
    "0": "tram",
    "1": "subway",
    "2": "rail",
    "3": "bus",
    "4": "ferry",
    "5": "cable_tram",
    "6": "aerial",
    "7": "funicular",
    "11": "trolleybus",
    "12": "monorail",
}

#: `location_type` values that are a place a passenger boards. 1 is a station (its child
#: platforms carry the times), 2-4 are entrances, nodes and boarding areas.
BOARDABLE_LOCATION_TYPES = frozenset({"", "0"})

#: GTFS calendar columns in weekday order, so `days[weekday()]` indexes them directly.
CALENDAR_DAYS: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


@dataclass
class StopSummary:
    """One stop, with what serves it collapsed to the questions a scorer asks."""

    stop_id: str
    name: str
    lat: float
    lon: float
    routes: set[str] = field(default_factory=set)
    modes: set[str] = field(default_factory=set)
    #: Departure counts keyed by day type: "weekday", "saturday", "sunday".
    departures: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    #: First and last departure per day type, for the same reason the counts are per type.
    span: dict[str, tuple[int, int]] = field(default_factory=dict)

    def observe(self, seconds: int, day_types: set[str]) -> None:
        for day_type in day_types:
            self.departures[day_type] += 1
            first, last = self.span.get(day_type, (seconds, seconds))
            self.span[day_type] = (min(first, seconds), max(last, seconds))

    @property
    def served(self) -> bool:
        """Whether any timetable was found for this stop at all.

        A stop with rows in `stop_times.txt` but no matching `calendar.txt` entry has an
        unreadable service pattern, not an empty one, and a summary of it would be a
        confident zero. Dropping it means `bailouts` reports the *stop* as absent rather
        than reporting it as never served.
        """
        return bool(self.span)


def parse_time(value: str) -> int | None:
    """GTFS `HH:MM:SS` to seconds after midnight, hours past 24 kept as they are.

    A trip departing at 25:10:00 is the 01:10 service *of the previous service day*, and
    collapsing it to 3,600 s would make it look like the first departure of the morning.
    """
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(p) for p in parts)
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


def day_types(row: dict[str, str]) -> set[str]:
    """Which of weekday / saturday / sunday a `calendar.txt` row runs on.

    Weekday is any of Monday-Friday rather than all five: a commute-only service that runs
    Monday to Thursday is still weekday service, and a runner asking whether they can get
    home on a Tuesday is not helped by an all-five test.
    """
    found: set[str] = set()
    if any(row.get(day) == "1" for day in CALENDAR_DAYS[:5]):
        found.add("weekday")
    if row.get("saturday") == "1":
        found.add("saturday")
    if row.get("sunday") == "1":
        found.add("sunday")
    return found


def _read_csv(archive: zipfile.ZipFile, member: str) -> Any:
    """Stream one GTFS table. A missing optional member is an empty table, not an error."""
    try:
        raw = archive.read(member)
    except KeyError:
        return iter(())
    # utf-8-sig: GTFS files in the wild routinely carry a byte-order mark, and without this
    # the first column's name comes back as "﻿stop_id" and every lookup of it misses.
    return csv.DictReader(io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8-sig", newline=""))


def summarise_feed(path: Path, feed: str) -> list[StopSummary]:
    """Collapse one GTFS zip into one row per boardable stop.

    Streams `stop_times.txt`, which is the only member large enough to matter — 50,000 rows
    for BART, and two orders of magnitude more for a large bus operator.
    """
    with zipfile.ZipFile(path) as archive:
        stops: dict[str, StopSummary] = {}
        for row in _read_csv(archive, "stops.txt"):
            if (row.get("location_type") or "") not in BOARDABLE_LOCATION_TYPES:
                continue
            try:
                lat, lon = float(row["stop_lat"]), float(row["stop_lon"])
            except (KeyError, TypeError, ValueError):
                continue
            stop_id = row.get("stop_id") or ""
            stops[stop_id] = StopSummary(
                stop_id=stop_id, name=row.get("stop_name") or stop_id, lat=lat, lon=lon
            )

        routes: dict[str, tuple[str, str]] = {}
        for row in _read_csv(archive, "routes.txt"):
            label = (row.get("route_short_name") or row.get("route_long_name") or "").strip()
            mode = ROUTE_MODES.get((row.get("route_type") or "").strip(), "other")
            routes[row.get("route_id") or ""] = (label, mode)

        services: dict[str, set[str]] = {}
        for row in _read_csv(archive, "calendar.txt"):
            services[row.get("service_id") or ""] = day_types(row)

        trips: dict[str, tuple[str, str]] = {}
        for row in _read_csv(archive, "trips.txt"):
            trips[row.get("trip_id") or ""] = (
                row.get("route_id") or "",
                row.get("service_id") or "",
            )

        for row in _read_csv(archive, "stop_times.txt"):
            stop = stops.get(row.get("stop_id") or "")
            if stop is None:
                continue
            seconds = parse_time(row.get("departure_time") or row.get("arrival_time") or "")
            if seconds is None:
                continue
            route_id, service_id = trips.get(row.get("trip_id") or "", ("", ""))
            label, mode = routes.get(route_id, ("", "other"))
            if label:
                stop.routes.add(label)
            stop.modes.add(mode)
            stop.observe(seconds, services.get(service_id, set()))

    return [s for s in stops.values() if s.served]


def summaries_to_frame(summaries: list[StopSummary], feed: str) -> GeoDataFrame:
    """Stop summaries as the frame `load_frame` writes."""
    import geopandas as gpd
    from shapely.geometry import Point

    records = [
        {
            "stop_key": f"{feed}:{s.stop_id}",
            "feed": feed,
            "stop_id": s.stop_id,
            "name": s.name,
            "routes": ",".join(sorted(s.routes)) or None,
            "modes": ",".join(sorted(s.modes)) or None,
            "weekday_departures": s.departures.get("weekday", 0),
            "saturday_departures": s.departures.get("saturday", 0),
            "sunday_departures": s.departures.get("sunday", 0),
            **{
                f"{day}_{edge}_s": value
                for day in ("weekday", "saturday", "sunday")
                for edge, value in zip(
                    ("first", "last"), s.span.get(day, (None, None)), strict=True
                )
            },
        }
        for s in summaries
    ]
    if not records:
        columns = ["stop_key", *GTFS_STOPS.columns]
        return gpd.GeoDataFrame({c: [] for c in columns}, geometry=[], crs="EPSG:4326")
    return gpd.GeoDataFrame(
        records, geometry=[Point(s.lon, s.lat) for s in summaries], crs="EPSG:4326"
    )


def load_gtfs(
    connection: Any,
    region: str,
    feeds: dict[str, Path],
    *,
    source_urls: dict[str, str] | None = None,
) -> dict[str, int]:
    """Load one summary row per stop, for each named feed, into `gtfs.stops`.

    `feeds` maps a short feed id — which becomes half the primary key — to a local zip.
    The id has to be stable across builds: it is what keeps two agencies that both number
    a stop `1` from overwriting each other, and what lets a reload replace a feed's rows
    rather than duplicate them.
    """
    from longrun.core.data.national import load_frame

    urls = source_urls or {}
    counts: dict[str, int] = {}
    for feed, path in feeds.items():
        frame = summaries_to_frame(summarise_feed(path, feed), feed)
        counts[feed] = load_frame(
            connection,
            frame,
            GTFS_STOPS,
            region=region,
            vintage=f"gtfs-{feed}",
            source_url=urls.get(feed),
        )
    return counts


__all__ = [
    "BOARDABLE_LOCATION_TYPES",
    "CALENDAR_DAYS",
    "GTFS_STOPS",
    "ROUTE_MODES",
    "StopSummary",
    "day_types",
    "load_gtfs",
    "parse_time",
    "summaries_to_frame",
    "summarise_feed",
]
