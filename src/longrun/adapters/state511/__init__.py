"""Tier 2: a state DOT's own feeds that are not WZDx (scope 7.10; M20, ADR 0059).

**Tier is source type.** A 511 system or DOT feed that publishes WZDx is tier 1 and lives in
`adapters/wzdx/` - 511 SF Bay and Arizona DOT are there, tier 1 though one needs a key,
because a credential is a reason a fetch failed and says nothing about what the data is.
This package is for the rest: a vendor's or a DOT's own shape, structured, windowed, and
not a standard. `MIN_HARD_FLAG_TIER` is 2, so a record here *may* fail a route, and ADR
0059 is the rule for when: **only along a drawn line**. A record placed by its end points
is held below `closures.HARD_FLAG_CONFIDENCE`.

**The survey M20 ran, so the next person does not have to repeat it.** Read 2026-10-28,
against the 29 states and DC that no keyless tier-1 or tier-2 source answered for.

*Adopted.* **California**, through Caltrans' Lane Closure System (`caltrans_lcs.py`):
keyless, one file per district, the state highway system statewide, and recordable under
Caltrans' conditions of use (ADR 0059). The WZDx registry's
only California row is MTC's regional feed (`wzdx.sfbay`), and the two are complements -
measured, about one record in ten has a counterpart in the other.

*Built, waiting for a key.* IBI Group builds 511 systems for many states, each serving the
same documented "GET Events" endpoint (`/api/v2/get/event`), each answering a keyless call
with HTTP 400 and `<Error><Message>Invalid Key</Message></Error>`, and each documenting a
throttle of ten calls a minute - one call per plan day, since the list is cached.
`ibi_v2.py` reads them all; `AWAITING_KEY` below is who is waiting.
Adopting one is a module constructing `ibi_v2.IbiClosures`, an `ApiKey` in
`adapters/keys.py` (and so in `ALL_KEYS` and `.env.example`), a `LICENCES` row and an entry
point - one commit, with the live check in `test_state511.py` run against the key.

*IBI's endpoint, but nowhere to register.* Pennsylvania (`www.511pa.com`) and Florida
(`fl511.com`) answer the event endpoint exactly as the IBI platforms do, and neither
publishes the developer pages the others do (`/developers/doc` and `/help/endpoint/event`
are 404 on both). Florida's WZDx feed is keyed and 109 MB (ADR 0056); a 511 key would be a
smaller door into the same state, if one can be had.

*Not needed.* Idaho, Arizona and Louisiana run IBI platforms too, but each already answers
at tier 1 with a keyless WZDx feed (`wzdx.iddot`, `wzdx.azdot`, `wzdx.ladotd`).

*Not an IBI platform.* Called for the IBI endpoint, the 511 or DOT sites of Alabama,
Arkansas, Colorado, DC, Hawaii, Illinois, Michigan, Minnesota, Montana, Nebraska, New
Mexico, Oklahoma, Oregon, South Carolina, South Dakota, Texas, Virginia, West Virginia and
Wyoming answered with an HTML page, a 404, a 403 or a 5xx; Ohio, Rhode Island and
Tennessee did not answer within 20 s. Each is a different vendor or none, and none was
surveyed further: several have keyed WZDx feeds in the USDOT registry, which are tier 1 and
the better door (M19).

*Cut.* New York and Wisconsin run IBI platforms, and neither is wanted at tier 2: both are
answered at tier 1 by keyless WZDx, and 511NY's terms prohibit redistribution.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Awaiting:
    """A tier-2 source that is built for and not adopted, because nobody here holds its key.

    Data rather than prose, so the day a key arrives the adoption is one commit, and a test
    can hold the list to the registry: nothing is both waiting and adopted.
    """

    #: The adapter name it would be registered under.
    name: str
    statefp: str
    #: The endpoint, which answers a keyless call with the refusal that proves it is there.
    url: str
    register_at: str
    why: str


#: Every IBI platform for a state no keyless tier-1 or tier-2 source answers, read
#: 2026-10-28. Each documents the same event fields at `<site>/help/endpoint/event`, and each
#: wants an account before it issues a key.
AWAITING_KEY: tuple[Awaiting, ...] = (
    Awaiting(
        name="state511.ga",
        statefp="13",
        url="https://511ga.org/api/v2/get/event",
        register_at="https://511ga.org/developers/doc",
        why="511GA (Georgia DOT); Georgia has no WZDx feed in the registry",
    ),
    Awaiting(
        name="state511.ak",
        statefp="02",
        url="https://511.alaska.gov/api/v2/get/event",
        register_at="https://511.alaska.gov/developers/doc",
        why="511 Alaska (Alaska DOT&PF); Alaska has no WZDx feed in the registry",
    ),
    Awaiting(
        name="state511.nv",
        statefp="32",
        url="https://www.nvroads.com/api/v2/get/event",
        register_at="https://www.nvroads.com/developers/doc",
        why="NVRoads (Nevada DOT); Nevada has no WZDx feed in the registry",
    ),
    Awaiting(
        name="state511.ct",
        statefp="09",
        url="https://ctroads.org/api/v2/get/event",
        register_at="https://ctroads.org/developers/doc",
        why="CTroads (Connecticut DOT); Connecticut has no WZDx feed in the registry",
    ),
    Awaiting(
        name="state511.ut",
        statefp="49",
        url="https://udottraffic.utah.gov/api/v2/get/event",
        register_at="https://udottraffic.utah.gov/developers/doc",
        why=(
            "UDOT Traffic; Utah's WZDx feed is frozen at 2023 (M14). Unverified: the site "
            "answered every request from here, documentation included, with HTTP 403"
        ),
    ),
)

__all__ = ["AWAITING_KEY", "Awaiting"]
