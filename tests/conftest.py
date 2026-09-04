"""Shared fixtures.

Golden and contract tests must not touch the network: adapters are served from recorded
cassettes, and the router is either a local GraphHopper container or a recorded response.
Anything that genuinely needs a live service is marked `network` and skipped by default.
"""
