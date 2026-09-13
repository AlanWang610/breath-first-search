"""The only place state- and city-specific code lives.

One subpackage per jurisdiction, e.g. ca/ for California (Caltrans 511, CA E&TS speed
surveys, regional park districts). Nothing here is imported by core; adapters are reached
through the registry.
"""
