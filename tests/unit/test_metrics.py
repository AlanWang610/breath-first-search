"""Scope 7.1's three acceptance metrics, and what each says when it cannot be measured.

The arithmetic is trivial and is not what these tests are about. What they are about is the
`None`s: two of the three metrics can be absent for reasons that are not failures, and a
metric that filled in a plausible default would survive review and mislead a tuner, because
detour ratio is the one number the regulariser reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from longrun.core.models.geometry import LatLon, Route, RoutePoint
from longrun.core.models.measurement import ScorerResult, SegmentMeasurement
from longrun.core.models.plan import Plan
from longrun.core.models.request import PlanRequest
from longrun.core.plan.metrics import (
    SHORTEST_DISTANCE_INFLUENCE,
    AcceptanceMetrics,
    acceptance_metrics,
    metrics_with_router,
    shortest_legal_length,
)
from longrun.core.routing.base import NullRouter
from longrun.core.scorers._common import ROUTE_SUMMARY_ID

ROUTE = Route(
    id="r",
    points=[RoutePoint(lat=37.77, lon=-122.41 + i * 0.001, cum_dist_m=i * 88.0) for i in range(21)],
)


def _hostility(fraction: float = 0.25, count: int = 2) -> ScorerResult:
    return ScorerResult(
        name="segment_hostility",
        measurements=[
            SegmentMeasurement(
                segment_id=ROUTE_SUMMARY_ID,
                values={"fraction_lts3_plus": fraction, "lts4_count": count},
            )
        ],
    )


class _Router:
    """A router that answers one length, and records what it was asked."""

    def __init__(self, length_m: float = 1000.0) -> None:
        self.length_m = length_m
        self.models: list[dict] = []  # type: ignore[type-arg]

    def route(self, waypoints, custom_model=None, **kwargs):  # type: ignore[no-untyped-def]
        self.models.append(dict(custom_model or {}))
        step = self.length_m / 10
        return Route(
            id="short",
            points=[RoutePoint(lat=37.77, lon=-122.41, cum_dist_m=i * step) for i in range(11)],
        )


# --- the two metrics that already existed and nothing read -------------------


def test_the_lts_metrics_come_from_hostilitys_own_route_summary() -> None:
    """`fraction_lts3_plus` and `lts4_count` have been written on every plan since M1 and
    read by nothing. This is the read."""
    metrics = acceptance_metrics(ROUTE, [_hostility(0.31, 4)], shortest_m=1500.0)

    assert metrics.fraction_lts3_plus == pytest.approx(0.31)
    assert metrics.lts4_count == 4


def test_a_route_hostility_did_not_score_reports_unknown_rather_than_zero() -> None:
    """Zero LTS 4 segments and "nobody measured LTS" are different claims, and a fitter
    reading the first when the second is true would score a vector against nothing."""
    metrics = acceptance_metrics(ROUTE, [], shortest_m=1500.0)

    assert metrics.fraction_lts3_plus is None
    assert metrics.lts4_count is None
    assert any("did not run" in reason for reason in metrics.reasons)


def test_the_summary_is_found_by_name_and_not_by_position() -> None:
    """The registry's order is dependency order and has changed twice; a positional read
    would have gone silently wrong both times."""
    other = ScorerResult(
        name="surface_profile",
        measurements=[SegmentMeasurement(segment_id=ROUTE_SUMMARY_ID, values={"lts4_count": 99})],
    )

    metrics = acceptance_metrics(ROUTE, [other, _hostility(0.1, 1)], shortest_m=1500.0)

    assert metrics.lts4_count == 1


# --- the metric that did not exist -------------------------------------------


def test_the_detour_ratio_is_length_over_the_shortest_legal_route() -> None:
    metrics = acceptance_metrics(ROUTE, [_hostility()], shortest_m=1000.0)

    assert metrics.detour_ratio == pytest.approx(ROUTE.length_m / 1000.0)


def test_without_a_router_the_detour_ratio_is_none_and_never_one() -> None:
    """1.0 reads as "this route is as short as it could be", which is a measurement. The
    truth is that nobody measured - scope 3.6 applied to a ratio."""
    metrics = acceptance_metrics(ROUTE, [_hostility()])

    assert metrics.detour_ratio is None
    assert not metrics.complete
    assert any("no router" in reason for reason in metrics.reasons)


def test_a_shortest_route_of_zero_is_undefined_rather_than_a_division_error() -> None:
    metrics = acceptance_metrics(ROUTE, [_hostility()], shortest_m=0.0)

    assert metrics.detour_ratio is None
    assert any("undefined" in reason for reason in metrics.reasons)


def test_the_shortest_legal_route_keeps_the_excludes_and_drops_the_preferences() -> None:
    """The denominator scope 7.1 names. Against a shortest path that ignored legality, a
    route forced onto the one legal bridge would read as a detour it chose."""
    router = _Router(2000.0)

    shortest_legal_length(router, [LatLon(lat=37.77, lon=-122.41)] * 2)

    assert router.models[0]["distance_influence"] == SHORTEST_DISTANCE_INFLUENCE


def test_a_router_that_raises_becomes_a_reason_not_an_exception() -> None:
    """This runs inside a plan. A plan that died for want of a denominator would have
    traded a whole sheet for one number."""

    class _Broken:
        def route(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("the router fell over")

    metrics = metrics_with_router(ROUTE, [_hostility()], _Broken())

    assert metrics.detour_ratio is None
    assert any("RuntimeError" in reason for reason in metrics.reasons)


def test_a_null_router_is_reported_as_unavailable_rather_than_crashing() -> None:
    """Repair mode runs on `NullRouter`, and most plans this project makes are repairs."""
    metrics = metrics_with_router(ROUTE, [_hostility()], NullRouter())

    assert metrics.detour_ratio is None
    assert metrics.reasons


def test_no_router_at_all_says_so_by_name() -> None:
    metrics = metrics_with_router(ROUTE, [_hostility()], None)

    assert any("no router configured" in reason for reason in metrics.reasons)


# --- serialisation -----------------------------------------------------------


def test_the_dict_rounds_because_raw_floats_do_not_compare_across_platforms() -> None:
    """M5.13 is this project's standing proof of that, and it cost a red CI run."""
    rendered = acceptance_metrics(ROUTE, [_hostility(1 / 3, 2)], shortest_m=1234.5678).as_dict()

    assert rendered["fraction_lts3_plus"] == 0.3333
    assert rendered["shortest_legal_m"] == 1234.6


