"""Model routing: the single seam for resolving *which* model a turn runs
and for rejecting a model that can't run on its backend
(budget-model-routing.md §4).

Two responsibilities, both deliberately centralized here so future routing
strategy lives in one place instead of scattered across call sites:

  * ``resolve_model(session, agent)`` — the priority chain
    ``session.model > agent.model > backend default``. A ``None`` result
    means "let the CLI pick its own default", which is exactly the
    pre-routing behavior, so an un-pinned session is unchanged.

  * ``validate_model_for_backend(backend, model)`` — a cross-family
    *blacklist* (NOT a whitelist). CLIs accept arbitrary model strings and
    new model names ship constantly, so a closed allow-list would reject
    legitimate new values. We only reject the combinations that are
    provably wrong: a Claude model on the Codex backend, or an OpenAI/Codex
    model on the claude-code backend.
"""

from __future__ import annotations

import re
from typing import Any

# OpenAI reasoning models are `o` followed by a digit — o1, o3, o4-mini,
# o1-preview. This is intentionally NOT a bare ``startswith("o")``: Claude's
# ``opus`` alias also starts with "o", and blacklisting it would wrongly reject
# a valid claude-code model.
_OPENAI_O_SERIES = re.compile(r"^o\d")


class ModelBackendError(ValueError):
    """A model string is incompatible with the backend it would run on
    (budget-model-routing.md §4.3).

    Carries a human-readable message; each entry point maps it onto its own
    transport-level error — HTTP 422 (REST create-session / agents), a
    ``DelegationError`` (ask_agent), or a ``TaskValidationError`` (Task
    Board)."""


def resolve_model(session: Any, agent: Any | None) -> str | None:
    """Resolve the effective model for a turn: ``session.model`` wins, then
    the owning ``agent.model``, then ``None`` (the backend's own default).

    This is the one place model selection is decided — the seam any future
    routing policy plugs into (budget-model-routing.md §4.1). ``session`` is a
    Session (anything with a ``.model`` attribute); ``agent`` is the agent row
    dict or ``None``."""
    session_model = getattr(session, "model", None)
    if session_model:
        return session_model
    if agent:
        return agent.get("model") or None
    return None


def validate_model_for_backend(backend: str | None, model: str | None) -> None:
    """Raise :class:`ModelBackendError` when ``model`` obviously can't run on
    ``backend``. No model (``None``/empty) and unknown backends are accepted
    silently — this is a blacklist of known-wrong pairings, not a whitelist
    (budget-model-routing.md §4.3)."""
    if not model:
        return
    m = model.strip().lower()
    if not m:
        return
    if backend == "codex":
        # Claude family on a Codex session — the exact mismatch behind the
        # "codex + claude model → silent empty turn" bug.
        if m.startswith("claude"):
            raise ModelBackendError(
                f"Model {model!r} is a Claude model and cannot run on the "
                f"Codex backend. Pick a Codex/OpenAI model (e.g. "
                f"'gpt-5-codex') or set the backend to 'claude-code'."
            )
    elif backend == "claude-code":
        if m.startswith("gpt") or m.startswith("codex") or _OPENAI_O_SERIES.match(m):
            raise ModelBackendError(
                f"Model {model!r} is an OpenAI/Codex model and cannot run on "
                f"the claude-code backend. Pick a Claude model (e.g. "
                f"'claude-opus-4') or set the backend to 'codex'."
            )
