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
then looks each up in the registry. Every id a jurisdiction sits in is a ladder of whoever
claims it, climbed from tier 1: an answer stops it, a failure falls through, and a
jurisdiction no ladder answered goes to tier 4 and is recorded as unverified (ADR 0047).
Tiers 3 and 4 are brittle by design; every plan reports which tiers returned data for each
jurisdiction crossed, and `longrun adapter-coverage` reports the same for the country.

Modules:
    base.py          the Adapter protocol, its context and result, optional attributes
    registry.py      entry-point discovery, the tier ladder, the per-plan memo and ceiling
    keys.py          credentials an install may lack, each naming where to get it
    promote.py       draft an adapter from a successful tier-4 extraction for human review
"""
