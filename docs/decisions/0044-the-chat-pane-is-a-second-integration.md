# 0044 — The chat pane is a second integration, not a panel

Status: **accepted**, M16, 2026-09-23.

## Context

Scope §10.3 lists a **chat pane** among the web UI's surfaces: *"wired to the same agent, so
elicitation and explanations happen next to the map rather than instead of it"*. §10.2 gives
the reason — chat is the right surface for elicitation, same-tier trade-off questions and
preference updates, with the agent citing the segment ids the plan sheet uses.

It has now been deferred three times, and only the third deferral gave a reason:

* **M0**, in the original `ui/README.md`: listed among the surfaces, with nothing scaffolded.
* **M7.3** (`a001f19`), which wrote the README's first "Not built" section and put the chat
  pane in it as *"missing endpoints and map interactions, not missing capability"*.
* **M12**, which built the direct-manipulation half of that sentence and, in doing so,
  discovered the other half was wrong. The README now reads: *"The chat pane, which wants the
  MCP session and the browser talking to one agent. `api/` reaches `tools/` only by importing
  the same `core/` functions those tools wrap, never through `MCPServer` itself — that is a
  second integration, not a panel."*

Three deferrals with one real argument between them is what earns a decision rather than a
fourth line in a README.

## Decision

**Not built, and the blocker is an architectural claim rather than an effort estimate.**

**"The same agent" is the load-bearing phrase, and there is no agent to be the same as.**
Scope §10.3 does not ask for a chat box that posts to a new endpoint; it asks for the pane to
be wired to *the agent*, so that a trade-off question raised by `arbitrate` can be answered in
the pane and a `lock` applied from the reply. That agent is `agent/loop.py`, driven over MCP
by a client this project does not own. A pane that talked to its own second agent would not
be the same agent, and the two would disagree about the plan the moment either wrote to it.

**`api/` does not reach `tools/`, and the resemblance is a coincidence of imports.** Both
layers call the same `core/` functions; neither calls the other. `api/app.py` states the rule
it is built on — *"no capability lives here"*, every handler being argument parsing, a call
into `jobs/` or `core/`, and a JSON response. So there is no `MCPServer` instance behind the
API to attach a session to. Wiring one is a new integration boundary between two processes,
with its own session lifetime, its own auth story and its own failure modes — the third such
boundary in the project, alongside the CLI and the MCP client.

**The part the project could build alone is the part that is already built.** Elicitation
questions, trade-off questions and preference proposals are already *plan state*:
`Arbitration.needs_input`, `TradeOff`, the `needs_input` job status and
`POST /api/jobs/{id}/resume` exist and are reachable from a browser that was closed and
reopened. A pane that rendered those and posted structured answers is a real and useful
thing, and it is **not what §10.3 asks for** — it is a question panel, and calling it a chat
pane would be the same over-claim M7.3's own README section was written to avoid.

**ADR 0015's rule applies to the remainder.** *"A call site with no deterministic answer is
not a call site, it is a dependency."* Free-text chat — "avoid Foothill Expressway", "I don't
mind sun when it's cool" — is a model call site by definition, and §4.1 fixes the project to
five of them, all of which are already spent and metered (`model_calls_max = 12`). A chat pane
is an *unbounded* sixth: one call per user turn, with no ceiling the budget could express,
inside a process whose whole design is that a plan's external cost is known in advance.

## Consequences

* **`ui/README.md`'s "Not built" section stays accurate and now has somewhere to point.** The
  sentence it carries is the summary; this document is the argument.
* **The UI is six of scope §10.3's eight surfaces, and says so.** Neither this nor the
  re-route endpoint is a gap in capability: both are integrations the hermetic suite cannot
  reach, which is the same reason the re-route endpoint is absent (it needs GraphHopper).
* **No dependency is added.** A chat pane implies a streaming transport and a model client in
  `api/`, and `api/` has neither. `async def` appears at exactly two boundaries today
  (ADR 0016) and a streaming chat endpoint would be a third.
* **Elicitation still happens**, over MCP in a chat client, which is where §10.2 actually
  puts it. What is missing is the *adjacency* to the map, not the conversation.

## What would make us revisit

**A question panel proving the demand.** Rendering `needs_input` jobs and their `TradeOff`s in
the browser, with structured answers posted back to `POST /api/jobs/{id}/resume`, needs no new
integration and no model call. If people use it and then ask to type at it, the case for the
real thing is evidence rather than a scope line — and that is the cheapest experiment
available.

**A ceiling for a conversational call site.** §6.4's budget counts calls per *plan*, and a
chat pane's calls belong to a *session*. Until there is a defensible meter for that, ADR
0015's objection stands on its own.

**A first-party MCP client in the browser.** If the project ever ships one, the session the
pane needs exists as a side effect and this decision is mostly cost rather than structure.
