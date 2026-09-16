# 0024 — Run history is graded on terrain, over the stride a plan grades on

Status: accepted (2026-09-15)

## Context

Scope 6.2 derives "median speed per 2% grade bin" from a runner's history and says nothing
about *which elevation* the grade is measured against. M5.10 built the derivation against
hand-built activities, where the question never arises: the elevation is whatever the test
wrote. It used the elevation in the file, point to point.

The first real history - a Strava bulk export of a few hundred runs, mostly `.fit.gz` from
a watch - produced a curve that was wrong in a way no test could have shown. Pace barely
changed with grade: a +12% climb read as **0.92 of flat speed**, where the population curve
predicts 0.53 and no runner holds 92% of their flat pace up a 12% hill. Three measurements
located the cause, and none of it was the arithmetic:

* **The device's elevation is noise at the scale it was being read.** Across the watch
  files, the elevation range was 2.76x what Strava reports for the same activities and the
  point-to-point climb 8.2x, while the lowest elevation agreed to within a few metres - not
  a unit error, jitter. The GPX files in the same archive agreed with Strava (1.00x, 1.24x).
* **Point to point, a watch's strides are a few metres long**, because it records every
  1-7 s. Divide half a metre of altimeter jitter by four metres of travel and a flat street
  is a 12% hill: 8% of all samples landed in bins steeper than 20%, and bins as absurd as
  +1110% had samples in them. Noise spread across bins flattens the curve, because every
  bin ends up holding mostly flat-ground speeds.
* **A plan does not grade on the device at all.** Scope 7.1, and `core/geo/dem.py` in its
  first sentence: "Elevation comes from the terrain model, never from the GPX." A plan
  grades its route on 3DEP over a centred 50 m window and looks the curve up by *that*. A
  curve measured on anything else is applied to a different quantity than it measured.

Re-graded on 3DEP over 50 m, the same archive gave a curve a runner would recognise: +6% at
**0.82** of flat speed and +12% at **0.65**, falling steadily through every bin between. On
the device's own elevation, at the same 50 m stride, those bins read 0.97 and 0.92. The
stride alone does not rescue device elevation; the terrain does.

## Decision

1. **Strides are `DEFAULT_SMOOTH_M` long** - the constant `dem.grades` uses - rather than
   point to point, so a bin means the same grade where it is measured and where it is
   applied. A stop ends a stride instead of diluting one.
2. **`longrun ingest-history --remote-rasters` grades on 3DEP**, sampled at the points that
   survive the privacy trim and nowhere else. It is opt-in, as it is for `repair` and `plan`.
3. **The curve records which elevation its bins came from** (`PacingCurves.grade_elevation`),
   and a plan whose curve was binned on device elevation says so on its sheet: "climbs may
   be paced faster than they will be run". A caveat that lives only in the ingest output
   would be gone by the first plan.
4. **A missing elevation is an unknown grade.** The reader used to subtract a known elevation
   from an absent one as though it were zero.

## Consequences

**Something leaves the machine that did not before.** A terrain lookup reads COG byte ranges
from USGS's public bucket, so which one-degree tiles a history covers, and which blocks of
them, reach an access log. Only the trimmed range is sampled, so the ends of runs are never
asked about, but the blocks are kilometres wide and a history's neighbourhood is in them.
That is the trade `--remote-rasters` asks a person to make, and why it is not the default.

**A run outside 3DEP contributes no grade samples.** Outside the US, on a tile edge the
middle-of-run tile does not cover, or with no terrain under it, its strides have no grade
and are left out of every bin rather than filed as flat.

**Bridges and tunnels are graded on the ground beneath them.** A DEM has the river under a
bridge, so a run across one reads as a descent and a climb that never happened. Unmeasured:
the stride and the median absorb short ones, and nothing here claims they absorb long ones.

**The downhill half of the population curve looked optimistic.** On terrain, descents on the
first real history were run at or a little below flat speed - 0.98 at -6%, 0.92 at -12% -
where Minetti predicts up to 25% faster.
One runner is not a population, and this changes nothing in `population.py` - but it is the
first evidence either way, and it is why a measured curve matters more on descents than the
default suggests.

## What would change this

A device whose altimeter agrees with terrain over a 50 m stride - measurable the same way,
against the same archive - would make `device` bins trustworthy and the caveat unnecessary
for that device. A terrain model with structures in it would retire the bridge caveat.
