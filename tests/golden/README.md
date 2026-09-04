# Golden routes

One directory per route under `routes/`:

```
routes/<name>/
  request.yaml      the plan request (or route.gpx for a repair-mode case)
  profile.yaml      the preference profile in force, pinned
  snapshot.json     data-snapshot pins, so the expectation is reproducible
  expected.json     per-segment measurements, flags, and the worst-N lists
```

Run through the CLI, no model in the loop. When a scorer changes on purpose, the diff in
`expected.json` is the review artifact.
