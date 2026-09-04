"""Long-run planner.

Layering (scope section 4.1), strictly one direction:

    cli/ api/ tools/ agent/  ->  core/  ->  adapters/

core never imports agent, tools, or cli. Nothing outside core.routing knows which router
is in use. Nothing outside adapters knows which jurisdiction it is in.
"""

__version__ = "0.0.0"
