# 0014 — Adapters reach a scorer by injection, not by import

Status: accepted (2026-09-11)

## Context

Scope §7.10 fixes how adapters are *registered* — "Registered via Python entry points" —
and says nothing about how `core/` reaches the result. The layering rule in
`src/longrun/__init__.py` points the arrow in the permitting direction:

```
cli/ api/ tools/ agent/  ->  core/  ->  adapters/
```

and `tests/unit/test_layering.py` enforces only that `core/` does not import `agent/`,
`tools/`, `cli/` or `api/`. So `core/scorers/closures.py` importing
`longrun.adapters.registry` would be legal, would pass every gate, and is the shorter
thing to write. It is what the package docstrings imply, too: `adapters/__init__.py`
describes discovery as something that happens, without saying who asks.

This needs a decision rather than a default because it is expensive to reverse. The choice
propagates to every one of the three §7.6 scorers, to their tests, and to every adapter
anyone writes afterwards — and reversing it later means rewriting all of them at once.

## Decision

**`core/` declares a `FeatureSource` Protocol and is handed an implementation on
`ScorerContext`. It imports nothing under `longrun.adapters`.**

`core/data/base.py` declares `FeatureSource` beside `LayerStore`, with a
`NullFeatureSource` beside it as `NullRouter` sits beside `Router`.
`ScorerContext.features` is `FeatureSource | None`. `cli/repair.py` constructs
`AdapterRegistry(cache, budget, offline=offline)` — imported lazily inside `score_route`,
so `longrun --version` pays nothing for entry-point scanning — and passes it in alongside
`FileRasterStore` and `SqliteCache`. Nothing in `core/` names an adapter, a tier-1 feed or
a jurisdiction.

**Four reasons, in the order they carried weight.**

*A test needs a stub, not an installed package.* With injection, a scorer test builds a
`ScorerContext` with an object that returns canned `Feature`s. With an import, every test
for all three scorers would have to fabricate installed entry points or monkeypatch a
module global — a second fixture mechanism, which this project has refused to build three
times already (`cache.py`: the production cache *is* the cassette store, "so there is no
second recording system to maintain").

*Ambient module state is the thing the layering test exists to prevent.* Every other source
a scorer reads — layers, rasters, cache, clock, profile, budget — arrives on the context.
A registry acquired by import would be the only exception, and it is the same shape of
problem as `test_core_has_no_implicit_clock`: not illegal, just invisible, and therefore
untestable at exactly the moment it matters.

*`core/` must import on a bare `uv sync`.* Adapters reach the network; tier 4 will
eventually want a model client. Under injection, whether an adapter needs `httpx`,
`duckdb` or `anthropic` is structurally none of a scorer's business. Under an import it
becomes a discipline every future adapter author has to remember, enforced by nothing.

*Precedent, twice.* `LayerStore` and `Router`/`NullRouter` are the same shape: a Protocol
in `core/`, two or more implementations, injected by the CLI. A third instance of a shape
used twice is not novelty.

**`None` rather than a default `NullFeatureSource`** on the context, because "no registry
was configured" and "a registry, but no adapter covers this jurisdiction" are different
sentences and §3.6 requires both to reach the plan sheet. A default instance would collapse
them into one silently.

## Consequences

* `adapters/__init__.py`'s claim — *"Nothing outside adapters knows which jurisdiction it
  is in"* — becomes true rather than aspirational. `closures.py` knows only "ask for
  closures in this polygon on this date".
* The registry, not the scorer, owns tier arbitration, the fetch budget and per-jurisdiction
  failure reasons. A scorer receives a `FeatureSet` and reports it.
* Third-party adapters still register by entry point, exactly as §7.10 says. The decision
  is about the consumer, not the producer, and changes nothing for an adapter author.
* `tests/unit/test_layering.py` does *not* forbid `core/ -> adapters/`, and deliberately
  still does not. The arrow is legal; this ADR is the reason the codebase does not use it.
  A test would state the conclusion without the argument, and the argument is the part that
  would otherwise be re-litigated.

## What would make us revisit

**An adapter that a scorer genuinely cannot describe through the Protocol.** The current
`fetch(kind, jurisdictions, polygon, day)` covers the four §7.10 kinds because they are all
"what does this body say about this ground on this date". A source shaped differently —
one that must be queried per segment, or that streams — would strain it, and at that point
widening the Protocol and importing directly should be compared honestly rather than
assumed.

**A second consumer that is inside `core/`.** `regions/build.py` imports
`AdapterRegistry` directly and is right to: it is a composition layer like `cli/`, above
`core/` in the same arrow, and it has no `ScorerContext` to be handed one on. What the two
callers share is the *vocabulary* — `Jurisdiction` and `AdapterInfo` live in
`core/models/jurisdiction.py`, so a region build reports adapter coverage in the same terms
a scorer does without `core/` importing anything. If a third caller appears that sits below
`core/`, or one that needs something the Protocol does not carry, the shape is worth
re-examining rather than widened twice.
