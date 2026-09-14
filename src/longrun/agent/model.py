"""One place where a model is built, and one place where a call is charged for.

Scope 4.1: "the LLM is called at fixed points with structured outputs (Pydantic schemas,
or Pydantic AI as the call wrapper)". Pydantic AI, for the reason ADR 0015 gives - it
carries `TestModel`, `FunctionModel` and a global `ALLOW_MODEL_REQUESTS` that makes "a test
called a model" a loud, specific failure rather than a slow one.

Every call site goes through `ask`, which does three things and nothing else: charge the
budget *before* the call, run it, and turn any failure into `None`. `None` is what every
call site degrades on, and ADR 0015 requires each of them to have somewhere to degrade to -
so a model that is absent, refusing, over budget or wrong costs a plan its enrichment and
never its plan.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from longrun.core.models.context import Budget

#: Read at call time, never at import - `conftest._clear_longrun_env`'s rule, and the
#: reason `adapters/keys.py` gives: a module-level read makes the value a property of when
#: the process started.
MODEL_ENV_VAR = "LONGRUN_MODEL"
KEY_ENV_VAR = "ANTHROPIC_API_KEY"

#: Tier-4 extraction does not get a better model, because it does not get a better
#: outcome: `MAX_EXTRACTION_CONFIDENCE = 0.5` caps what any model may claim there.
DEFAULT_MODEL = "anthropic:claude-sonnet-5"


class ModelUnavailable(RuntimeError):
    """No model is configured, and the caller asked for one anyway."""


@dataclass(frozen=True)
class ModelSettings:
    """What to call and whether it can be called at all."""

    name: str = DEFAULT_MODEL
    key: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.key)

    @property
    def missing_reason(self) -> str:
        return (
            f"{KEY_ENV_VAR} is not set; the four LLM call sites degrade to their "
            "deterministic paths (ADR 0015)"
        )


def settings_from_env(env: dict[str, str] | None = None) -> ModelSettings:
    source = os.environ if env is None else env
    return ModelSettings(
        name=source.get(MODEL_ENV_VAR, "").strip() or DEFAULT_MODEL,
        key=source.get(KEY_ENV_VAR, "").strip() or None,
    )


def build_agent[T](
    output_type: type[T], instructions: str, settings: ModelSettings | None = None
) -> Any:
    """A pydantic-ai agent with a structured output type.

    Imported inside the function: `pydantic-ai` is the `agent` extra and `core/` must
    import on a bare `uv sync`, so nothing above may pay for it at import time either.
    """
    from pydantic_ai import Agent

    settings = settings or settings_from_env()
    if not settings.available:
        raise ModelUnavailable(settings.missing_reason)
    return Agent(settings.name, output_type=output_type, instructions=instructions)


def ask(
    agent: Any,
    prompt: str,
    *,
    budget: Budget | None = None,
) -> Any | None:
    """Run one call at a fixed call site, or return `None`.

    The budget is charged **before** the call, not after: a call that was made and then
    failed still cost what it cost, and a counter that only records successes understates
    exactly the runs a reader would want to look at.
    """
    from longrun.core.models.context import BudgetExceeded

    if budget is not None:
        try:
            budget.spend_model_call()
        except BudgetExceeded:
            return None
    try:
        result = agent.run_sync(prompt)
    except Exception:  # noqa: BLE001 - a model is an enrichment; it may not take a plan down
        return None
    if budget is not None:
        budget.model_tokens_used += _tokens(result)
    return result.output


def _tokens(result: Any) -> int:
    """Whatever the provider reported, or nothing. Recorded, never capped.

    Refusing a long answer halfway through is a worse failure than an expensive one, so
    this is a number for the manifest rather than a limit for the loop.
    """
    try:
        usage = result.usage()
        return int(getattr(usage, "total_tokens", 0) or 0)
    except Exception:  # noqa: BLE001 - usage reporting is not worth a failed plan
        return 0


__all__ = [
    "DEFAULT_MODEL",
    "KEY_ENV_VAR",
    "MODEL_ENV_VAR",
    "ModelSettings",
    "ModelUnavailable",
    "ask",
    "build_agent",
    "settings_from_env",
]
