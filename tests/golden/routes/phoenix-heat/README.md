# phoenix-heat

Van Buren Street, downtown Phoenix, eastward — 5.2 km of six-lane arterial with essentially
no canopy — at **midday on 15 July**. Scope §11's test region 2, and the route
[ADR 0004](../../../../docs/decisions/0004-wbgt-formulation.md) named as the trigger for
revisiting the WBGT formulation.

Where the other two goldens divide the labour between *arithmetically right* and *survives
real data*, this one answers a **decision**: the ADR chose a temperature-and-humidity-only
WBGT over Liljegren and said Phoenix in July was the case that would show whether that was
tolerable. It is:

| | |
|---|---|
| segments above the 30 °C hard floor | **97 of 97** |
| max WBGT | 31.06 °C at 36 °C / 30% RH |
| shaded fraction | **0.000** |
| mean sky view factor | 0.999 |
| peak irradiance | 978 W/m² |
| confidence on every heat measurement | 0.604 |

Read the last two rows together with the first. The flag detail says *"WBGT 31.0 C at
36 C / 30% RH, 100% sunlit — a humidity-only WBGT understates this"*, and it is right to:
978 W/m² of direct sun on an unshaded arterial is precisely the load the formula omits. The
plan flags the route **and** says its own number is a lower bound. That pairing is the
whole of what ADR 0004 decided.

15 July was a **moderate** July day here, not a peak one. The month's hottest hour was
46.3 °C at 16% RH — 36.6 °C WBGT — so this route clears the floor with the weather nearer
its median than its extreme, which is what "hard flags in ordinary conditions" means.

## What is real, and what is missing

| Layer | Source | State |
|---|---|---|
| DEM | USGS 3DEP 1/3 arc-second, tile `n34w113`, clipped | real, 329–338 m — 6 m of gain over 5.2 km |
| Ways, nodes, amenities, railways | OSM, Geofabrik arizona clipped to the metro bbox, `2026-09-10` | real, via `longrun build-region deploy/regions/phoenix.yaml` |
| Flowlines | USGS NHD HUC4 1506 (Middle Gila) | real |
| Buildings, canopy | Overture, Meta/WRI | **absent** — see below |
| Transit stops | — | **empty**: only BART is loaded, and it does not reach Arizona |

**No buildings, and the sheet says so** — *"shade measured from dem; buildings and canopy
unavailable"*. For this route that is a smaller loss than it looks: a six-lane arterial in a
city of two-storey strip development has almost no building shade to miss, and the measured
sky view factor of 0.999 is what a runner actually experiences there. It is still a gap,
and the coverage manifest carries it rather than the shaded fraction quietly absorbing it.

The transit row is the more instructive absence. `transit` and `bailouts` report against
the feeds a region build loaded, and Phoenix's build loaded none — so the answer is about
this database, not about Valley Metro, and the coverage manifest is where that distinction
has to survive.

## Why the route was drawn by the router

Unlike the other two, this route came out of `GraphHopperRouter` rather than being drawn by
hand. It is on the graph by construction, so `check_2 on_network` **passes** here where
`bay-urban`'s hand-interpolated waterfront fails it with 64 offenders — and all 97 segments
match a way, against `bay-urban`'s 62%. A golden that exercises heat should not also be
fighting its own geometry.

## Why the date is in the past

`request.yaml` pins **2026-07-15**, recorded on 2026-09-10 — 57 days later. A golden pins
an absolute date forever and this one was chosen for its weather, so the cassette comes
from Open-Meteo's **archive** rather than its forecast endpoint, which reaches about a
fortnight either side of today. `forecast.open_meteo_root` is what decides that, and it is
what makes a July golden recordable at all. `freeze-cassette`'s warning that "a forecast
for a past date can never be re-fetched" was a property of the endpoint, not of the weather.

Arizona does not observe daylight saving, so `utc_offset_hours: -7` is pinned rather than
looked up — and that is exactly the trap ADR 0008 recorded. Longitude gives −7 here *by
accident*, so a route relying on the rule of thumb would be right for the wrong reason and
would tell nobody when the rule broke somewhere else.
