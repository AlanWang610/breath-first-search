"""Drafting an adapter from a tier-4 extraction, for a human to review (scope 7.10).

§7.10's last sentence is the one this module exists for: *"Successful tier-4 extractions can
be promoted to a drafted adapter for human review; this is how the adapter set grows without
hand-writing thousands of scrapers."* There are roughly 19,000 incorporated places in the
United States and nobody is going to write an adapter for each.

**A pure function returning source text.** `draft_adapter` takes a record and returns a
string; a thin CLI writes it. That split is deliberate and it is what makes this testable
with a hand-built extraction, no model and no filesystem - which matters more here than
usual, because the thing being tested is *generated code* and the only cheap assertion worth
making about generated code is that it parses and declares what it claims to.

**A draft is never registered.** It is written to a file, and somebody has to read it, decide
the endpoint is real, and add the entry point by hand. Automatic registration would let a
model's guess about a URL become a tier-1 source of record, which inverts the whole point of
tiers - and `MIN_HARD_FLAG_TIER` would then be guarding nothing, because the draft would
claim whatever tier it was drafted at.

So the drafted tier is the tier the *extraction* had, never better. A human raising it is a
human taking responsibility for the endpoint.
"""

from __future__ import annotations

import ast
import keyword
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.coverage import Tier
    from longrun.core.models.features import FeatureKind
    from longrun.core.models.jurisdiction import Jurisdiction

#: What a drafted adapter may claim. Never better than the extraction it came from, and
#: tier 4 is where extraction lives - so a draft is tier 4 unless a human edits it, and a
#: tier-4 record provably cannot hard-flag a route (ADR 0013).
DRAFT_TIER: Tier = 4


@dataclass(frozen=True)
class DraftedAdapter:
    """Source text for an adapter, and where it should go."""

    module_name: str
    entry_point: str
    source: str
    #: The `pyproject.toml` line a human adds by hand to register it. Not written anywhere
    #: automatically: see the module docstring.
    registration: str


def _safe_text(value: str) -> str:
    """Text fit to interpolate into a generated docstring.

    This module generates *code* from tier-4 output, which is a model reading a web page -
    so its inputs are the least trustworthy in the project. A triple quote in a park name or
    a URL ends the generated docstring early and produces a module that will not import, and
    a draft nobody can open is no use at all. Control characters go for the same reason, and
    a trailing backslash would escape the closing quote.

    Stripped rather than escaped: a draft is for a human to read, and a name carrying a
    triple quote has something wrong with it that a reviewer should see plainly.
    """
    cleaned = value.replace('"""', '"').replace("\\", "/")
    return "".join(ch for ch in cleaned if ch == "\n" or ch >= " ").strip()


def _identifier(value: str) -> str:
    """A jurisdiction id or agency name as a Python identifier.

    `padus:CITY:29` and `tiger:place:2938000` are not identifiers, and neither is "Kansas
    City". A drafted module whose name does not import is a draft nobody can review.
    """
    cleaned = re.sub(r"[^0-9a-zA-Z]+", "_", value).strip("_").lower()
    if not cleaned:
        cleaned = "adapter"
    if cleaned[0].isdigit():
        cleaned = f"j_{cleaned}"
    if keyword.iskeyword(cleaned):
        cleaned = f"{cleaned}_"
    return cleaned


def draft_adapter(
    jurisdiction: Jurisdiction,
    kind: FeatureKind,
    source_url: str,
    *,
    notes: str | None = None,
) -> DraftedAdapter:
    """Source text for an adapter covering one jurisdiction, for a human to finish.

    The `fetch` body is deliberately a `NotImplementedError` rather than a guess at how to
    parse the endpoint. An extraction knows *where* a jurisdiction publishes its closures and
    does not know the shape of what is there; drafting a parser would be drafting the part
    the model has no evidence for, and a plausible-looking wrong parser is worse than an
    obvious blank.
    """
    slug = _identifier(jurisdiction.id)
    safe_name = _safe_text(jurisdiction.name)
    safe_notes = _safe_text(notes) if notes else None
    class_name = "".join(part.title() for part in slug.split("_")) + "".join(
        part.title() for part in kind.split("_")
    )
    entry_point = f"{kind.replace('_', '-')}-{slug.replace('_', '-')}"
    constant = kind.upper()

    body = f'''"""DRAFTED ADAPTER - not registered, not reviewed.

Promoted from a tier-4 extraction for {safe_name} ({jurisdiction.id}).
Source: {_safe_text(source_url)}

**This does not work yet, and that is deliberate.** An extraction found where this
jurisdiction publishes its {kind.replace("_", " ")} and knows nothing about the shape of
what is there, so drafting a parser would be drafting the part with no evidence behind it.
Fill in `fetch`, check the endpoint is what it claims to be, then register it by adding to
`pyproject.toml`:

    [project.entry-points."longrun.adapters"]
    {entry_point} = "longrun.adapters.jurisdictions.{slug}:{constant}"

Raise `tier` only if you have checked the source yourself. A draft stays at tier
{DRAFT_TIER} because a model's guess about a URL must not become a source of record - see
`adapters/promote.py`.
{"" if safe_notes is None else chr(10) + "Notes from the extraction: " + safe_notes + chr(10)}"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from longrun.adapters.base import AdapterResult

if TYPE_CHECKING:  # pragma: no cover
    from datetime import date

    from longrun.adapters.base import AdapterContext

URL = {source_url!r}


class {class_name}:
    name = {f"draft.{slug}.{kind}"!r}
    kind = {kind!r}
    tier = {DRAFT_TIER}
    jurisdictions = ({jurisdiction.id!r},)
    source = {f"draft_{slug}"!r}
    vintage: str | None = None

    def fetch(self, polygon: Any, day: date, ctx: AdapterContext) -> AdapterResult:
        raise NotImplementedError(
            "drafted from a tier-4 extraction and never reviewed; fill this in before "
            "registering the entry point"
        )


{constant} = {class_name}()

__all__ = [{constant!r}, "URL", {class_name!r}]
'''

    return DraftedAdapter(
        module_name=f"longrun.adapters.jurisdictions.{slug}",
        entry_point=entry_point,
        source=body,
        registration=f'{entry_point} = "longrun.adapters.jurisdictions.{slug}:{constant}"',
    )


def drafts_parse(draft: DraftedAdapter) -> bool:
    """Whether a draft is syntactically valid Python.

    The only cheap assertion worth making about generated code, and the one that catches the
    realistic failure: a jurisdiction name with an apostrophe or a newline in it turning the
    module into something that will not import.
    """
    try:
        ast.parse(draft.source)
    except SyntaxError:
        return False
    return True


__all__ = ["DRAFT_TIER", "DraftedAdapter", "draft_adapter", "drafts_parse"]
