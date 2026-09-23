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
1.0, which this package has never parsed and now records a decision not to guess at - see
**ADR 0039**.

*The ten remaining keyed feeds* were refused on the rule "prefer keyless feeds; do not add
keys nobody has". `registry.adapters_for` does not check whether a key is present, so an
adapter that can never authenticate still counts toward *"N of M jurisdictions have a
closure adapter"* - adding ten would have inflated the one number this milestone is
measured by.

**Which feeds may be committed as cassettes is ADR 0038**, and the short version is that
the project acts on `feed_info.license` in the payload and on nothing else. Ten of the
sixteen adopted feeds declare CC0 1.0 there and have cassettes; six declare nothing and do
not. New York publishes an explicit prohibition on redistributing any part of 511NY, which
is why the rule is not over-caution - see `nysdot.py`.
"""
