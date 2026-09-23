# 0038 — A feed declares its own licence, or its payload is not committed

Status: **accepted**, M14, 2026-09-23.

## Context

ADR 0006 settled the rule: *"a source whose data cannot be redistributed cannot be committed
as a cassette, and therefore cannot back a golden route."* What it never settled is how this
project decides which sources those are.

Until M14 the question had not come up in bulk. Four WZDx feeds were wired up and three of
them happened to be obviously CC0; `azdot.py` records the fourth as uncommittable because
"AZ511 terms of use" is not a redistribution grant, which is right but reads as a judgement
call made once. M14 called twenty-nine feeds in a day and needed the same judgement
twenty-nine times.

**The USDOT registry states no licence for any feed.** Its schema has `format`, `version`,
`needapikey`, `apikeyurl` and a URL, and nothing about terms. So eligibility is a
per-publisher question, and that — not the thirteen lines of code an adapter takes — is the
expensive part of adding one.

Three things could have answered it, and two of them cannot be relied on:

* **A publisher's terms-of-use page.** These are written for drivers using a 511 website,
  not for a redistributor, and reading one is an act of legal interpretation this project
  is not equipped to perform at scale. WSDOT's data-catalog page answered HTTP 404 when
  M14 looked for it. The Iowa DOT open-data portal does say CC0 — about the portal's
  datasets, not about the ATMS endpoint that publishes the WZDx feed from a vendor host.
* **The publisher being a US government agency.** Tempting, and wrong. Federal works are
  public domain; state works are not, and several of these feeds are published by
  contractors (Arcadis, HaulHub, Blyncsy, Castle Rock) or by universities (NJIT) on an
  agency's behalf.
* **The `license` field inside the payload.** WZDx 4.x carries an optional
  `feed_info.license`, and where a publisher fills it in, it is a grant delivered with the
  data by the party publishing it.

The third is also, it turns out, what the project has been doing already without writing it
down. All three cassettes committed before M14 — `wzdx_modot`, `wzdx_kdot`, `wzdx_maricopa`
— carry `"license": "https://creativecommons.org/publicdomain/zero/1.0/"` in their
envelopes, and that is where the "CC0" in `attribution.LICENCES` came from.

## Decision

**A feed's payload may be committed to this repository only if the payload itself declares
CC0 in `feed_info.license` (or `road_event_feed_info.license` at 4.0).** Nothing weaker
counts: not a terms page, not the publisher being a state agency, not a licence on a
different dataset from the same body, not silence.

A feed that declares nothing may still have an adapter. Reading a feed at plan time is use;
committing it is redistribution, and only the second needs the grant. The consequence is
stated on the adapter rather than implied: six of M14's sixteen adopters say **no cassette**
in their docstring and say why.

`test_wzdx_breadth.py::test_every_committed_cassette_declares_cc0` makes this executable. It
scans `tests/contract/cassettes/` rather than iterating a list, because a list can be
extended without thinking and a directory cannot be added to without the test failing.

## Consequences

**Ten of sixteen adopted feeds have a cassette; six do not.** The ten declare CC0: North
Dakota, Delaware, Indiana, Kentucky, Louisiana, Maryland SHA, Mississippi, New Jersey,
Wisconsin, Austin. The six declare nothing: New England Compass (ME/NH/VT), Idaho, Iowa,
New York, North Carolina, Washington.

**So six live adapters have no CI-tested parse path, and that is a licence consequence
rather than a gap in the work.** `massdot.py:28` warns that a feed which "answers and
parses to nothing would look exactly like a state with no work zones", and the only defence
those six have is the `network`-marked live check, which CI deselects. This is the real
price of the rule and it should be stated plainly rather than discovered later: New York,
the largest of the six at 6,330 features, is one schema change away from silently reporting
an empty state, and nothing in the hermetic suite would notice.

**The rule is not over-caution, and 511NY is the proof.** Its terms page, read 2026-09-23:

> Redistribution or republication of any part of 511NY or its content is prohibited,
> including by such methods as framing, other similar methods or by any other means,
> without the prior express written consent of NYSDOT.

That is an explicit prohibition rather than an absent grant. Before M14 went looking, the
honest expectation was that silent feeds were mostly permissive and the rule was
bureaucratic. One of the six is the opposite, and the project has no way to tell which
without reading every publisher's terms — which is the argument for acting on the payload
and nothing else.

**A golden route can only ever cross the ten.** Scope §11's regions are unaffected — the
five existing ones are in California, Massachusetts, Missouri, Kansas and Arizona — but a
future region chosen for its closure data should be chosen from the CC0 ten, the way §11
region 4 was placed on the Kansas–Missouri line rather than at Portland–Vancouver because
Oregon's feed needs a key (`modot.py`).

**`attribution.LICENCES` gains a row per feed even where none is owed.** CC0 asks for no
attribution at all, and the generic `wzdx` row would already resolve every new source by
prefix. The rows exist so the sheet names the publishing agency: "Work zone data via the
USDOT WZDx feed registry" on a Wisconsin plan credits a registry that publishes nothing and
hides the body a reader would have to ring to check a closure.

## What would make us revisit

**A publisher confirming redistribution in writing somewhere machine-readable.** A
`LICENSE` file beside a feed, a data.gov entry with a licence field, a repository the
agency publishes to. Any of those is the same kind of evidence as the payload field and
would extend the rule rather than break it.

**A WZDx spec change.** `feed_info.license` is optional in 4.x. If a later version makes it
required, or narrows the permitted values, the rule gets stronger for free. If a version
drops it, this ADR loses its only evidence and the question reopens with nothing to replace
it.

**A cheap hermetic check for the six.** The gap this ADR creates is testability, not
legality, and it could be closed without committing anybody's data — a stored *shape*
rather than a payload: field names, types and counts, with every value stripped. Nobody has
established whether that is a derivative work, and it would not have caught the bugs the
real cassettes caught (`feed.py`'s envelope-key bug was about a key name, which a shape
fixture would hold, but the timestamp-format bug was about values, which it would not). It
is worth trying before it is worth assuming.
