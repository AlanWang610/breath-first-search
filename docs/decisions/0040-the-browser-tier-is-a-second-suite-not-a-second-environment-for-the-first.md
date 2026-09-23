# 0040 — The browser tier is a second suite, not a second environment for the first

Status: **accepted**, M15, 2026-09-23.

> Numbered 0040 rather than 0037: M14 and M16 run in parallel from the same `main` and hold
> 0038–0039 and 0042 upward, and 0037 is on a pull request this branch cannot see.
> `ls docs/decisions/` here ends at 0036.

## Context

M12 shipped five write gestures — click to select, place a via, draw an avoid polygon,
replace a stretch, scrub the timeline — and gave them **no** automated coverage. It said so
in the PR and again in `ui/README.md`:

> Nothing verifies a gesture. A click that selects a segment, a click that places a polygon
> corner, a drag on the MapLibre canvas, the popup on hover, the camera staying put across a
> write — none of those have automated coverage, here or anywhere, and a headless run cannot
> give them any. […] Mounting a component to assert that it rendered would have raised the
> number without changing that sentence, so there is none.

That was the right call at the time and the second sentence of it was wrong. A headless run
*can* give them coverage; M15.1 measured it before anything was built on the assumption —
twenty-five consecutive Chromium launches resolving a `queryRenderedFeatures` click against
a rendered line layer, twenty-five clean, first launch 2.8 s and the rest 550–660 ms, on
ANGLE over SwiftShader.

So the question M15 actually had to decide is not *whether* to test the gestures but **where
those tests live**, and the existing node tier makes that a real decision rather than a
default. `vite.config.ts` says:

```
// `node`, not `jsdom`: everything under test is a function of a plan and returns a
// value, and a DOM would invite tests of components that a headless run cannot honestly
// check anyway.
test: { environment: "node", include: ["src/**/*.test.ts"] },
```

Three options were on the table.

**Switch the existing suite to `jsdom` or `happy-dom`.** This is the cheapest to set up and
it is the one that cannot work. Neither has WebGL. MapLibre would have to be mocked, and a
mocked map is precisely the thing that cannot answer the question every one of these
gestures asks: `queryRenderedFeatures(event.point, { layers: ["segments"] })` reads the
*rendered frame*, not the source data. A test against a mock would assert that a handler
called a function — which is the "mounting a component to assert that it rendered" M12
refused, arrived at through a config change instead of a component.

**Vitest browser mode.** One runner, one config, one `npm test`. It puts the browser suite
and the node suite behind the same command, which is the problem: the two have different
costs, different failure modes and different reasons to be believed, and a single green tick
over both hides which one is which. It would also have meant the `include` glob growing to
collect `.test.tsx`, so the node tier's boundary would have become a convention rather than
a setting.

**A separate suite with its own runner.** More moving parts, and the only one where the
node tier's promise survives intact.

## Decision

**Two suites. `npm test` is unchanged and keeps `environment: "node"`; the browser tier is
Playwright under `ui/gestures/`, reached by `npm run test:gestures`, and runs in its own CI
job.**

Four parts.

**1. `vite.config.ts` is not edited.** `environment: "node"` and
`include: ["src/**/*.test.ts"]` stay exactly as M12.8 set them, so a `.test.tsx` is still
not collected and a mounted component still cannot reach the node tier by accident. The
browser tier is additive; it took nothing away and it renamed nothing.

**2. Chromium only, headless, and no retries.** One rasteriser to keep honest rather than
three, for behaviour that is the application's rather than the browser's. `retries: 0` is in
the config with the reason written next to it: a retry is how a flaky browser job becomes
one everybody learns to re-run, and a green tick nobody believes is worth less than the
honest paragraph in `ui/README.md` that this milestone replaced.

**3. It drives the shipped arrangement, not a development one.** `npm run build` and then
`longrun api --ui ui/dist` — one origin, one server, the command `ui/README.md` tells a
reviewer to run and the one the gestures were exercised by hand against. The alternative,
vite's dev server with its `/api` proxy, would test a two-origin arrangement that exists
only while somebody is developing.

**4. Its own CI job.** Not a step on `ui`, which is `npm ci` plus two commands over pure
functions and finishes in under a minute; this one needs Python, uv, the golden fixtures, a
Chromium download and a server. Not a step on `test`, whose matrix has no headroom and whose
Windows leg is primary for path, locale and CRLF failures a browser has nothing to do with.

## Consequences

**Two commands in the gate, and that is the point.** `npm test` says whether the functions
of a plan still return what they should; `npm run test:gestures` says whether the page still
works. When one goes red the other says where to look, which a single suite could not.

**The node tier can still be wrong in a way it cannot detect, and M15 proved it.** Twenty
passing vitest cases agreed that `tierColour("safety")` returns red while every flag the
server has ever sent carries `tier: 0`, because the fixtures are built from the same
hand-written types the code reads — the exact failure `api.ts`'s own docstring describes
about `TradeOff.options`. The browser tier is where a type can be checked against a plan the
server actually produced, and `contract.spec.ts` is four small cases that do that.
Deliberately four and not a walk of the whole response: a complete check would be the second
schema `api.ts` argues against, and would fail on the first key a scorer added that the UI
does not draw.

**A first run pays for a browser.** `npx playwright install --with-deps chromium` is roughly
200 MB and a minute of CI. It is cached between runs by nothing in this workflow, which is
accepted: the job's budget is 20 minutes against a measured ~70 seconds of build, seed and
tests, and a cache for a download that changes once a quarter is a maintenance surface for a
saving nobody has missed.

**Traces are uploaded on failure and only on failure.** A browser failure that cannot be
reproduced locally is the reason this tier gets a reputation; a trace is how somebody who
did not write it finds out what happened without re-running it until it fails again.

## What this does not decide

**Whether the node tier should ever mount a component.** It still should not, for M12.8's
reason, but nothing here enforces that beyond the `include` glob. If a future milestone
wants a component test, the honest place is this tier, where the component runs in a browser.

**Whether a second browser is ever worth it.** Firefox and WebKit have their own software
rasterisers and their own quirks, and none of the behaviour under test is browser-specific.
The day one of these tests fails for a reason that is the browser's, that is the evidence
this decision would be revisited on.
