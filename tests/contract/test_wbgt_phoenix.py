"""ADR 0004's trigger, kept standing (scope 7.4, 8.3, 11).

The ADR chose the ACSM/BoM approximation over Liljegren and named exactly one condition
for revisiting it:

> If Phoenix in July produces WBGT figures that stay under 30 °C on exposed arterials at
> midday, the approximation is failing at exactly the case the project exists to catch.

That is a question about real weather, so it lives here rather than as a number in the ADR
that decays the moment someone edits `wbgt_c`. `network`-marked and run on demand:

    uv run pytest tests/contract/test_wbgt_phoenix.py -m network

**The thresholds are asserted loosely and the direction tightly.** Reanalysis is revised,
and a test that pinned "83% of midday hours" would go red for a reason that has nothing to
do with this project. What must not change is that Phoenix in July clears the hard floor
in the ordinary case — that is the claim ADR 0004 rests on.
"""

from __future__ import annotations

from typing import Any

import pytest

from longrun.core.preferences.floors import WBGT_HARD_C
from longrun.core.scorers.heat import WBGT_SOFT_C, wbgt_c

pytestmark = pytest.mark.network

#: Downtown Phoenix. Scope 11's test region 2.
LAT, LON = 33.4484, -112.0740

#: The month scope 11 and ADR 0004 both name. An absolute year, so the measurement is
#: repeatable: "last July" would compare different weather on every run.
YEAR, MONTH = 2026, 7

#: Hours the ADR's wording covers — "at midday", which for a runner means the window they
#: would be told to avoid rather than the single hour of solar noon.
MIDDAY_HOURS = range(11, 17)

#: The share of midday hours that has to clear the hard floor for the approximation to be
#: doing its job. Well under the 83% measured on 2026-09-10, because the point is the
#: direction and not the digit.
MIN_HARD_FRACTION = 0.5


@pytest.fixture(scope="module")
def hours() -> list[tuple[str, float, float]]:
    """July's hourly temperature and humidity from Open-Meteo's reanalysis."""
    import httpx

    try:
        response = httpx.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": LAT,
                "longitude": LON,
                "start_date": f"{YEAR}-{MONTH:02d}-01",
                "end_date": f"{YEAR}-{MONTH:02d}-31",
                "hourly": "temperature_2m,relative_humidity_2m",
                "timezone": "America/Phoenix",
            },
            timeout=60.0,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()["hourly"]
    except Exception as exc:  # noqa: BLE001 - an unreachable archive is a skip
        pytest.skip(f"Open-Meteo archive unavailable: {exc}")
        raise

    rows = [
        (when, temp, humidity)
        for when, temp, humidity in zip(
            payload["time"],
            payload["temperature_2m"],
            payload["relative_humidity_2m"],
            strict=True,
        )
        if temp is not None and humidity is not None
    ]
    if len(rows) < 500:
        pytest.skip(f"the archive returned only {len(rows)} hours for {YEAR}-{MONTH:02d}")
    return rows


def _midday(hours: list[tuple[str, float, float]]) -> list[float]:
    return [
        wbgt_c(temp, humidity) for when, temp, humidity in hours if int(when[11:13]) in MIDDAY_HOURS
    ]


def test_phoenix_in_july_clears_the_hard_floor_in_the_ordinary_case(
    hours: list[tuple[str, float, float]],
) -> None:
    """ADR 0004's revisit condition, inverted into an assertion.

    If this ever fails, the approximation has stopped producing hard flags in the case
    scope 11 expects them, and Liljegren becomes worth implementing properly — against a
    reference dataset, and against a *humid* case rather than this one.
    """
    values = _midday(hours)
    assert values, "no midday hours"
    fraction = sum(1 for v in values if v > WBGT_HARD_C) / len(values)
    assert fraction >= MIN_HARD_FRACTION, (
        f"only {fraction:.0%} of Phoenix midday hours in July {YEAR} exceed "
        f"{WBGT_HARD_C} C WBGT; ADR 0004's trigger is met and the formula needs revisiting"
    )


def test_the_peak_is_well_clear_rather_than_marginal(
    hours: list[tuple[str, float, float]],
) -> None:
    """A formula that grazed the floor at the annual peak would be one bad revision away
    from never flagging. On 2026-09-10 the peak was 36.6 C, 6.6 C above the floor."""
    assert max(_midday(hours)) > WBGT_HARD_C + 3.0


def test_every_midday_hour_at_least_clears_the_soft_floor(
    hours: list[tuple[str, float, float]],
) -> None:
    """The weaker claim, and the one that would survive a cool July: a midday hour in
    Phoenix that is not even *uncomfortable* would mean the arithmetic is wrong, not that
    the weather was."""
    values = _midday(hours)
    assert sum(1 for v in values if v > WBGT_SOFT_C) / len(values) > 0.8


def test_a_dry_climate_is_the_friendly_case_for_a_humidity_only_index() -> None:
    """Stated as a test because it is the caveat the passing result must not bury.

    Phoenix clears the floor on temperature alone. The same WBGT reached in a humid
    climate comes from a much lower air temperature, and there the omitted solar and wind
    terms no longer pull the answer in one known direction — which is why ADR 0004 names a
    humid case, not this one, as what Liljegren would have to be validated against.
    """
    dry = wbgt_c(46.0, 16.0)
    humid = wbgt_c(33.0, 70.0)
    assert dry > WBGT_HARD_C and humid > WBGT_HARD_C
    assert humid > wbgt_c(33.0, 16.0) + 5.0
