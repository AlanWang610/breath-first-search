"""A Strava bulk export becomes runs, and everything that was not a run is accounted for.

Built as a zip in `tmp_path` with the real export's layout: `activities.csv` at the top
level, originals under `activities/`, the CSV's repeated column names kept. The tracks are
synthetic GPX, somewhere in Kansas, and carry nothing from anybody's history.

Two assertions are the point of the module. A ride's file is **never opened** - its bytes
here are garbage, so opening it would show up as an unreadable run - and a run recorded in
another sport mode is kept as a run, because the CSV is what a person set.
"""

from __future__ import annotations

import csv
import gzip
import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import gpxpy.gpx
import pytest

from longrun.core.pacing.strava import NotAStravaExport, is_strava_export, read_export

START = datetime(2026, 3, 15, 13, 0, tzinfo=UTC)

#: The first columns of a real export, repeated names included. `Distance` appears twice,
#: in kilometres and then in metres.
HEADER = [
    "Activity ID",
    "Activity Date",
    "Activity Name",
    "Activity Type",
    "Activity Description",
    "Elapsed Time",
    "Distance",
    "Commute",
    "Filename",
    "Elapsed Time",
    "Distance",
    "Commute",
]


def gpx_track(*, kind: str | None = "running", metres: float = 3000.0) -> bytes:
    """A straight track north at 2.8 m/s, one point every 11 m."""
    gpx = gpxpy.gpx.GPX()
    track = gpxpy.gpx.GPXTrack()
    track.type = kind
    segment = gpxpy.gpx.GPXTrackSegment()
    steps = int(metres / 11.1)
    segment.points = [
        gpxpy.gpx.GPXTrackPoint(
            latitude=39.0 + i * 0.0001,
            longitude=-98.0,
            elevation=500.0,
            time=START + timedelta(seconds=4 * i),
        )
        for i in range(steps)
    ]
    track.segments.append(segment)
    gpx.tracks.append(track)
    return gpx.to_xml(version="1.1").encode()


def _row(activity_id: str, kind: str, filename: str) -> list[str]:
    row = [""] * len(HEADER)
    row[0], row[3], row[8] = activity_id, kind, filename
    return row


