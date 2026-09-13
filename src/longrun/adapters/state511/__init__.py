"""Tier 2: state 511 APIs that are not WZDx.

Empty, and the emptiness is a finding rather than an omission. Every 511 system this project
reaches publishes its work zones **as WZDx**, which scope 7.10 calls tier 1 - so 511 SF Bay
and Arizona DOT live in `adapters/wzdx/` beside Missouri's and Kansas's, and are tier 1 for
the same reason they are.

What belongs here is a 511 system's *other* endpoints - AZ511's `/events`, `/alerts`,
`/cameras` - which are a proprietary shape rather than a standard one and do need a key.
Nothing reads them yet.

The mistake this package is left as a marker of: two adapters sat here at tier 2 because
they needed credentials. A credential is a reason a fetch failed. Scope 7.10 assigns the
tier by *source type*, and `MIN_HARD_FLAG_TIER` reads the tier - so conflating the two
changed which sources were permitted to fail a route.
"""
