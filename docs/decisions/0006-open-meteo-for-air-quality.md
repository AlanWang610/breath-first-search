# 0006 — Air quality from Open-Meteo, not AirNow

Status: accepted, 2026-09-10.

## Context

Scope §7.4 names AirNow (EPA) and PurpleAir as the sources for `air_quality`. Both need API
keys. `.env.example` ships `LONGRUN_AIRNOW_API_KEY=` and `LONGRUN_PURPLEAIR_API_KEY=`
blank, and §14 records PurpleAir as "API key + terms; per-user key; not redistributed".

Written against either, `air_quality` would report `unavailable` on every plan this project
can currently produce. A scorer that can only ever say "not checked" is honest and useless.

## Decision

**Use Open-Meteo's air-quality endpoint.** No key, US AQI and PM2.5 and PM10 hourly,
published on a grid alongside the weather forecast that `core/data/forecast.py` already
reads, through the same `cache.fetch` door and the same cassette mechanism.

**And say so on every plan.** The coverage entry reads:

> *N of M sites, modelled (CAMS) rather than measured; AirNow needs a key this install does
> not have*

That sentence is the point of this ADR being written rather than the substitution being
made quietly. AirNow reports what a monitor measured. Open-Meteo publishes a model, and a
model over a street canyon on a smoky afternoon is not a measurement of that canyon. A
runner deciding whether to go out deserves to know which of the two they are reading, and
the difference is not visible in the number.

## Consequences

* `open_meteo` gains a §14 attribution entry (CC-BY 4.0). It had none, despite already
  being named in §7.4 as the microclimate fallback — an omission in the scope, not a
  statement that nothing is owed.
* `air_quality` produces **no flags**. §8.3 has no air-quality row, so there is no threshold
  to test against, and inventing one would be attaching a sign to a measurement — which
  §3.2 forbids a scorer to do. It measures and reports.
* §7.4's signature is `air_quality(gpx, **date**)`, unlike every other environment tool,
  which take `etas`. This module resolves that against the scope's own §3 "time-aware
  everything" principle and reads the hourly series **at the ETA** — a route finishing at
  dusk meets a different AQI from one finishing at noon.
* AQI is read from the nearest hour rather than interpolated. It is a banded index, and
  the average of two bands is in neither.

## What would make us revisit

An AirNow key. The endpoint is free and the signup is a form; if it appears in `.env`, an
AirNow client belongs beside this one with AirNow preferred and Open-Meteo as the fallback
— exactly the shape `forecast.py` already uses for NWS and Open-Meteo, and the reason that
client's cache keys are provider-scoped.

PurpleAir is a different proposition: §14 records that its data is not redistributable
under a per-user key, so it cannot be recorded into a committed cassette, and a golden
route could never replay it.
