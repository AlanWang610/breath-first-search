# 0029 — A routing request's cache key learns a field by eliding its default

Status: **accepted**, M10, 2026-09-18. Amends ADR 0017.

## Context

ADR 0017 established that router responses are cached, keyed on the graph rather than the
date, and it states the key as six fields. M10 needs a seventh: a cue sheet requires
GraphHopper turn instructions, and `route_body` has set `"instructions": False` since M0.

`tools/runnability.py` recorded the trap before this milestone existed, in the `unavailable`
reason it shipped `cue_sheet` with:

> Flipping `instructions` to True in `route_body` is the obvious first move and it would fail
> silently: `instructions` is not among the fields `CachedRouter._paths` keys a route on, so
> every cassette recorded without instructions would be served to a request that wants them,
> and the result would be a cue sheet with no turns in it — which is indistinguishable from a
> route that has none. **The key has to learn the flag first.**

The naive fix — add the field to the args dict — is worse than it looks. `args_hash` is
`sha256(json.dumps(args, sort_keys=True, separators=(",", ":")))`, so a key holding `False`
changes the hash **exactly as much** as one holding `True`.

Measured against `loop-bayarea`'s committed cassette before deciding:

| args dict | key | in the cassette |
|---|---|---|
| today's six fields | `b7c326780d10196c…` | **yes** |
| `+ "instructions": False` | `c4599f2650b28f31…` | no |
| `+ "instructions": True` | `032d99e2925517bb…` | no |

So inserting it unconditionally, with either value, orphans every recorded route in the
repository — and the remedy would be re-recording against a Bay Area graph that has to be
rebuilt first.

## Decision

**A new field enters the cache key only in its non-default state.** `route_args` inserts
`"instructions": True` when instructions were requested and omits the key entirely otherwise,
which reproduces the historical hash byte for byte.

The elision is a property of the **cache-key args dict only**. `route_body` keeps emitting
`instructions` in the body on every request, including when `False`, because GraphHopper's
own default for it is *true* — dropping the key would silently turn instructions on and grow
every response. Blurring those two is how this gets built wrong.

`route_args` is lifted out of `CachedRouter._paths` to a module-level function, because it is
the one thing in that module that must be *pinned* rather than merely tested.

## Consequences

**No cassette is re-recorded and no graph is rebuilt.** Asking for instructions becomes a
genuinely different question with its own recording, which is the correct behaviour.

**This holds only while not asking is the default.** Flipping `route_body`'s default would
re-key everything — loudly, as a `CacheMiss`, which is the safe direction.

**The dangerous direction is a *new* body field that changes the answer and is not added
here.** That is silent, and it is the same failure one field along. `elevation: False` is the
live candidate: it is in the body, it would change the response, and nothing keys on it. So
the guard is written as the general invariant rather than as a test about instructions:
`test_no_two_distinct_requests_share_a_key` builds every shape `route_body` can produce and
asserts the keys are distinct, and a companion test asserts that the three fields the key
*ignores* are exactly the ones no input can vary.

**The strongest guard is in the golden suite and costs nothing.**
`test_the_opening_route_key_still_matches_the_recorded_cassette` recomputes `loop-bayarea`'s
opening key and asserts its cassette holds it — against the committed artifact rather than
against a constant a developer can update, and without running the route, so a re-keying is
reported *as* a re-keying rather than as a `CacheMiss` five frames down inside a CLI
invocation. Zero bytes, no graph, no JVM.

**`CachedRouter.map_match`'s key has the same latent problem** and is left alone here. It
omits `details`, so asking `/match` for more than `osm_way_id` would be served a recording
that has less. Relevant because a measurement taken while writing this ADR found that
`POST /match?instructions=true` **does** return instructions on GraphHopper 11 — which would
make a cue sheet's frame shift structurally zero and let it survive a reroute. Taking that
route means giving the match key this same treatment, and it is the natural follow-up rather
than something M10 left undone by accident.
