"""Verification, plan sheet, attribution and scratchpad (scope 3.6, 7.9, 9, 14).

The recurring theme: a missing input must never read as a pass. Check 6 skipping because
no closure adapter exists, and a coverage section that leads with what was *not* checked,
are the same idea applied twice.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from longrun.core.export import attribution
from longrun.core.export.sheet_md import render_markdown
from longrun.core.geo.dem import ElevationProfile, SustainedRun
from longrun.core.models.coverage import CoverageEntry, CoverageManifest
from longrun.core.models.geometry import Route, RoutePoint
from longrun.core.models.measurement import Flag, FlagKind, ScorerResult, Tier
from longrun.core.models.plan import Plan, TradeOff
from longrun.core.models.request import LockedRange, PlanRequest, TimeConstraints
from longrun.core.plan.scratchpad import Scratchpad
from longrun.core.verify.runner import gpx_verify

START = datetime(2026, 3, 15, 7, 30)


def _route(n: int = 21, step_deg: float = 0.0005) -> Route:
    """Points ~55 m apart, comfortably inside the 100 m gap check."""
    return Route(
        id="r",
        points=[
            RoutePoint(lat=37.77 + i * step_deg, lon=-122.4, cum_dist_m=i * 55.6) for i in range(n)
        ],
        name="Test route",
    )


def _request(**kwargs: object) -> PlanRequest:
    return PlanRequest(mode="repair", date=date(2026, 3, 15), **kwargs)  # type: ignore[arg-type]


def _flag(scorer: str, segment_id: str = "s00000", kind: FlagKind = FlagKind.HARD) -> Flag:
    return Flag(
        scorer=scorer,
        segment_id=segment_id,
        kind=kind,
        tier=Tier.SAFETY,
        severity=0.9,
        reason_code="rc",
    )


# --- verify: skips are not passes -------------------------------------------


def test_a_bare_route_skips_rather_than_passes_what_it_cannot_check() -> None:
    report = gpx_verify(_route(), _request())
    skipped = {c.number for c in report.skipped}
    assert {2, 4, 5, 6, 8, 10} <= skipped
    assert not any(c.status == "passed" for c in report.results if c.number in skipped)


def test_summary_counts_all_three_states() -> None:
    summary = gpx_verify(_route(), _request()).summary()
    assert "passed" in summary and "failed" in summary and "skipped" in summary


def test_skips_do_not_block() -> None:
    """A partially-built plan is incomplete, not invalid."""
    assert gpx_verify(_route(), _request()).passed is True


def test_a_gap_fails_check_three_and_names_the_points() -> None:
    route = Route(
        id="g",
        points=[
            RoutePoint(lat=37.77, lon=-122.4, cum_dist_m=0.0),
            RoutePoint(lat=37.7727, lon=-122.4, cum_dist_m=300.0),
        ],
    )
    report = gpx_verify(route, _request())
    assert report.passed is False
    failure = next(c for c in report.results if c.number == 3)
    assert failure.status == "failed"
    assert "gap between points 0 and 1" in failure.offenders[0]


def test_elevation_spike_fails_check_four() -> None:
    elevations: list[float | None] = [100.0] * 10
    elevations[5] = 900.0
    report = gpx_verify(_route(n=10), _request(), elevations=elevations)
    assert next(c for c in report.results if c.number == 4).status == "failed"


def test_clean_elevation_passes_check_four() -> None:
    report = gpx_verify(_route(n=10), _request(), elevations=[float(i) for i in range(10)])
    assert next(c for c in report.results if c.number == 4).status == "passed"


def test_distance_tolerance_is_checked_when_a_target_was_given() -> None:
    route = _route()
    ok = gpx_verify(route, _request(target_distance_km=route.length_m / 1000))
    assert next(c for c in ok.results if c.number == 7).status == "passed"
    bad = gpx_verify(route, _request(target_distance_km=50.0))
    assert next(c for c in bad.results if c.number == 7).status == "failed"


def test_arrive_by_violation_is_a_hard_failure() -> None:
    etas = [START + timedelta(minutes=i * 5) for i in range(21)]
    request = _request(time_constraints=TimeConstraints(arrive_by=datetime(2026, 3, 15, 8, 0)))
    report = gpx_verify(_route(), request, etas=etas)
    check = next(c for c in report.results if c.number == 9)
    assert check.status == "failed"
    assert "after arrive-by" in check.offenders[0]


def test_time_budget_violation_is_reported() -> None:
    etas = [START + timedelta(minutes=i * 10) for i in range(21)]
    request = _request(time_constraints=TimeConstraints(total_budget=timedelta(hours=1)))
    check = next(c for c in gpx_verify(_route(), request, etas=etas).results if c.number == 9)
    assert check.status == "failed"
    assert "time budget" in check.offenders[0]


def test_a_scorer_that_ran_and_found_nothing_passes_its_check() -> None:
    """Distinct from the scorer never running, which skips."""
    results = [ScorerResult(name="legality", flags=[])]
    results[0].coverage.append(CoverageEntry(source="osm", kind="legality", checked=True))
    check = next(
        c for c in gpx_verify(_route(), _request(), results=results).results if c.number == 5
    )
    assert check.status == "passed"


def test_a_scorer_with_a_hard_flag_fails_its_check() -> None:
    results = [ScorerResult(name="legality", flags=[_flag("legality")])]
    check = next(
        c for c in gpx_verify(_route(), _request(), results=results).results if c.number == 5
    )
    assert check.status == "failed"
    assert check.offenders == ["s00000"]


def test_an_unavailable_scorer_skips_rather_than_passes() -> None:
    """No closure adapter must not read as "no closures on this route"."""
    unavailable = ScorerResult(name="closures")
    unavailable.coverage.append(
        CoverageEntry(source="closures", kind="closures", checked=False, reason="no adapter")
    )
    check = next(
        c for c in gpx_verify(_route(), _request(), results=[unavailable]).results if c.number == 6
    )
    assert check.status == "skipped"


def test_offending_segments_are_collected_for_rerouting() -> None:
    """Scope 8.1 step 9 sends failures back with the failing segment."""
    results = [ScorerResult(name="legality", flags=[_flag("legality", "s00003")])]
    assert "s00003" in gpx_verify(_route(), _request(), results=results).offending_segments()


# --- plan sheet -------------------------------------------------------------


def _plan(**kwargs: object) -> Plan:
    coverage = CoverageManifest()
    coverage.record(
        CoverageEntry(source="osm", kind="hostility", checked=True, vintage="2026-09-04")
    )
    coverage.record(
        CoverageEntry(
            source="WZDx",
            kind="closures",
            checked=False,
            jurisdiction="0600000US",
            reason="no adapter registered",
        )
    )
    defaults: dict[str, object] = {
        "id": "p1",
        "request": _request(),
        "route": _route(),
        "coverage": coverage,
    }
    defaults.update(kwargs)
    return Plan(**defaults)  # type: ignore[arg-type]


def test_sheet_leads_with_what_was_not_checked() -> None:
    """Scope 3.6: the unchecked list is what keeps the rest of the sheet honest."""
    sheet = render_markdown(_plan())
    assert "Not checked" in sheet
    assert "no adapter registered" in sheet
    assert sheet.index("Not checked") < sheet.index("**Checked")


def test_sheet_names_every_unchecked_source() -> None:
    sheet = render_markdown(_plan())
    assert "WZDx" in sheet
    assert "0600000US" in sheet


def test_sheet_reports_worst_segments_with_machine_stable_codes() -> None:
    results = [
        ScorerResult(
            name="segment_hostility",
            flags=[
                Flag(
                    scorer="segment_hostility",
                    segment_id="s00007",
                    kind=FlagKind.HARD,
                    tier=Tier.SAFETY,
                    severity=0.9,
                    reason_code="lts_4",
                    detail="6-lane arterial, no sidewalk",
                )
            ],
        )
    ]
    sheet = render_markdown(_plan(results=results))
    assert "s00007" in sheet
    assert "lts_4" in sheet
    assert "6-lane arterial" in sheet


def test_sheet_says_so_when_nothing_was_flagged() -> None:
    """An absent section is indistinguishable from a route with no problems."""
    sheet = render_markdown(_plan(results=[ScorerResult(name="legality")]))
    assert "No segment was flagged" in sheet


def test_sheet_says_so_when_no_scorer_ran() -> None:
    assert "No scorers were run" in render_markdown(_plan())


def test_sheet_carries_pacing_caveats() -> None:
    """Scope 12: extrapolation is labelled a guess, on the sheet, not just in the model."""
    sheet = render_markdown(_plan(), pacing_caveats=["projected times are a guess"])
    assert "projected times are a guess" in sheet


def test_sheet_surfaces_trade_offs_left_to_the_user() -> None:
    trade = TradeOff(
        segment_id="s00014",
        option_a="A",
        option_b="B",
        comparison="A adds 0.8 km; B lacks sidewalk at mile 41",
    )
    sheet = render_markdown(_plan(trade_offs=[trade]))
    assert "Choices left to you" in sheet
    assert "lacks sidewalk at mile 41" in sheet


def test_sheet_includes_elevation_when_available() -> None:
    profile = ElevationProfile(
        gain_m=250.0,
        loss_m=180.0,
        min_ele_m=5.0,
        max_ele_m=180.0,
        longest_climb=SustainedRun(start_m=0.0, end_m=1500.0, gain_m=90.0, mean_grade_pct=6.0),
    )
    sheet = render_markdown(_plan(), elevation=profile)
    assert "250 m" in sheet
    assert "Longest climb" in sheet


def test_sheet_admits_when_elevation_is_absent() -> None:
    assert "No elevation data" in render_markdown(_plan())


def test_sheet_reports_verification_including_skips() -> None:
    sheet = render_markdown(_plan(), verify=gpx_verify(_route(), _request()))
    assert "Verification" in sheet
    assert "skipped" in sheet


def test_sheet_is_valid_utf8_markdown(tmp_path: Path) -> None:
    """It is written to disk and read by other tools; encoding must not surprise."""
    path = tmp_path / "sheet.md"
    path.write_text(render_markdown(_plan()), encoding="utf-8")
    assert path.read_text(encoding="utf-8").startswith("# Plan p1")


# --- attribution (scope 14) -------------------------------------------------


def test_osm_attribution_is_rendered_with_share_alike_notice() -> None:
    text = attribution.render(["osm"])
    assert "OpenStreetMap contributors" in text
    assert "ODbL" in text


def test_public_domain_sources_need_no_attribution_text() -> None:
    assert "public domain" in attribution.render(["3dep"])


def test_sources_are_deduplicated() -> None:
    assert attribution.render(["osm", "osm"]).count("OpenStreetMap") == 1


def test_qualified_source_names_resolve() -> None:
    assert attribution.licence_for("osm_ways") is not None


def test_every_layer_a_scorer_can_read_has_a_licence() -> None:
    """Scope 14 is a licence obligation, and a scorer reports the *layer* it read.

    A scorer's coverage entry says `ways`, not `osm` — that is what it asked the store for.
    Until the M3 loader existed no real plan had an OSM layer in its coverage, so every
    sheet's attribution block was accidentally complete; the first corridor with ways in it
    printed three `LICENCE NOT RECORDED` lines for data that is ODbL and share-alike.

    This walks the store's own table map, so a layer added there without a licence entry
    fails here rather than on someone's sheet.
    """
    from longrun.core.data.postgis import DEFAULT_LAYER_TABLES

    missing = [layer for layer in DEFAULT_LAYER_TABLES if attribution.licence_for(layer) is None]
    assert not missing, f"layers with no licence entry: {missing}"


def test_the_osm_layers_are_attributed_to_openstreetmap() -> None:
    """Not merely *a* licence: the right one. `ways` resolving to a public-domain row
    would pass the test above and still be a licence violation."""
    for layer in ("ways", "nodes", "amenities", "railways", "way_matching"):
        licence = attribution.licence_for(layer)
        assert licence is not None and licence.source == "osm", (layer, licence)
    assert "OpenStreetMap" in attribution.render(["ways", "nodes"])
    assert attribution.unattributed(["ways", "nodes", "way_matching"]) == []


def test_an_unknown_source_is_surfaced_not_hidden() -> None:
    """An unattributed source is a licence bug and should be visible immediately."""
    assert attribution.unattributed(["some_new_feed"]) == ["some_new_feed"]
    assert "LICENCE NOT RECORDED" in attribution.render(["some_new_feed"])


def test_no_sources_renders_cleanly() -> None:
    assert "No external sources" in attribution.render([])


# --- scratchpad (scope 4.2, 8.1) --------------------------------------------


def _scratchpad() -> Scratchpad:
    return Scratchpad(plan_id="p1", request=_request(), route=_route())


def test_scratchpad_round_trips_through_disk(tmp_path: Path) -> None:
    """Scope 4.2: a needs_input pause resumes from this, so it must persist whole."""
    pad = _scratchpad()
    pad.status = "needs_input"
    pad.round = 3
    pad.trade_offs.append(TradeOff(segment_id="s00014", option_a="A", option_b="B", comparison="…"))
    restored = Scratchpad.load(pad.save(tmp_path / "pad.json"))
    assert restored.status == "needs_input"
    assert restored.round == 3
    assert restored.trade_offs[0].segment_id == "s00014"


def test_putting_a_result_twice_does_not_double_count() -> None:
    """A rerun after a reroute must replace, not append."""
    pad = _scratchpad()
    pad.put_result(ScorerResult(name="legality", flags=[_flag("legality")]))
    pad.put_result(ScorerResult(name="legality", flags=[]))
    assert len(pad.results) == 1
    assert pad.result("legality") is not None
    assert pad.result("legality").flags == []  # type: ignore[union-attr]


def test_locking_a_range_is_recorded() -> None:
    pad = _scratchpad()
    pad.lock(100.0, 300.0, reason="user chose alternative B")
    assert pad.locked == [
        LockedRange(start_m=100.0, end_m=300.0, reason="user chose alternative B")
    ]


def test_unknown_scorer_lookup_returns_none() -> None:
    assert _scratchpad().result("nope") is None


# --- check 8 must not pass on missing signalization -------------------------


def _crossings_result(*, signals_checked: bool, with_soft_flag: bool) -> ScorerResult:
    """A crossings scorer that read the ways layer but maybe not the node layer."""
    flags = []
    if with_soft_flag:
        flags.append(
            Flag(
                scorer="crossings",
                segment_id="s00002",
                kind=FlagKind.SOFT,
                tier=Tier.COMFORT,
                severity=0.4,
                reason_code="unsignalized_secondary_crossing",
            )
        )
    result = ScorerResult(name="crossings", flags=flags)
    result.coverage.append(CoverageEntry(source="ways", kind="osm_crossings", checked=True))
    result.coverage.append(
        CoverageEntry(
            source="nodes",
            kind="traffic_signals",
            checked=signals_checked,
            reason=None if signals_checked else "no node layer",
        )
    )
    return result


def test_check_eight_skips_when_signalization_was_never_known() -> None:
    """A safety check cannot pass on data nobody had.

    The crossings scorer can read the ways layer and emit soft flags while having no node
    layer to distinguish signalized from unsignalized. Judging by "did anything get
    flagged" would let check 8 pass on a region where signalization is simply unknown.
    """
    results = [_crossings_result(signals_checked=False, with_soft_flag=True)]
    check = next(
        c for c in gpx_verify(_route(), _request(), results=results).results if c.number == 8
    )
    assert check.status == "skipped"


def test_check_eight_skips_even_with_no_flags_when_signals_are_unknown() -> None:
    results = [_crossings_result(signals_checked=False, with_soft_flag=False)]
    check = next(
        c for c in gpx_verify(_route(), _request(), results=results).results if c.number == 8
    )
    assert check.status == "skipped"


def test_check_eight_passes_once_signalization_is_actually_known() -> None:
    results = [_crossings_result(signals_checked=True, with_soft_flag=True)]
    check = next(
        c for c in gpx_verify(_route(), _request(), results=results).results if c.number == 8
    )
    assert check.status == "passed"


def test_check_eight_fails_on_a_hard_crossing_when_signals_are_known() -> None:
    result = _crossings_result(signals_checked=True, with_soft_flag=False)
    result.flags.append(
        Flag(
            scorer="crossings",
            segment_id="s00004",
            kind=FlagKind.HARD,
            tier=Tier.SAFETY,
            severity=0.95,
            reason_code="unsignalized_primary_crossing",
        )
    )
    check = next(
        c for c in gpx_verify(_route(), _request(), results=[result]).results if c.number == 8
    )
    assert check.status == "failed"
    assert check.offenders == ["s00004"]
