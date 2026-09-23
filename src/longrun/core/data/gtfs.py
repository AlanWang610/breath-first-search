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

**`calendar_dates.txt` is read, and until M16 it was not — which was a silent correctness
bug, not a missing feature.** GTFS lets a feed define service in either of two files, and
`calendar.txt` is the *optional* one: a feed may carry no `calendar.txt` at all and
enumerate every service day in `calendar_dates.txt` instead. Those feeds exist in the wild
and small agencies publish them routinely. Against such a feed the old code built an empty
`services` map, so every `observe()` was handed an empty day-type set, every stop's `span`
stayed empty, `served` was False for all of them, and `summarise_feed` returned **zero
rows**. The feed then vanished: `transit` and `bailouts` reported *"no stop within reach"*
for a station with a train every twenty minutes, which is the one thing `in_service`'s
tri-state exists to prevent — and it happened a layer below it, where the tri-state cannot
see, because a dropped stop is not an unknown stop.

**What an exception may and may not do to a day-type summary** is the decision inside that
fix, and it is deliberately asymmetric:

*A service with no `calendar.txt` row takes its day types from its added dates.* There is no
recurring pattern to contradict, and the added dates are the whole service calendar.

*A service that has a `calendar.txt` row keeps it.* Additions do not widen it: a
weekday-only service with one added Saturday for a street fair would otherwise report
`saturday_departures` in the hundreds, and a runner finishing on an ordinary Saturday would
be told the stop is served. Removals do not narrow it either: a summary keyed on *day type*
cannot say "not this one Tuesday", and dropping "weekday" because one Tuesday is cancelled
would under-claim the other 260. Both directions lose information the schema has no room
for, and losing it loudly here is better than encoding it wrongly.

Neither this nor `calendar.txt`'s own `start_date`/`end_date` is tested for validity, so a
feed whose service ended last year still summarises as served. That is pre-existing and
unchanged; the vintage on `meta.layer_vintage` is what a reader has to go on.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
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
        # timetable was read and this stop has no Sunday service, NULL means neither
        # `calendar.txt` nor `calendar_dates.txt` gave this service a day to run on.
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

        A stop with rows in `stop_times.txt` and no service calendar anywhere - neither a
        `calendar.txt` row nor an added date in `calendar_dates.txt` - has an unreadable
        service pattern, not an empty one, and a summary of it would be a confident zero.
        Dropping it means `bailouts` reports the *stop* as absent rather than reporting it
        as never served.

        That is a real cost and it is why reading `calendar_dates.txt` mattered: on a
        calendar-dates-only feed this was False for every stop, so an agency with a train
        every twenty minutes summarised to nothing at all.
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


def parse_service_date(value: str) -> date | None:
    """A GTFS `YYYYMMDD` service date, or None when it is not one.

    None rather than an exception: one malformed row in `calendar_dates.txt` must not take
    a whole feed's service calendar down with it, which is the same rule `parse_time` and
    the stop reader already follow.
    """
    text = value.strip()
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def day_types_for(day: date) -> set[str]:
    """Which day type a calendar date falls on.

    The inverse of `day_types`, and the join between the two files: `calendar.txt` names
    day types directly and `calendar_dates.txt` names dates, so an added date has to be
    resolved to the same three buckets the schema stores.

    Derived from `CALENDAR_DAYS` rather than from a second weekday tuple. `transit.py`
    carries its own `DAY_TYPES` for the read side and may not be imported from here — a
    data module reaching into `core/scorers` is the arrow `test_layering.py` forbids — so
    the constant stays single-sourced on this side at least.
    """
    named = CALENDAR_DAYS[day.weekday()]
    return {named if named in ("saturday", "sunday") else "weekday"}


def added_service_days(rows: Any) -> dict[str, set[date]]:
    """Dates each service is **added** on, from `calendar_dates.txt`.

    `exception_type` 2 - a removal - is read and dropped on purpose. A summary keyed on day
    type cannot express "not this one Tuesday", and removing the whole day type because one
    Tuesday is cancelled would under-claim every other Tuesday of the year. See the module
    docstring: both directions lose information the schema has no room for.
    """
    added: dict[str, set[date]] = {}
    for row in rows:
        if (row.get("exception_type") or "").strip() != "1":
            continue
        when = parse_service_date(row.get("date") or "")
        if when is None:
            continue
        added.setdefault(row.get("service_id") or "", set()).add(when)
    return added


def merge_service_days(
    calendar: dict[str, set[str]], added: dict[str, set[date]]
) -> dict[str, set[str]]:
    """Day types per service, from `calendar.txt` widened only where it says nothing.

    A service `calendar.txt` describes keeps its row: an added Saturday for a street fair
    must not make a weekday-only line report hundreds of Saturday departures, because a
    runner finishing on an ordinary Saturday would then be told the stop is served. A
    service `calendar.txt` never mentions has no pattern to contradict, so its added dates
    *are* its calendar - which is the whole of a calendar-dates-only feed.
    """
    merged = dict(calendar)
    for service_id, dates in added.items():
        if service_id in merged:
            continue
        merged[service_id] = {kind for day in dates for kind in day_types_for(day)}
    return merged


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

        # Both files, because either may be absent and `calendar.txt` is the optional one.
        # `_read_csv` already treats a missing member as an empty table, so a feed with
        # only one of them reads as it should rather than raising.
        calendar: dict[str, set[str]] = {}
        for row in _read_csv(archive, "calendar.txt"):
            calendar[row.get("service_id") or ""] = day_types(row)
        services = merge_service_days(
            calendar, added_service_days(_read_csv(archive, "calendar_dates.txt"))
        )

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
    "added_service_days",
    "day_types",
    "day_types_for",
    "load_gtfs",
    "merge_service_days",
    "parse_service_date",
    "parse_time",
    "summaries_to_frame",
    "summarise_feed",
]
