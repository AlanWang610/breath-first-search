# Architecture decisions

One file per decision that was expensive to make and would otherwise be re-litigated:
`NNNN-short-title.md`, with context, the decision, and what would make us revisit it.

Decisions already made in the scope, to be written up here as they are implemented:

- Router: GraphHopper over Valhalla (scope 4.3)
- No graph-workflow framework for the agent loop (scope 4.2)
- SVF computed per route corridor on first use, not region-wide at build (scope 5)
- Preference elicitation in context, never an up-front questionnaire (scope 6.3)
- Position weight applied in the scoring loop, not the router (scope 8.2)