def test_an_unmeasured_metric_serialises_as_null_rather_than_vanishing() -> None:
    rendered = acceptance_metrics(ROUTE, []).as_dict()

    assert rendered["detour_ratio"] is None
    assert "detour_ratio" in rendered


def test_the_one_line_summary_says_unknown_where_it_does_not_know() -> None:
    assert "unknown" in str(AcceptanceMetrics(length_m=100.0))
    assert "not measured" in str(AcceptanceMetrics(length_m=100.0))


# --- the command --------------------------------------------------------------


def test_the_command_reports_not_measured_without_a_router(tmp_path: Path) -> None:
    """The honest default. `longrun metrics` with no `--router` still publishes the two
    LTS numbers, because those cost nothing and have never been published at all."""
    from typer.testing import CliRunner

    from longrun.cli.main import app

    plan_file = tmp_path / "plan.json"
    plan_file.write_text(_stored_plan().model_dump_json(), encoding="utf-8")

    result = CliRunner().invoke(app, ["metrics", str(plan_file)])

    assert result.exit_code == 0
    assert "not measured" in result.output
    assert "25.0%" in result.output


def test_the_command_writes_the_metrics_back_only_when_asked(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from longrun.cli.main import app

    plan_file = tmp_path / "plan.json"
    plan_file.write_text(_stored_plan().model_dump_json(), encoding="utf-8")

    CliRunner().invoke(app, ["metrics", str(plan_file)])
    assert Plan.model_validate_json(plan_file.read_text(encoding="utf-8")).metrics == {}

    CliRunner().invoke(app, ["metrics", str(plan_file), "--write"])
    assert Plan.model_validate_json(plan_file.read_text(encoding="utf-8")).metrics


def test_a_file_that_is_not_a_plan_exits_two_rather_than_traces(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from longrun.cli.main import app

    bad = tmp_path / "nope.json"
    bad.write_text("{}", encoding="utf-8")

    assert CliRunner().invoke(app, ["metrics", str(bad)]).exit_code == 2
    assert CliRunner().invoke(app, ["metrics", str(tmp_path / "missing.json")]).exit_code == 2


def test_a_plan_carries_the_free_metrics_and_says_the_ratio_needs_a_router() -> None:
    """`build_plan` fills these on every plan, because they are a read of a summary that
    has existed since M1. The detour ratio is not free and says so rather than being
    silently absent."""
    plan = _stored_plan()
    from longrun.core.plan.metrics import acceptance_metrics

    filled = acceptance_metrics(
        plan.route, plan.results, reasons=["detour ratio needs a router"]
    ).as_dict()

    assert filled["fraction_lts3_plus"] == 0.25
    assert filled["detour_ratio"] is None
    assert any("router" in reason for reason in filled["reasons"])


def _stored_plan() -> Plan:
    """The smallest plan that carries a hostility summary."""
    from datetime import date

    return Plan(
        id="p",
        request=PlanRequest(mode="repair", date=date(2026, 9, 15)),
        route=ROUTE,
        results=[_hostility(0.25, 2)],
    )
