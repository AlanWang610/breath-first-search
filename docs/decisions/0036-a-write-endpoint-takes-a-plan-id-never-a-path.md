# 0036 — A write endpoint takes a plan id, never a path

Status: **accepted**, M12, 2026-09-22.

> Numbered 0036 rather than 0034: M13 landed in parallel from `main` and took 0034 and
> 0035. `ls docs/decisions/` on this branch does not show them, because this branch is off
> M11 and M13 is off `main`.

## Context

`src/longrun/tools/` is file-path-parameterised throughout, by design and by scope §7:
`import_route(gpx_path)`, `gpx_verify(gpx_path)`, `lock_segment(scratchpad_path, ...)`,
`export_sheet(plan_path, out_path)`. That is the right shape for a tool an agent calls in
a shell it already has the run of — the path is how the caller names the thing they are
working on, and there is no privilege boundary between the caller and the files.

`src/longrun/api/` has a boundary, and until M12 it had exactly one write: `POST
/api/jobs/{id}/resume`, which takes a job id and a string that must be one of the options
already on the stored question. Everything else was a read.

`_plan_path(plans, plan_id)` was the only traversal guard in the codebase, and its
docstring says what it is for: "`plan_id` arrives from a URL. Without this check
`../../etc/passwd` reads a file, which is the one way a read-only local API can still be
dangerous." M12 makes that sentence's qualifier false. Scope §10.3's five gestures are
five writes, and the shortest path from `tools/` to an endpoint is to keep the parameter
that `tools/` already takes.

There is a second, quieter pressure in the same direction. `longrun edit choose` takes
`--alternative path/to/line.gpx`, and the obvious HTTP translation of that command is a
body with a filename in it.

## Decision

**Every write endpoint identifies its subject by an id, and the id is resolved to a path
by the server. No request body or path parameter on `api/` names a file, anywhere.**

Three parts.

**1. One resolver, `_plan_id_dir`, and every write goes through it.** It refuses an empty
id, a leading `.`, and any of `/`, `\`, `:` or `\x00`; then it resolves the candidate and
requires the resolved plans directory to be among its parents. Two checks, because neither
is sufficient: the character list is what somebody thought of, and containment is what
catches what nobody did.

**2. `choose` takes geometry, not a filename.** The alternative line arrives as a list of
`'lat,lon'` strings — the spelling `PlanSubmission` already uses for `start`, `end` and
`via` — and `core.plan.edits.splice` receives a `Route` built from them. A GPX file on the
server's disk is not addressable from the browser at all, which is the point.

**3. `tools/` does not change.** This is a rule about one boundary and not a new convention
for the project. A tool called over MCP by an agent running as the user, on files that
user can already read and write, is not the same situation as a POST from a page.

## Consequences

**A traversal that had been live since M7 is closed.** The character check alone let
`C:plan` through: it has no separator and no leading dot, and on Windows
`Path("plans") / "C:plan"` is `WindowsPath("C:plan")` — a drive-relative path that leaves
the plans directory entirely. It was a read before this milestone. It would have been a
write after it. `test_the_resolver_refuses_every_id_that_leaves_the_plans_directory` pins
the resolver directly rather than through one endpoint, because a 404 from the endpoint
that happened to be written first proves nothing about the next four.

**The API cannot express one thing the CLI can**, and that is accepted rather than worked
around. `longrun edit choose --alternative track.gpx` reads a file the runner already has;
the browser equivalent is a line they draw. Scope §3.9 requires that every UI control call
a capability the CLI already has, and it says nothing about the reverse — a command may do
things no page can, and "read a file off this disk" is a reasonable member of that set.

**A hand-written `'lat,lon'` list is a wire format with no schema.** It is the same one
`PlanSubmission` has used since M7, so this adds no new spelling, but it does mean a
malformed point is a 422 from a `float()` rather than from pydantic. `_point` raising is
caught at each handler and answered with the input echoed back.

**Ids are opaque and the client must get one from the server.** `GET /api/plans` and a
job's `plan_id` are the only two sources, which is what makes the containment check
sufficient in practice rather than merely correct: a client that constructs an id has
already left the path the UI takes.

## What this does not decide

**Whether `plans/` should be addressable at all.** Today it is a directory the CLI and the
API share by convention, and `_stored_plans` reads whatever is in it. A multi-user
deployment would need ownership per plan and this decision would not survive contact with
it — but nothing in scope §10.3 is multi-user, and inventing an owner for a local tool is
the kind of speculative generality this project has avoided elsewhere.

**How `longrun edit reroute` reaches a browser.** It needs a GraphHopper server, so there
is no endpoint for it in M12 and the UI names the command instead. When there is one it
will take an id like the rest; what it will also need is a router the API is allowed to
reach, and that is a deployment question rather than an addressing one.