def _csv(rows: list[list[str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HEADER)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _export_files() -> dict[str, bytes]:
    return {
        "activities.csv": _csv(
            [
                _row("101", "Run", "activities/101.gpx"),
                _row("102", "Ride", "activities/102.gpx"),
                _row("103", "Run", "activities/103.gpx.gz"),
                _row("104", "Run", ""),
                _row("105", "Run", "activities/105.tcx.gz"),
                _row("106", "Run", "activities/106.fit.gz"),
                _row("107", "Run", "activities/107.gpx"),
                _row("108", "Virtual Run", "activities/108.gpx"),
            ]
        ),
        "activities/101.gpx": gpx_track(),
        # Garbage: opening it would make an unreadable run, so it must never be opened.
        "activities/102.gpx": b"\x00 not a track",
        "activities/103.gpx.gz": gzip.compress(gpx_track()),
        "activities/105.tcx.gz": gzip.compress(b"<TrainingCenterDatabase/>"),
        "activities/106.fit.gz": gzip.compress(b"\x0e\x10garbage that is not a FIT file"),
        "activities/107.gpx": gpx_track(kind="cycling"),
        "activities/108.gpx": gpx_track(),
        "messages.json": b"[]",
    }


@pytest.fixture
def export_zip(tmp_path: Path) -> Path:
    path = tmp_path / "export_1.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in _export_files().items():
            archive.writestr(name, data)
    return path


@pytest.fixture
def export_folder(tmp_path: Path) -> Path:
    root = tmp_path / "export_1"
    for name, data in _export_files().items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    return root


def test_only_runs_are_read_and_they_carry_stravas_ids(export_zip: Path) -> None:
    read = read_export(export_zip)

    assert sorted(a.activity_id for a in read.activities) == ["101", "103", "107"]
    assert {a.sport for a in read.activities} == {"running"}
    assert read.listed == 8


def test_a_ride_is_counted_by_type_and_its_file_never_opened(export_zip: Path) -> None:
    read = read_export(export_zip)

    assert read.not_runs == {"Ride": 1, "Virtual Run": 1}
    assert not any(entry.startswith("102") for entry in read.unreadable)


def test_stravas_label_beats_the_devices_sport_mode_and_says_so(export_zip: Path) -> None:
    """Activity 107's file says cycling. A person labelled it a run, and scope 6.3 believes
    the person - but the disagreement is reported, not absorbed."""
    read = read_export(export_zip)

    relabelled = next(a for a in read.activities if a.activity_id == "107")
    assert relabelled.sport == "running"
    assert read.relabelled == 1
    assert any("another sport mode" in note for note in read.notes)


def test_everything_that_is_not_a_run_track_is_accounted_for(export_zip: Path) -> None:
    read = read_export(export_zip)

    assert read.no_track == 1  # 104, a run with no file
    assert read.unsupported == {".tcx.gz": 1}
    assert [entry.split(":")[0] for entry in read.unreadable] == ["106"]
    accounted = (
        len(read.activities)
        + sum(read.not_runs.values())
        + read.no_track
        + sum(read.unsupported.values())
        + len(read.unreadable)
    )
    assert accounted == read.listed


def test_the_missing_race_marker_is_said_rather_than_read_as_zero(export_zip: Path) -> None:
    """A Strava export has no workout-type column. "0 races excluded" would read as a check
    that ran and found nothing; it is a check that could not run."""
    read = read_export(export_zip)

    assert any("no race marker" in note for note in read.notes)


def test_an_unzipped_folder_reads_the_same_as_the_zip(
    export_zip: Path, export_folder: Path
) -> None:
    from_zip, from_folder = read_export(export_zip), read_export(export_folder)

    assert [a.activity_id for a in from_folder.activities] == [
        a.activity_id for a in from_zip.activities
    ]
    assert from_folder.not_runs == from_zip.not_runs
    assert from_folder.unsupported == from_zip.unsupported


def test_a_filename_that_climbs_out_of_the_folder_is_not_read(tmp_path: Path) -> None:
    """`Filename` is text in a CSV, and a CSV is text anybody can edit."""
    root = tmp_path / "export"
    root.mkdir()
    (tmp_path / "outside.gpx").write_bytes(gpx_track())
    (root / "activities.csv").write_bytes(_csv([_row("1", "Run", "../outside.gpx")]))

    read = read_export(root)

    assert read.activities == []
    assert read.unreadable == ["1: ValueError"]


def test_something_that_is_not_an_export_is_refused_by_name(tmp_path: Path) -> None:
    stray = tmp_path / "stray.zip"
    with zipfile.ZipFile(stray, "w") as archive:
        archive.writestr("run.gpx", gpx_track())

    assert not is_strava_export(stray)
    assert not is_strava_export(tmp_path)
    with pytest.raises(NotAStravaExport):
        read_export(stray)


def test_the_cli_reads_an_export_zip_and_writes_nothing_unasked(export_zip: Path) -> None:
    from typer.testing import CliRunner

    from longrun.cli.main import app

    result = CliRunner().invoke(app, ["ingest-history", str(export_zip)])

    assert result.exit_code == 0, result.output
    assert "Strava export: 3 run(s) read of 8 activities" in result.output
    assert "not runs, never opened: Ride 1, Virtual Run 1" in result.output
    assert "--remote-rasters" in result.output  # the device-elevation warning
    assert "nothing written" in result.output


def test_an_index_without_the_columns_it_needs_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "export"
    root.mkdir()
    (root / "activities.csv").write_bytes(b"Activity ID,Activity Name\n1,Morning Run\n")

    with pytest.raises(NotAStravaExport, match="Activity Type"):
        read_export(root)
