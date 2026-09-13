"""Jurisdiction-specific sources behind one interface (scope 7.10).

An adapter declares jurisdiction (Census GEOID or PAD-US unit ID), kind
(closures | trail_status | access_hours | speed_survey), tier, and
fetch(polygon, date) -> Features in the common schema. Registered via the
longrun.adapters entry point group, never imported by path.

Tiers, most to least reliable:
    1  wzdx/        WZDx work-zone feeds
    2  state511/    state 511 APIs
    3  portals/     city and county open-data portals
    4  extraction/  LLM extraction from agency pages and PDFs, confidence <= 0.5

Discovery spatial-joins the route buffer against TIGER and PAD-US to get jurisdiction IDs,
then looks each up in the registry. A missing adapter falls back to tier 4 and is recorded
in the coverage manifest as unverified. Tiers 3 and 4 are brittle by design; every plan
reports which tiers returned data for each jurisdiction crossed.

Planned modules:
    base.py          Adapter protocol, Tier enum, Feature schema, confidence rules
    registry.py      entry-point discovery, jurisdiction lookup, coverage reporting
    promote.py       draft an adapter from a successful tier-4 extraction for human review
"""
