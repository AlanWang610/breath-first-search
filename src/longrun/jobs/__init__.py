"""Async job runner with progress events (scope 4.4).

Plans run as jobs so the CLI, MCP server, and web UI all drive the same execution and see
the same progress stream. A needs_input pause is a job state, not an exception.
"""
