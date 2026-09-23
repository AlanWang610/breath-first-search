# 0045 — A forecast hour is UTC, an ETA is local, and the conversion belongs to the data layer

Status: **accepted**, 2026-09-23.

> Numbered 0045 rather than 0037: `main` ends at 0036, and 0037–0044 are taken on four
> branches open in parallel. `ls docs/decisions/` on this branch shows neither.

## Context

Every plan this project has ever produced read the weather and the air quality at the
wrong hour.

**ADR 0008 opens with half of the reason.** *"A plan carries a **naive local** start
time."* `FrozenClock` takes one, `request.yaml` pins one, `PlanRequest.start_time` is a
bare `time`, and `core.pacing.model` propagates it to every entry of the ETA vector. An
ETA is a naive local wall clock, everywhere, deliberately.

**The other half is in `forecast.open_meteo_args`**, which sets `"timezone": "UTC"`. The
recorded cassettes confirm what comes back: `timezone: GMT`, `utc_offset_seconds: 0`, and
hours stamped `2026-09-12T00:00` through `2026-09-12T23:00`. `parse_open_meteo` reads
those with `datetime.fromisoformat`, which yields a naive datetime — naive **UTC**.
`air.air_args` does the identical thing with the identical result.

Two naive datetimes compare without complaint and mean different things. So
`SiteForecast.at(eta)` was matching a 17:30 local arrival against the row labelled 17:30
UTC. For `bay-urban` — San Francisco, 17:30 on 12 September, UTC−7 — that row is 10:30 in
the morning. Its expectation recorded `min_temp_c 17.7 / max_temp_c 17.827`, interpolated
between the cassette's 17:00 and 18:00 rows. The instant the runner actually starts is
`2026-09-13T00:30Z`, which is not in that cassette at all.

**NWS has the same bug and fails loudly instead of quietly**, which is why nobody saw
either. Its `validTime` is an ISO-8601 interval carrying an offset —
`2026-09-26T07:00:00-07:00/PT1H` — so `fromisoformat` returns a tz-**aware** datetime.
Measured: `nws tzinfo: UTC-07:00` against `open-meteo tzinfo: None`. Feeding an aware hour
to `at()` with a naive ETA raises `TypeError: can't compare offset-naive and offset-aware
datetimes`; `run_scorers` catches it at `registry.py:361` and records
`scorer failed: TypeError: …`.

Scope §7.4 makes NWS the **primary** provider and Open-Meteo the fallback. All seven golden
cassettes hold `open_meteo.forecast` and `open_meteo.air_quality` and not one
`nws.gridpoints`. **The primary path has never run end to end**, and its failure mode —
a caught exception three layers from its cause — is indistinguishable from a provider being
down. No test fed NWS-parsed points into `at()` either: `test_forecast._site_forecast`
labels itself `provider="nws"` and then builds its `HourlyPoint`s by hand from a naive
constant, so the stamps NWS actually produces never reached the comparison.

**`core/geo/solar.py` already had the shape of the answer.** `_index` converts at the point
of use — `shifted = [t - timedelta(hours=utc_offset_hours) if t.tzinfo is None else t …]` —
and `solar_positions`/`clear_sky` both route through it. The caller passes naive local times
and an offset; the module does the arithmetic once. `sun_exposure` and
`start_time_optimizer` both call `utc_offset_for` and hand the result to `solar_positions`
correctly, and then read the forecast with the raw local ETA in the same function. The
asymmetry was inside one call.

## Decision

**One convention, enforced at parse time. One conversion, in the data layer.**

**1. Every stored hour is naive UTC.** `HourlyPoint.time` and `AirHour.time` say so in
their field comments. `expand_intervals` normalizes an NWS `validTime` through
`as_naive_utc` before it is keyed, so `07:00−07:00` and `14:00Z` become the same stamp and
the two providers can no longer produce two kinds of datetime. `SiteForecast.at` and
`AirSite.at` normalize an aware argument rather than refusing it, so the `TypeError` cannot
return.

**2. `RouteForecast` and `RouteAirQuality` carry the offset, and their reading API takes a
naive local ETA.** `at_distance` and `for_segments` subtract; `at()` stays on UTC instants
because that is what the series holds. `route_offset(route, ctx)` resolves it once per
fetch from the route's first point and `ctx.clock.now()` — `FrozenClock(start_at)`, the
plan's own naive local start, so no wall clock enters `core/` (scope §3.3). It is the same
`solar.utc_offset_for(lat, lon, when, stated)` that ADR 0008 already governs, so the
forecast is read on the clock the shade is computed on, and `core.data.air` calls the same
function so weather and air quality cannot drift onto two clocks.

**The conversion is in the data layer rather than in the scorers, and that is the load-
bearing half of this decision.** The brief this work started from named two scorers.
There are **five**: `microclimate`, `heat_stress` and `air_quality` read the wrong hour,
and `sun_exposure` and `start_time_optimizer` read the wrong hour while correctly resolving
an offset for their solar half a few lines above. Three of five had already forgotten.
Asking the sixth to remember is not a fix; it is the same bug with a later date on it. Put
behind `RouteForecast`, no scorer needed a line changed and a new one cannot get it wrong.

