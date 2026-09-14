"""Tier 4: LLM search-and-extract from agency pages and PDFs.

The fallback when no registered adapter covers a jurisdiction. Output carries confidence
<= 0.5 and a source URL, and is marked unverified in the coverage manifest. This is the
one place in adapters/ where a model is in the loop.
"""
