# 0057 — A keyed feed is adopted by whoever holds its key, and the rest are data

Status: **accepted**, M19, 2026-10-28. Amends M14's rule in `adapters/wzdx/__init__.py`.

## Context

M14 called every keyless feed in the USDOT WZDx registry and adopted sixteen. It refused the
keyed ones on a rule: *"prefer keyless feeds; do not add keys nobody has"*. Its reason was the
number M14 was measured by. `registry.adapters_for` did not ask whether a key was set, so an
adapter that could never authenticate counted toward *"N of M jurisdictions have a closure
adapter"*.

Two things changed:

- **M18 counts a claim and an answer apart.** `CoverageRow.claimed_tier` is any claimant;
  `tier` is one a plan can ask today. A keyed adapter with no key no longer inflates the
  usable count, and a second ratchet counts the states covered with every key set.
- **Keys arrived.** The person running this project registered for Oregon's, Illinois's,
  MassDOT's, Ohio's and the NPS's.

The refusals were also prose. Which feeds needed a key, where to get one and what each
returned without one lived in a docstring paragraph. Nothing tested it, and nobody could act
on it without re-reading the registry.

## Decision

**1. A keyed feed is adopted by whoever holds its key.** The adapter is written, called with
the key, and registered. Without a key it replays a recorded payload or returns its key's own
reason (M17, `client.replay_or_refuse`). A key decides who can fetch; the envelope's licence
decides what may be committed (ADR 0038). A keyed feed declaring CC0 backs a cassette that
replays in keyless CI.

**2. The ones nobody here holds a key for are `wzdx.AWAITING_KEY`.** Each entry carries:
- the registry `feedname`;
- the jurisdiction it would claim;
- the feed URL with no credential;
- where to register;
- what calling it without a key returned.

Adopting one is a single commit, and deleting its entry is part of that commit. Tests hold
that no feed is both awaiting and adopted, and that no entry carries a credential.

**3. A credential published inside a registry URL is still somebody's credential.**
Oklahoma and Florida publish a shared token in their registry rows and no registration page.
Their entries point at the registry, and `.env.example` says such a token goes in a person's
own `.env` and never in this repository.

**4. A key goes in a header wherever the feed accepts one** (Oregon, MassDOT, the NPS).
Where it must travel as a query parameter (511 SF Bay, Illinois), a refused request still
never names it: `http.get_json` raises `FeedRefused`, carrying the status, host and path and
nothing in the query (ADR 0059).

## Consequences

* **Adopted in M19, each verified live with its key on 2026-10-28, each CC0 with a
  cassette:**

  | Adapter | Claims | Spec | Records |
  |---|---|---|---|
  | `wzdx.ordot` | Oregon | WZDx 4.0 | 171 |
  | `wzdx.ildot` | Illinois, the Tollway's records included | CWZ 1.0 | 800 |
  | `wzdx.nps` | `padus:NPS`, park roads | WZDx 4.1 | 114 |
  | `wzdx.massdot` | Massachusetts, verified for the first time | CWZ 1.0 | 1,363 |

* **The key named `LONGRUN_ODOT_API_KEY` is Oregon's.** Three state DOTs answer to ODOT, and
  the key was tried against two:
  - Oklahoma's feed answered 403;
  - Oregon's answered 200.

  Ohio's feed is OHGO's, under its own key. The variable is now `LONGRUN_ORDOT_API_KEY`,
  after the registry's `ordot`, and the person who owns the `.env` renames the line.
* **Ohio's key is in hand and its feed is not adopted.** `publicapi.ohgo.com` accepted no
  TCP connection on 2026-10-28, while `www.transportation.ohio.gov` answered from the same
  network. An adapter verified against nothing is what `azdot.py:11` forbids. Ohio waits in
  `AWAITING_KEY`, and its entry says what stands in the way, which is not a key.
* **Both ratchets rise**, measured on top of M20's California. Every key set: 22 → 25
  (Oregon, Illinois, Minnesota). Keyless: 21 → 22, for Minnesota, which M19's re-survey
  found answering keyless.
* **Ten feeds remain in `AWAITING_KEY`:** Colorado (WZDx and CWZ), Florida, the Illinois
  Tollway, Michigan, Ohio, Oklahoma, the PA Turnpike, Texas and Virginia. M20 keeps the same
  kind of list for 511 platforms, `state511.AWAITING_KEY`; the two are separate structures.

## What would make us revisit

A key that cannot be shared even through a cassette: a publisher whose terms forbid
redistributing what its key fetches. Rule 1 still holds then, but ADR 0038 decides what is
committed, and an adapter with no cassette and no key in CI has no hermetic parse path.
That is what shape fixtures are for (ADR 0055).