**3. `utc_offset_hours` is required, and `None` means absence rather than zero.** Neither
model defaults it, so a forecast cannot be constructed without someone deciding how to read
its clock. `None` is legal and means the offset could not be resolved; every reader then
returns nothing and confidence falls to 0.

A default of `0.0` was the tempting alternative and is precisely the bug wearing a
plausible number: an assumed-UTC ETA read against UTC hours is arithmetically well-formed
and seven hours wrong, and nothing downstream could tell. Absence is not zero (scope §12).

The question "what if `ctx.utc_offset_hours` is `None`?" turns out to be the wrong
question, and the brief's framing of it is worth correcting: `ctx.utc_offset_hours` is the
**stated** offset, from `--utc-offset`, and it is `None` on most plans — `bay-urban`
deliberately leaves it unset so ADR 0008's lookup is exercised. That case is already
answered: `utc_offset_for` looks the zone up from the coordinate, falls back to longitude,
and reports which of the three it used. `route_offset` returns `None` only when the route
has no points at all. The decision above is about a failure to **resolve**, not a failure
to **state**.

**4. Which hour was read is reported.** `microclimate`'s route summary gains
`utc_offset_hours` and `utc_offset_source` — the same three-state report ADR 0008 put on
`sun_exposure` and `lighting`. The hour is what was wrong, and a reader comparing a
temperature against a weather site needs to know which clock it is on.

## The half this does **not** fix, and why

**`open_meteo_args` and `air_args` are unchanged, deliberately.** They ask for one *local
calendar day* of *UTC* hours. At UTC−7 that covers local 17:00 on the previous day to 17:00
today. **An evening ETA falls off the end**, and `bay-urban` — a 17:30 start chosen
precisely because late-afternoon sun casts real shadows — now has no forecast at all: its
microclimate and WBGT readings are `None` and its confidence is 0.019.

That is a **known coverage gap**, recorded here rather than papered over, and the reason it
is not closed is that the fix is unavailable:

* These four values *are* the cache key. Widening the window changes the `args_hash`, which
  changes the key, which orphans every committed cassette.
* `forecast.open_meteo_root` can re-record weather for a past day from
  `archive-api.open-meteo.com`. **`core.data.air` has no archive endpoint at all.** A
  past-dated air-quality cassette re-fetches as nothing and is gone permanently.
* Six of the seven golden routes pin a date that is already in the past.
  `bay-urban/README.md` states the rule plainly: *"a forecast for a past date can never be
  re-fetched. Do not regenerate the cassette."*

Losing one route's evening readings is recoverable — the route reports honestly that the
data was never fetched, and the next re-record can fix it. Losing every past-dated route's
air quality is not. So `window_gap_entry` produces a coverage line that distinguishes "the
fetch failed" from "the fetch succeeded and your hour was not in it", `microclimate` and
`heat_stress` both emit it, and `forecast.open_meteo_args` carries the argument in its own
docstring where the next person to widen it will read it first.

## Consequences

* **All seven golden expectations change, and `--update-golden` is correct here.** The
  recorded numbers were wrong, not merely different. The commit that carries them accounts
  for every route: one loses its readings, six are corrected to a different hour,
  `heat_stress` moves on all seven, `resupply_schedule`'s WBGT follows it on six and its
  scaled dry-gap thresholds move on the one route whose WBGT sits between §8.3's soft
  threshold and its hard floor.
* **`sun_exposure`'s clear-sky caveat had to be re-gated.** `_cloud_factors(None)` returns
  `(1.0, 1.0)`, so missing cloud silently becomes clear sky, and the caveat for that was
  gated on `forecast.answered`. Those were the same question until now and are not any
  more: a route can fetch every site and still have no cloud at the hours it runs.
* **`ScorerContext.utc_offset_hours`'s docstring said the opposite of the code.** "Hours to
  add to a naive plan time to get UTC" — every reader subtracts. Corrected in place.
* NWS still has no cassette, so the primary path is fixed but remains unexercised end to
  end. `TestTheTwoClocks` pins it at the unit level, which is the first time anything has.

## What would make us revisit

**Recording an NWS cassette for one golden.** It is the missing evidence, it is what would
turn scope §7.4's stated primary into a path the suite actually runs, and it needs a
`LONGRUN_NWS_USER_AGENT` and a date inside the gridpoint endpoint's window — so it must be
recorded on a *future*-dated route, which no current golden is.

**A region build (M3) that knows its own polygon** makes the offset a property of the
region, at which point `route_offset` becomes a build-time lookup and the per-fetch path is
the fallback. The three-state reporting should survive that move unchanged, exactly as ADR
0008 says.

**A cassette re-record for `bay-urban` from a live date** is the only thing that can close
the window gap, because it is the only moment at which the air-quality key may change
without losing data. If that is ever done, widen both requests at the same time — a day
either side, or the true local day in local hours — and this section is the note that says
so.
