"""The scope 8 planning loop as explicit code. No graph-workflow framework (scope 4.2).

Control flow is known in advance, so the loop is a loop. The LLM is called at fixed points
with structured outputs: parse intent, choose among same-tier alternatives, write
trade-off explanations, draft tier-4 extractions, propose preference updates. It is never
handed the whole tool bag to sequence freely, and it never draws routes.

Human-in-the-loop pauses persist the scratchpad and return needs_input; the job resumes
from the scratchpad.

Planned modules:
    loop.py          the ten steps of scope 8.1, with the 5-round iteration cap
    intent.py        request -> structured constraints and per-run preference overrides
    tradeoffs.py     one-line comparisons for same-tier alternatives left to the user
    profile_updates.py  proposed field/value from a conversational statement, confirmed
                     before saving; stated always overrides inferred
    budget.py        latency target, external API and imagery caps, ray-cast degradation
    prompts/         prompt text, versioned alongside the schemas they produce
"""
