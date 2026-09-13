"""Router interface plus the GraphHopper adapter (scope 4.3, 7.1).

The only package that knows GraphHopper exists. The interface exposes route,
alternatives, and map_match; the costing model is an opaque object this adapter owns, so
the router can be swapped without touching scorers or the agent.

Planned modules:
    base.py          Router protocol; NoRouteError carrying the nearest-connected failure
    graphhopper.py   HTTP client; flexible/LM mode so custom models are query-time
    custom_model.py  priority terms (LTS multipliers, unpaved, path bonus, sidewalk gap),
                     hard excludes, and the profile-driven surface and hills terms
    lts.py           offline LTS 1-4 per way (Furth), written as an encoded value at import
    tuning.py        fit the ~6 priority parameters against pairwise route preferences
"""
