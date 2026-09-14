# 0016 — Jobs are threads, not asyncio

Status: accepted (2026-09-12)

## Context

Scope §4.4 says "plans run as async jobs with progress events" and names no mechanism.
§10.3 adds that the web UI runs "FastAPI over the async job runner (same jobs the CLI and
MCP server use)", and `jobs/__init__.py` has said since the scaffold that "a `needs_input`
pause is a job state, not an exception".

"Async" in that sentence is doing two different jobs: *concurrent* — several plans in
flight — and *non-blocking* — a caller that is not stuck while one runs. Only the second
implies `asyncio`, and the decision is which reading to build.

What the codebase actually is:

* `core/` is ~120 files and contains **no `async def`, no `await`, no `asyncio`** at all.
  Every scorer, store, cache and router call is synchronous.
* The MCP SDK at the locked version (`mcp` 2.1.1) runs a synchronous tool function through
  `anyio.to_thread.run_sync` (`mcp/server/mcpserver/utilities/func_metadata.py`). A sync
  `core/` function is already a first-class MCP tool and already does not block the loop.
* `pytest-asyncio` is installed with `asyncio_mode = "auto"` and there is not one `async`
  test in the repository.

So converting `core/` to `async` would touch every call site in the project to buy one
thing the transport already provides.

## Decision

**`jobs/` is a `ThreadPoolExecutor`. `core/` stays synchronous. `async def` appears only at
the MCP boundary, where the SDK puts it.**

`JobRunner.submit` returns a job id immediately; `events()` is the progress stream;
`wait()` blocks for callers that want to. `JobStore` is a directory of scratchpad JSON
files, because scope §4.4 already says the scratchpad plus the manifest is the stored plan
and a second store would be a second thing to keep in step.

**One job is one thread, and candidates within a job are scored sequentially.** This is the
load-bearing half and it is not a performance note:

* `Budget.spend_api_call` is load–add–store. Two threads can both pass the cap check and
  both spend, and scope §6.4's 200-call ceiling stops being a ceiling.
* `SqliteCache` holds one connection with `check_same_thread=False`, and `fetch` is a
  read-then-write. Two threads can miss the same key, both call the producer, and both pay
  for it — so the cassette mechanism stops being one.

Parallelising the candidate sweep is the obvious optimisation, it would make both of those
true, and it is therefore forbidden. Concurrency belongs *between* jobs, where each has its
own budget and its own connection.

## Consequences

**The hermetic suite stays synchronous**, which matters more than it sounds: a test that
awaits is a test that needs an event loop fixture, and every existing test would have had
to grow one.

**A runner must be shut down.** `conftest._block_network` patches `socket.connect` and
restores it at teardown, so a worker that outlives the test that started it runs with the
network *unblocked* — and a later failure then looks like a flake in whatever ran next.
The fixture is a context manager for that reason.

**M7's FastAPI does the same thing the MCP boundary does** — `await
anyio.to_thread.run_sync(...)` — and adds no capability, which is what §3.9 asks of it.

**A non-obvious cost, paid immediately.** The accessors nest (`events` takes the lock and
then asks `_job`, which takes it again), and `threading.Lock` is not reentrant, so the
first version deadlocked. The suite *hung* rather than failed, which is the worse way to
find out and the reason this is written down: the lock is an `RLock`.

## Revisit when

Scope §4.2 already names the trigger for its sibling decision, and the same one applies
here: **durable execution across many concurrent users**. A thread pool in one process is
not that. Until then, the thing that would make `asyncio` pay — thousands of mostly-idle
connections — is not what this is.
