"""Backend-neutral DTOs + errors for the harness layer.

`HarnessEvent` is the normalized event every harness run emits (the
vocabulary `session_manager` broadcasts on WS). `HarnessCredential` is a
resolved credential ready for a profile to apply at spawn — in one of two
shapes selected by the profile's `credential_style`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HarnessCredential:
    """Resolved credential, ready for a profile to apply to its subprocess.

    Two shapes, picked by the harness profile's `credential_style`:
      - ``env_secret`` (Claude): ``secret`` is a plaintext API key / OAuth
        token, applied as an env var (ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN).
      - ``home_dir`` (Codex): ``home_dir`` is a CODEX_HOME directory holding
        ``auth.json``; ``secret`` is unused.

    ``secret`` is plaintext at this point — decrypted upstream by the
    credential resolver. The profile is responsible for never logging it.
    """

    backend: str            # "claude-code" | "codex"
    auth_type: str          # "api_key" | "oauth"
    secret: str = ""        # plaintext key/token (env_secret style)
    home_dir: str | None = None   # CODEX_HOME dir (home_dir style)


@dataclass
class TokenUsage:
    """Normalized per-turn token consumption (usage-tracking.md §2).

    Backend-neutral vocabulary; each parser maps its CLI's native shape
    into it. `input_tokens` is FRESH input, excluding cache reads —
    Claude reports it that way natively, Codex reports a cache-inclusive
    `input_tokens` the parser subtracts `cached_input_tokens` from.
    `reasoning_tokens` (Codex only) is an informational subset of
    `output_tokens`, never added on top.
    """

    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
            + self.output_tokens
        )


@dataclass
class HarnessEvent:
    """Normalized event emitted by any harness run.

    The vocabulary mirrors what session_manager already broadcasts on WS,
    so the front-end doesn't change when we swap the underlying CLI.
    """

    type: str  # text | thinking | tool_use | tool_result | result | error | question_request | session_started
    content: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    is_error: bool = False
    cost: float | None = None
    session_id: str | None = None  # backend's resume id (carried on `result`)
    duration_ms: int | None = None
    num_turns: int | None = None
    usage: TokenUsage | None = None  # normalized tokens (carried on `result`)
    # Claude's per-model `modelUsage` dict, verbatim (None for Codex / when
    # absent). Persisted as JSON so per-model attribution can be built later
    # without committing to its key names today (usage-tracking.md §8).
    model_usage: dict[str, Any] | None = None
    raw: dict[str, Any] | None = field(default=None, repr=False)


class HarnessOneshotError(Exception):
    """A one-shot (`run_oneshot`) model call failed. `code` is a stable
    machine token (``not_found`` | ``timeout`` | ``failed`` | ``bad_output``
    | ``empty``) the caller maps to a domain-specific, user-facing message
    (e.g. schedule parsing → ScheduleParseError)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def join_text_blocks(blocks: list[str]) -> str:
    """Join captured `text` events back into one body.

    Every consumer that accumulates `text` events (delegation replies,
    research leaves) must use this — NOT `"".join`.

    A `text` event carries one COMPLETE assistant block: Claude ignores
    `stream_event` and emits whole content blocks, and Codex only emits on
    `item.completed`. Neither is a token delta. Blocks end at a sentence
    boundary with no trailing newline, so `"".join` fuses them —
    `...bootstrap.Now I'll...` — collapsing a whole reply into one
    multi-thousand-character line.

    We insert a separator ONLY where the boundary lacks one, and we never
    rewrite a block's interior:

    - Blocks are joined verbatim. Stripping each block would eat
      *meaningful* leading whitespace — an indented Markdown code block
      (`    print(1)`) or a list continuation line would be destroyed.
    - The separator is a single `\\n`, not a blank line. A block boundary is
      not necessarily a paragraph boundary: two blocks of `- first` /
      `- second` are one tight list, and forcing a blank line between them
      would render it as a loose list and change what the reading model sees.
    - A block that already ends in a newline gets no extra separator.

    Only the assembled body is trimmed at its outer edges.
    """
    out: list[str] = []
    for block in blocks:
        if not block:
            continue
        if out and not out[-1].endswith("\n") and not block.startswith("\n"):
            out.append("\n")
        out.append(block)
    return "".join(out).strip()
