# 0028 — The FIT course encoder is hand-written, with no new dependency

Status: **accepted**, M10, 2026-09-18.

## Context

Scope §10.1 names `longrun export plan.json --fit`, and `cli/__init__.py` has advertised it
since the first commit. `fitdecode` has been in the `history` extra since M5 and is a
**decoder**; nothing in the Python ecosystem that this project already carries can write one.

The M8–M14 sketch proposed adding `fit-tool`. Checked before deciding: `fit-tool` 0.9.16 is
BSD-3-Clause, `requires_python >=3.9`, has zero runtime dependencies (its openpyxl/jinja2
deps are `gen`-extra only) and ships a `write_course_example.py`. It is also a stale 0.x that
has not been updated in some years and has known bugs. Garmin's own Python SDK **cannot
create** FIT files at all — only read them — so the official route does not exist.

## Decision

Write the encoder, in `core/export/fit.py`. No new dependency, no `export` extra.

## Consequences

**A FIT course is a closed subset of the format.** One `file_id`, one `course`, one `lap`,
N `record`, M `course_point`, a 14-byte header and two CRCs. That is `struct.pack` plus a
16-entry CRC table, in about 200 lines.

**The authoritative constants are already installed, and are asserted rather than
transcribed.** `fitdecode` ships the FIT profile, is synced by CI (`ci.yml` already passes
`--extra history`), and is already in mypy's `ignore_missing_imports`. Every number in the
module was read out of `fitdecode.profile` and three tests hold it there:

* `FIT_EPOCH == fitdecode.FIT_UTC_REFERENCE` — the constant most likely to be silently
  wrong, because a course off by 19 years still decodes, still validates, and puts every
  point in 2007;
* every value in `COURSE_POINT_TYPE` is a member of
  `FIELD_TYPES["course_point"].enum`, which catches a transposed digit that would otherwise
  put a water stop on a summit;
* our `crc16` equals `fitdecode.utils.compute_crc` byte for byte, checked with no file
  involved so a CRC bug is distinguishable from a layout bug.

**The round trip is what makes this safe rather than brave.** `fitdecode` validates both
CRCs on read (`CrcCheck.RAISE`), so decoding what we wrote is a real check of the encoder.
Measured on `bay-urban`'s 96 waypoints: 6 kB, 126 `record` frames and 96 `course_point`
frames, `file_id.type == "course"`.

**`core/` stays importable on a bare `uv sync` by construction.** There is no optional import
to guard, no `ast`-walking test needed to enforce that the guard stays, no `--extra export`
for CI to forget, and no `uv.lock` regeneration. That is the strongest single argument for
this route and it was not in the sketch.

**Nothing degrades in FIT.** All eight `WaypointKind` members have a native course-point
member — `water`, `food`, `danger`, `mile_marker`, `meeting_spot`, `toilet`, `obstacle`,
`transport`. The degradation rule is a **TCX** rule only, which is why TCX was written first:
both of its traps get designed against the format that forces them, rather than papered over
by FIT's richer enum. TCX's three, all of which fail silently on a device: `CoursePoint/Name`
is `Token_t` maxLength 10 and a longer value is *rejected*, not truncated; `PointType` is a
closed 16-member enumeration; and `CoursePoint_t` is an XSD **sequence**, so child order is
part of validity and `Time` and `Position` are both required.

**The risk this accepts** is that the FIT profile changes under us and nothing tells us. It
is bounded: the three assertions above fail loudly if `fitdecode`'s profile moves, and a
course is a stable corner of a format Garmin has versioned conservatively for fifteen years.

**Two things this project has already paid for** make the alternative worse rather than
merely different: `mcp>=1.2` admitted a breaking 2.x that the code could not use, and
fitdecode 0.11.0 needed a `FitReader` subclass to work around an upstream crash. A stale 0.x
underneath a file somebody follows in the dark is a third.
