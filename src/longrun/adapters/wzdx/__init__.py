"""Tier 1: WZDx work-zone data exchange feeds. Confirm feed status from the current
registry before relying on a jurisdiction having one - and then call it, because the
registry is wrong in both directions.

**The survey M14 ran, so the next person does not have to repeat it.** The USDOT registry
(`https://data.transportation.gov/resource/69qe-yiui.json`) was read on **2026-09-23**: 43
rows, 42 active, of which 29 claim to need no key. Every one of the 29 was called. The
sixteen adopted feeds are one module each in this package and each records its own call;
what follows is the refusals, which are the part that is expensive to discover and free to
read.

**The registry's `needapikey` flag is unreliable in both directions.** `fldot` and `odot`
(Oklahoma) are listed as needing no key and both answer **HTTP 401** without the credential
the registry publishes inside their own URLs - `app_key=` for Florida, `access_token=` for
Oklahoma. So 27 of the 42 active feeds are genuinely keyless, not 29. In the other
direction `wzdx.azdot` is not in the registry at all, needs no key, and has documentation
saying it does; calling is the only evidence (`azdot.py:11`). All eight keyed feeds
reachable at all were re-tested unauthenticated on 2026-09-23 and all eight genuinely
refuse - Colorado 403; Michigan, Ohio, Oregon, the Pennsylvania Turnpike, TxDOT and VDOT
401; and the Illinois Tollway could not be reached, its certificate chain failing to
validate.

**Refused, with the measurement that refused it. All called 2026-09-23.**

*Frozen feeds, which are the dangerous ones.* Hawaii (`hidot`) answers 200 with a CC0
dedication and 66 features - and all 66 carry the identical window 2026-09-08 to
2026-09-15 while the envelope's `update_date` is **2024-02-22**. Every record had expired
eight days before the call. Utah (`udot`) answers 200, CC0, 744 features, envelope
`update_date` **2023-03-19**, every feature ending between 2022-11-15 and 2024-04-01.
Adopting either would report the state as checked with no closures on every future date,
which is exactly the false negative `massdot.py:28` names. Delaware's and Louisiana's
feeds empty out within hours too and were adopted, because their envelopes are minutes
old: the test is whether the publisher is still publishing, not how long a record lasts.

*Keyed after all.* Florida (`fldot`) is keyed despite the registry, and its payload
measured **108,981,785 bytes** - four hundred times the median feed and nine times the
largest adopted - against `client.HTTP_TIMEOUT_S` of 20 seconds. Oklahoma (`odot`) is
keyed despite the registry, and adopting it would mean committing somebody else's
`access_token` to this repository, which `.env.example` forbids in as many words.

*Did not answer.* Minnesota (`mndot`) raised `ReadTimeout` at 60 seconds, twice, six
minutes apart. New Mexico (`nmdot`) answered **HTTP 503** with the body `no healthy
upstream`. The registry lists both active.

*Nothing to register them against.* `CivicLink_CrewCast` answers 200, CC0, 730 features,
and the registry's state for it is `n/a`; its features span lon -98.6 to -80.2 and lat
28.5 to 40.8, roughly Texas to New England. `base.Adapter` requires jurisdictions and
there is no defensible set. `stcharlesco_v4` answers 200, CC0, 105 features, but MoDOT's
statewide CC0 feed already answers for every place in Missouri, so it adds no jurisdiction
and this milestone is about jurisdictions with no adapter at all.

*Out of scope by a rule already written.* Quebec City is outside the United States
(scope §2) and was not called. `pbs`, `cdot_cwz`, `idot_cwz` and `massdot__cwz` are CWZ
1.0, which M14 declined to guess at (ADR 0039). M19 held keys for two of them, and the
shared reader parsed every record of both, so CWZ now reads here (**ADR 0058**).

*The keyed feeds are data, not prose* (`AWAITING_KEY` below, ADR 0057). M14 refused them on
the rule "prefer keyless feeds; do not add keys nobody has", because an adapter that could
never authenticate counted toward *"N of M jurisdictions have a closure adapter"*. M18 took
that reason away: coverage now counts a claim and an answer apart (`claimed_tier` against
`tier`), so a keyed feed is adopted by whoever holds its key. The ones nobody here holds are
listed with the URL to register at, so adopting one is a single commit.

**M19 called the refusals again on 2026-10-28**, so the record above is a first survey and
this is the second:

- **Minnesota answers now.** HTTP 200, twice, the second in under four seconds: 404 live
  records, CC0. Adopted (`mndot.py`), the first keyless state since M14.
- **Hawaii is still a generated file.** HTTP 200 and CC0, with 102 records this time, all
  102 carrying the identical window 2026-10-27 19:51:56 to 2026-11-03 19:51:56 and all
  updated the same day. The envelope is still dated 2024-02-22. A window written
  identically onto every record by the publisher's own clock is not a window. Refused again.
- **Utah now answers HTTP 403**, which is no improvement on 2023's frozen file.
- **New Mexico answers HTTP 503, `no healthy upstream`, again.**
- **The Illinois Tollway's certificate still fails to validate.** Its records reach IDOT's
  CWZ feed as one of four data sources (`ildot.py`).
- **Two hosts accepted no connection at all:** `publicapi.ohgo.com`, Ohio's, and
  `atms.paturnpike.com`, the Turnpike's, each tried three times. Ohio's key is in hand and
  `www.transportation.ohio.gov` answered from the same network, so what blocks Ohio is a
  route to one host, not a credential. Both stay in `AWAITING_KEY` with that said.

**Which feeds may be committed as cassettes is ADR 0038**, and the short version is that
the project acts on `feed_info.license` in the payload and on nothing else. Ten of the
sixteen adopted feeds declare CC0 1.0 there and have cassettes; six declare nothing and do
not. New York publishes an explicit prohibition on redistributing any part of 511NY, which
is why the rule is not over-caution - see `nysdot.py`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AwaitingKey:
    """A keyed registry feed with no adapter, and what adopting it needs (ADR 0057).

    `url` is the feed without any credential. Two registry rows carry a shared credential
    inside the URL itself, and this repository commits nobody's key, published or not.
    """

    #: The USDOT registry's `feedname`, so a re-read of the registry finds the row.
    registry_id: str
    #: What the adapter will claim: the state, or for park roads the agency.
    jurisdiction: str
    url: str
    #: Where to get the key. For the two rows that publish a shared credential and no
    #: registration page, the registry itself.
    register_at: str
    #: What calling it without a key returned, and anything adopting it must know.
    why: str


#: Where the registry publishes Oklahoma's and Florida's credentials, inside their URLs.
REGISTRY = (
    "https://data.transportation.gov/Roadways-and-Bridges/"
    "Work-Zone-Data-Exchange-WZDx-Feed-Registry/69qe-yiui"
)

#: The registry's keyed feeds with no adapter, each called without a key on 2026-10-28.
#: Adopting one is one commit: a module, an `ApiKey`, an entry point and a licence row, and
#: deleting its entry here, which `test_wzdx_breadth` holds to.
AWAITING_KEY: tuple[AwaitingKey, ...] = (
    AwaitingKey(
        registry_id="cdot",
        jurisdiction="tiger:state:08",
        url="https://data.cotrip.org/api/v1/wzdx",
        register_at="https://maps.cotrip.org/help/section/for-developers.html",
        why="HTTP 403 'Not Authorized' without a key",
    ),
    AwaitingKey(
        registry_id="cdot_cwz",
        jurisdiction="tiger:state:08",
        url="https://data.cotrip.org/api/v1/cwz",
        register_at="https://manage-api.cotrip.org/subscriber/subscription",
        why="HTTP 403 'Not Authorized'; the same publisher's CWZ form, so adopt one of the two",
    ),
    AwaitingKey(
        registry_id="fldot",
        jurisdiction="tiger:state:12",
        url="https://us-datacloud.one.network/fdot/feed.json",
        register_at=REGISTRY,
        why=(
            "HTTP 401 'API key was not provided'; 109 MB, past the 32 MB ceiling, so it "
            "needs ADR 0056's tiling, built with it"
        ),
    ),
    AwaitingKey(
        registry_id="iltollway",
        jurisdiction="tiger:state:17",
        url="https://tims2go.tollway.state.il.us/wzdx/v4.2",
        register_at="https://tims2go.tollway.state.il.us/",
        why=(
            "its certificate chain fails to validate, as in M14; its records already reach "
            "IDOT's CWZ feed (`ildot.py`), and it is a partial network, so adopt it with "
            "complete = False"
        ),
    ),
    AwaitingKey(
        registry_id="mdot_4",
        jurisdiction="tiger:state:26",
        url=(
            "https://mdotridedata.state.mi.us/api/v1/organization/"
            "michigan_department_of_transportation/dataset/work_zone_information/query"
        ),
        register_at="https://www.michigan.gov/mdot/travel/safety/efforts/its/its-data",
        why="HTTP 403 without a key",
    ),
    AwaitingKey(
        registry_id="odot",
        jurisdiction="tiger:state:40",
        url="https://oktraffic.org/api/Geojsons/workzones",
        register_at=REGISTRY,
        why=(
            "HTTP 403 without a key; the registry publishes a shared access_token in the "
            "URL and no registration page, and that token belongs in a .env, never here"
        ),
    ),
    AwaitingKey(
        registry_id="ohio",
        jurisdiction="tiger:state:39",
        url="https://publicapi.ohgo.com/api/work-zones/wzdx/4.2",
        register_at="https://publicapi.ohgo.com/accounts/registration",
        why=(
            "the key is in hand, but publicapi.ohgo.com accepted no TCP connection on "
            "2026-10-28 while ohio.gov answered; adopt it from a network that reaches it"
        ),
    ),
    AwaitingKey(
        registry_id="ptc",
        jurisdiction="tiger:state:42",
        url="https://atms.paturnpike.com/api/WZDxWorkZoneFeed",
        register_at=(
            "https://datahub.transportation.gov/api/views/69qe-yiui/files/"
            "de6330ba-7e11-4335-9a3f-caf24015dc1a?download=true"
            "&filename=WZDx_API_Key_Instructions.docx"
        ),
        why=(
            "HTTP 401 without a key in M14, and no TCP connection on 2026-10-28; the "
            "Turnpike's own road, so adopt it with complete = False"
        ),
    ),
    AwaitingKey(
        registry_id="txdot_v4_2",
        jurisdiction="tiger:state:48",
        url="https://api.drivetexas.org/api/conditions.wzdx.geojson",
        register_at="https://api.drivetexas.org/",
        why="HTTP 401 'Unauthorized'; Austin answers for its own streets meanwhile",
    ),
    AwaitingKey(
        registry_id="vdot",
        jurisdiction="tiger:state:51",
        url=(
            "https://data.511-atis-ttrip-prod.iteriscloud.com/smarterRoads/workzone/"
            "workZoneJSON/current/workZone.json"
        ),
        register_at="https://smarterroads.vdot.virginia.gov/register",
        why="HTTP 401 'Unauthorized'",
    ),
)

__all__ = ["AWAITING_KEY", "REGISTRY", "AwaitingKey"]
