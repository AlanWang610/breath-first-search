"""API keys an install may or may not have (scope 7.10, 12; ADR 0006, ADR 0012).

Every tier-2 and tier-3 source this project can reach needs a free key tied to an email
address. That is not a reason to leave them unwritten, and it is not a reason to pretend
they answered: it is the `cell_coverage` and HPMS situation exactly, and both were settled
the same way. **The adapter is written, the key is blank in `.env.example`, and the coverage
manifest says which key is missing by name.**

Named by name on purpose. *"closures unavailable"* tells a reader nothing they can act on;
*"LONGRUN_AZ511_API_KEY is not set"* tells them what to do, and a key dropped into `.env`
later costs no code change at all.

Read at call time, never at import. A module-level `os.environ[...]` would make the value a
property of when the process started, and `tests/conftest.py` clears every `LONGRUN_*` var
per test precisely so a suite cannot pass or fail on someone's shell.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ApiKey:
    """One credential, and where to get it.

    The registration URL lives here rather than only in `.env.example`, so the reason a
    jurisdiction went unchecked and the fix for it arrive together.
    """

    env_var: str
    service: str
    register_at: str

    def value(self, env: dict[str, str] | None = None) -> str | None:
        raw = (env if env is not None else os.environ).get(self.env_var, "")
        return raw.strip() or None

    def missing_reason(self) -> str:
        return f"{self.env_var} is not set; {self.service} needs a free key ({self.register_at})"


#: 511 SF Bay, the Metropolitan Transportation Commission's regional feed. Its WZDx endpoint
#: is the Bay Area's tier-1 source and is the reason `bayarea` reports no closures.
SF_BAY_511 = ApiKey(
    env_var="LONGRUN_511SF_API_KEY",
    service="511 SF Bay",
    register_at="https://511.org/open-data/token",
)

# There is no AZ511 entry, and its absence is the correction. This project shipped one,
# pointing at `https://www.az511.com/developers` - a URL that 302s to `/notfound` - for a
# feed that turns out to need no key at all. Arizona's WZDx endpoint answers unauthenticated
# (see `adapters/wzdx/azdot.py`), so asking anyone to register for it was asking them to do
# work for nothing, in a file whose whole purpose is telling them what work to do.

#: MassDOT's construction work-zone feed. The USDOT registry lists it active, and it
#: answers HTTP 401 without a credential - which is why Boston reported no closure adapter
#: for any of its 23 jurisdictions until one was written.
#:
#: `register_at` is the registry's own `apikeyurl` for `massdot__cwz`, the "Work Zones API"
#: portal. It used to be a mass.gov developer page that nobody had confirmed offers this
#: key, and which refuses automated requests, so it could not be checked either.
MASSDOT = ApiKey(
    env_var="LONGRUN_MASSDOT_API_KEY",
    service="the MassDOT work-zone feed",
    register_at="https://api-app.massdot-swzm.com/",
)

#: Oregon DOT's WZDx feed, through its Azure API Management portal (M19). Named after the
#: registry's `ordot` rather than "ODOT", which is also Ohio's and Oklahoma's: the key this
#: project was given as `LONGRUN_ODOT_API_KEY` turned out, by calling, to be Oregon's.
ORDOT = ApiKey(
    env_var="LONGRUN_ORDOT_API_KEY",
    service="Oregon DOT's work-zone feed",
    register_at="https://apiportal.odot.state.or.us/",
)

#: The National Park Service alerts API - what `trail_status` reads for any route crossing
#: `padus:NPS` land, which PAD-US resolves for every national park in the country.
NPS = ApiKey(
    env_var="LONGRUN_NPS_API_KEY",
    service="the NPS data API",
    register_at="https://www.nps.gov/subjects/developer/get-started.htm",
)

#: The model key, which is a key-gated external source like any other - it just happens to
#: gate five call sites rather than one feed. Listed here so the two contract tests that
#: keep every other key honest keep this one honest too: blank in `.env.example`, and a
#: registration URL somebody can actually follow.
ANTHROPIC = ApiKey(
    env_var="ANTHROPIC_API_KEY",
    service="the LLM call sites and tier-4 extraction",
    register_at="https://console.anthropic.com/settings/keys",
)

#: A Socrata app token (M21). **Optional even where it is used**, and the only key here that
#: no adapter declares as its `key`: Socrata answers anonymous queries at a lower rate limit,
#: so a catalog dataset is asked whether or not this is set. Declaring it would make the
#: registry skip every Socrata rung on a machine without one - which is every CI run - for
#: a credential that changes no answer.
SOCRATA = ApiKey(
    env_var="LONGRUN_SOCRATA_APP_TOKEN",
    service="Socrata open-data portals (a higher rate limit only)",
    register_at="https://dev.socrata.com/docs/app-tokens.html",
)

ALL_KEYS = (SF_BAY_511, MASSDOT, ORDOT, NPS, ANTHROPIC, SOCRATA)

__all__ = ["ALL_KEYS", "MASSDOT", "NPS", "ORDOT", "SF_BAY_511", "SOCRATA", "ApiKey"]
