"""The HTML sheet is self-contained, and stays that way (scope 9).

"No server required" is the property most easily lost by accident — one convenient CDN
link for a font or a charting library and the file silently stops working on a plane,
which is where a plan sheet is most likely to be read. So it is asserted directly rather
than trusted: no `src=`, no `href=` to anywhere, no `@import`, no `url(http`.

The rest is what a sheet has to survive: a plan where most scorers reported nothing, a
route with a name someone put an angle bracket in, and being rendered from `plan.json`
alone with the fixtures long gone.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import pytest

from longrun.core.export.sheet_html import render_html
from longrun.core.geo.dem import ElevationProfile
from longrun.core.models.coverage import CoverageEntry, CoverageManifest
from longrun.core.models.geometry import Route, RoutePoint, Segment
from longrun.core.models.measurement import (
    Flag,
    FlagKind,
    ScorerResult,
    SegmentMeasurement,
    Tier,
)
from longrun.core.models.plan import Plan
from longrun.core.models.request import PlanRequest
from longrun.core.models.verification import CheckResult, VerifyReport

START = datetime(2026, 9, 12, 7, 0)

#: Anything that would make the browser reach for the network.
EXTERNAL = re.compile(r"""(src\s*=|href\s*=|@import|url\(\s*['"]?http)""", re.I)


def _plan(name: str = "Test route", *, empty: bool = False) -> Plan:
    points = [
        RoutePoint(
            lat=37.7955 + i * 0.0002,
            lon=-122.40 + i * 0.0004,
            cum_dist_m=i * 50.0,
            ele_m=10.0 + i * 2.0,
        )
        for i in range(12)
    ]
    route = Route(id="t", name=name, points=points)
    segments = [
        Segment(
            id=f"s{i:05d}",
            index=i,
            start_idx=i * 4,
            end_idx=min(i * 4 + 4, 11),
            cum_start_m=i * 200.0,
            length_m=200.0,
        )
        for i in range(3)
    ]

    results: list[ScorerResult] = []
    if not empty:
        results.append(
            ScorerResult(
                name="heat_stress",
                measurements=[
                    SegmentMeasurement(segment_id=s.id, values={"wbgt_c": 24.0 + i})
                    for i, s in enumerate(segments)
                ]
                + [SegmentMeasurement(segment_id="route", values={"max_wbgt_c": 26.0})],
                flags=[
                    Flag(
                        scorer="heat_stress",
                        segment_id="s00002",
                        kind=FlagKind.SOFT,
                        tier=Tier.PHYSIOLOGICAL,
                        severity=0.4,
                        reason_code="wbgt_above_soft_threshold",
                        detail="WBGT 26.0 C",
                    )
                ],
            )
        )
        results.append(
            ScorerResult(
                name="sun_exposure",
                measurements=[
                    SegmentMeasurement(segment_id=s.id, values={"shaded_fraction": 0.2 * i})
                    for i, s in enumerate(segments)
                ]
                + [
                    SegmentMeasurement(
                        segment_id="route",
                        values={"shaded_fraction": 0.3, "surface": "dem and canopy"},
                    )
                ],
            )
        )

    coverage = CoverageManifest()
    coverage.record(CoverageEntry(source="3dep", kind="surface_model", checked=True))
    coverage.record(
        CoverageEntry(source="closures", kind="closures", checked=False, reason="no adapter (M4)")
    )

    return Plan(
        id="plan-1",
        request=PlanRequest(mode="repair", date=date(2026, 9, 12)),
        route=route,
        segments=segments,
        results=results,
        etas=[START + timedelta(seconds=i * 30) for i in range(12)],
        residual_flags=[f for r in results for f in r.flags],
        coverage=coverage,
        elevation=ElevationProfile(gain_m=22.0, loss_m=0.0, min_ele_m=10.0, max_ele_m=32.0),
        verify=VerifyReport(
            results=[
                CheckResult(number=1, name="valid_gpx", status="passed"),
                CheckResult(
                    number=6,
                    name="no_active_closures",
                    status="skipped",
                    detail="no closures adapter",
                ),
            ]
        ),
    )


# --- self-containment -------------------------------------------------------


def test_the_sheet_reaches_for_nothing() -> None:
    """The property "no server required" actually means. Asserted, not assumed."""
    html = render_html(_plan())
    found = EXTERNAL.search(html)
    assert found is None, (
        f"external reference: {html[max(0, found.start() - 60) : found.end() + 60]}"
    )


def test_the_sheet_is_one_document_with_its_own_styles() -> None:
    html = render_html(_plan())
    assert html.startswith("<!doctype html>")
    assert "<style>" in html
    assert "prefers-color-scheme: dark" in html, "the second theme is not optional"


def test_the_sheet_is_small_enough_to_send() -> None:
    """A sheet nobody can email is a sheet nobody reads."""
    assert len(render_html(_plan()).encode("utf-8")) < 400_000


# --- content ----------------------------------------------------------------


def test_segment_ids_and_reason_codes_reach_the_page() -> None:
    """Scope 9: "flagged segments with IDs". The id is how a user names one back to us."""
    html = render_html(_plan())
    assert "s00002" in html
    assert "wbgt_above_soft_threshold" in html


def test_what_was_not_checked_is_rendered_before_what_was() -> None:
    """Scope 3.6 in the place a user actually looks."""
    html = render_html(_plan())
    assert "NOT CHECKED" in html
    assert "no adapter (M4)" in html
    assert html.index("NOT CHECKED") < html.index(">checked<")


def test_a_skipped_check_is_not_shown_as_a_pass() -> None:
    html = render_html(_plan())
    assert "skipped" in html
    assert "1 passed, 0 failed, 1 skipped of 2" in html


def test_attribution_is_rendered_from_the_sources_actually_used() -> None:
    assert "3DEP" in render_html(_plan()) or "public domain" in render_html(_plan())


def test_the_elevation_profile_is_drawn_from_stored_samples() -> None:
    """`plan.json` carries DEM elevations, so a sheet renders without the DEM."""
    html = render_html(_plan())
    assert "Elevation profile" in html
    assert "polyline" in html


# --- robustness -------------------------------------------------------------


def test_a_route_name_cannot_inject_markup() -> None:
    """Route names come from user-supplied GPX files."""
    html = render_html(_plan(name='<script>alert("x")</script>'))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_plan_where_nothing_ran_still_renders() -> None:
    """The M1 case, and the one a broken fixture directory produces."""
    html = render_html(_plan(empty=True))
    assert "<!doctype html>" in html
    assert "No segment was flagged" in html


def test_a_plan_with_no_elevation_still_renders() -> None:
    plan = _plan()
    stripped = plan.model_copy(update={"elevation": None})
    assert "<!doctype html>" in render_html(stripped)


@pytest.mark.parametrize("tier", list(Tier))
def test_every_tier_has_a_colour(tier: Tier) -> None:
    """A tier with no colour would draw as the default and read as unflagged."""
    from longrun.core.export.sheet_html import TIER_COLOUR

    assert tier in TIER_COLOUR
