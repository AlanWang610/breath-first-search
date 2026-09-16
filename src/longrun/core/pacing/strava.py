"""A Strava bulk export, read where it sits (scope 6.2, 3.7).

Written against a real archive rather than a description of one, and four things in that
archive decided its shape:

* **`activities.csv` decides what an activity was, not the file.** Strava's type is what a
  person chose or corrected; a FIT file's `sport` is whatever mode the watch was left in.
  They disagree - runs recorded in walking or cycling mode and relabelled afterwards - and
  the person is the one to believe, which is scope 6.3's "stated beats inferred" at the
  scale of one activity. The disagreement is counted and reported, never silent.
* **Only runs are opened.** Rides, walks, swims and workouts are counted by type from the
  CSV and their files are never decoded.
* **The CSV repeats column names** (`Elapsed Time`, `Distance`, `Commute` and more), so
  columns are found by their first occurrence. `csv.DictReader` would silently keep the
  *last*, which for `Distance` is a different unit.
* **There is no race marker.** No workout-type column is exported, so a race cannot be told
  from a training run - and the reader says so rather than reporting "0 races excluded",
  which would read as a check that found nothing.

The rest of the archive - messages, contacts, logins, media, privacy zones - is never
opened, and a zip is read in place rather than extracted.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from longrun.core.pacing.history import (
    RUNNING_SPORTS,
    UnsupportedActivityFile,
    read_activity,
)

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.pacing.history import Activity

INDEX = "activities.csv"

#: Strava's activity types that are running on the ground, spelled as `RUNNING_SPORTS`
#: spells them. "Virtual Run" is deliberately absent: a treadmill sets no grade.
RUN_TYPES = {"Run": "running", "Trail Run": "trail_running"}

_COLUMNS = ("Activity ID", "Activity Type", "Filename")


class NotAStravaExport(ValueError):
    """The source has no `activities.csv`, or one without the columns this reads."""


@dataclass(frozen=True)
class ExportRead:
    """The runs an export held, and an account of everything that was not one."""

    activities: list[Activity]
    listed: int = 0
    not_runs: dict[str, int] = field(default_factory=dict)
    no_track: int = 0
    unreadable: list[str] = field(default_factory=list)
    unsupported: dict[str, int] = field(default_factory=dict)
    relabelled: int = 0
    notes: list[str] = field(default_factory=list)


def is_strava_export(source: Path) -> bool:
    """A folder or a `.zip` with `activities.csv` at its top level."""
    if source.is_dir():
        return (source / INDEX).is_file()
    if source.is_file() and zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            return INDEX in archive.namelist()
    return False


def read_export(source: Path) -> ExportRead:
    """Every run in a Strava export, as `Activity` values carrying Strava's own id."""
    if not is_strava_export(source):
        raise NotAStravaExport(f"{source}: no {INDEX} at the top level")
    opener = _Zip(source) if source.is_file() else _Folder(source)
    with opener as archive:
        header, rows = _index(archive.read(INDEX))
        missing = [name for name in _COLUMNS if name not in header]
        if missing:
            raise NotAStravaExport(f"{INDEX} has no {', '.join(missing)} column")
        id_col, type_col, file_col = (header.index(name) for name in _COLUMNS)

        activities: list[Activity] = []
        not_runs: Counter[str] = Counter()
        unsupported: Counter[str] = Counter()
        unreadable: list[str] = []
        no_track = relabelled = 0
        for row in rows:
            row = row + [""] * (len(header) - len(row))
            kind, name, activity_id = row[type_col], row[file_col], row[id_col]
            sport = RUN_TYPES.get(kind)
            if sport is None:
                not_runs[kind or "(no type)"] += 1
                continue
            if not name:
                no_track += 1
                continue
            try:
                activity = read_activity(name, archive.read(name))
            except UnsupportedActivityFile:
                unsupported[_suffix(name)] += 1
                continue
            except Exception as exc:  # noqa: BLE001 - one bad file is not the whole history
                unreadable.append(f"{activity_id}: {type(exc).__name__}")
                continue
            if activity is None:
                no_track += 1
                continue
            if activity.sport is not None and activity.sport not in RUNNING_SPORTS:
                relabelled += 1
            activities.append(dataclasses.replace(activity, activity_id=activity_id, sport=sport))

    return ExportRead(
        activities=activities,
        listed=len(rows),
        not_runs=dict(not_runs.most_common()),
        no_track=no_track,
        unreadable=unreadable,
        unsupported=dict(unsupported),
        relabelled=relabelled,
        notes=_notes(no_track, relabelled, unsupported, unreadable),
    )


def _notes(
    no_track: int, relabelled: int, unsupported: Counter[str], unreadable: list[str]
) -> list[str]:
    notes = [
        "Strava's export carries no race marker, so race efforts could not be excluded: "
        "a race in this history is binned as training pace"
    ]
    if relabelled:
        notes.append(
            f"{relabelled} run(s) were recorded in another sport mode and are labelled runs "
            "in Strava; the Strava label is used, because a person set it"
        )
    if no_track:
        notes.append(
            f"{no_track} run(s) have no GPS track (treadmill, indoor, or no file) and "
            "contribute nothing"
        )
    if unsupported:
        listed = ", ".join(f"{suffix} {count}" for suffix, count in unsupported.items())
        notes.append(f"runs in a format this does not decode were skipped: {listed}")
    if unreadable:
        notes.append(f"{len(unreadable)} run file(s) could not be decoded")
    return notes


def _index(data: bytes) -> tuple[list[str], list[list[str]]]:
    reader = csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""))
    header = next(reader, [])
    return header, [row for row in reader if row]


def _suffix(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    return "." + base.split(".", 1)[1] if "." in base else "(none)"


class _Zip:
    def __init__(self, path: Path) -> None:
        self._archive = zipfile.ZipFile(path)

    def __enter__(self) -> _Zip:
        return self

    def __exit__(self, *_: object) -> None:
        self._archive.close()

    def read(self, name: str) -> bytes:
        return self._archive.read(name)


class _Folder:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def __enter__(self) -> _Folder:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, name: str) -> bytes:
        path = (self._root / name).resolve()
        # `Filename` comes from a CSV, and a CSV is text anybody can edit.
        if not path.is_relative_to(self._root):
            raise ValueError(f"{name}: outside the export")
        return path.read_bytes()


__all__ = [
    "INDEX",
    "RUN_TYPES",
    "ExportRead",
    "NotAStravaExport",
    "is_strava_export",
    "read_export",
]
