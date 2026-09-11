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

#: Arizona's statewide 511. Maricopa County's WZDx feed is keyless and covers Phoenix, so
#: this adds the rest of the state rather than unblocking it.
AZ_511 = ApiKey(
    env_var="LONGRUN_AZ511_API_KEY",
    service="AZ511",
    register_at="https://www.az511.com/developers",
)

#: The National Park Service alerts API - what `trail_status` reads for any route crossing
#: `padus:NPS` land, which PAD-US resolves for every national park in the country.
NPS = ApiKey(
    env_var="LONGRUN_NPS_API_KEY",
    service="the NPS data API",
    register_at="https://www.nps.gov/subjects/developer/get-started.htm",
)

ALL_KEYS = (SF_BAY_511, AZ_511, NPS)

__all__ = ["ALL_KEYS", "AZ_511", "NPS", "SF_BAY_511", "ApiKey"]
