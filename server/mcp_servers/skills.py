"""MCP stdio server: skill candidate proposal
(experience-consolidation.md §3.3/§3.4).

Thin HTTP shim to `/api/sessions/{sid}/skills/candidates`, like bg/ask/
research. Deliberately exposes only `propose`, `list_pending`, and `diff` —
never `approve`/`reject`: a candidate takes effect only through the human
review queue (the web UI hitting `/api/skills/candidates/{id}/approve`),
never a model call (§4: "no auto-generated skill takes effect").
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s skills-mcp %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP("owlery-skills")


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    if not v:
        logger.error("Required env var %s not set", name)
    return v


def _context() -> tuple[str, str, str] | None:
    api, sid, tok = (
        _env("OWLERY_API_BASE"), _env("OWLERY_SESSION_ID"), _env("OWLERY_AUTH_TOKEN"),
    )
    if not (api and sid and tok):
        return None
    return api.rstrip("/"), sid, tok


def _call(method: str, path: str, *, body: dict[str, Any] | None = None,
          params: dict[str, Any] | None = None) -> tuple[int, Any]:
    ctx = _context()
    if ctx is None:
        return 0, "skills server is misconfigured (required env vars missing)"
    api, _sid, tok = ctx
    try:
        r = httpx.request(
            method, f"{api}{path}", json=body, params=params,
            headers={"Authorization": f"Bearer {tok}"}, timeout=15.0, trust_env=False,
        )
    except httpx.HTTPError as e:
        return 0, f"failed to reach Owlery: {e}"
    try:
        payload: Any = r.json()
    except ValueError:
        payload = r.text[:1000]
    return r.status_code, payload


def _render(status: int, payload: Any, *, action: str) -> str:
    if status == 0:
        return f"Error: {payload}"
    if status >= 400:
        detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
        return f"Error: cannot {action} ({status}): {detail}"
    import json
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


@mcp.tool(name="propose")
def propose(
    slug: str, title: str, description: str, body_markdown: str, rationale: str,
    scope: str = "agent+repo", bundle_files: dict[str, str] | None = None,
) -> str:
    """Propose a skill candidate for human review (experience-consolidation.md
    §3.3 point 3: "a repeatable multi-step process becomes a skill candidate").

    Landing does NOT happen here — this only files a `pending` row a human
    reviews via the web UI (approve/reject + diff + file tree), same shape
    as hermes' `/skills pending`. Nothing changes on disk until a human
    approves it.

    Args:
        slug: lowercase-kebab-case identifier, e.g. "hermes-pr-flow". Reusing
            an existing approved slug proposes a REPLACEMENT for that skill.
        title: short human title.
        description: one paragraph — becomes the SKILL.md frontmatter
            `description` a future session sees when deciding whether to load
            this skill.
        body_markdown: the full SKILL.md content (YAML frontmatter + body).
        rationale: why this is worth keeping — what went wrong or took too
            long the first time, and why it'll recur.
        scope: "agent+repo" (default — visible only in the repository you're
            proposing from) or "agent-global" (visible in every repository
            this agent works in, no repo fingerprint). The reviewer can
            change this on approve (experience-consolidation-v2.md §3③).
        bundle_files: optional `{relative_path: content}` map of extra files
            (scripts/templates/examples/tests) alongside SKILL.md — read
            their content yourself (you're in the room and already have it)
            and pass it here; there is no separate file-upload step. Paths
            must be relative and may not escape the skill directory or reuse
            the reserved name `SKILL.md`.
    """
    ctx = _context()
    if ctx is None:
        return "Error: skills server is misconfigured (env vars missing)."
    _api, sid, _tok = ctx
    body: dict[str, Any] = {
        "slug": slug, "title": title, "description": description,
        "body_markdown": body_markdown, "rationale": rationale,
        "scope": scope, "bundle_files": bundle_files or None,
    }
    task_id = os.environ.get("OWLERY_TASK_ID")
    run_id = os.environ.get("OWLERY_TASK_RUN_ID")
    if task_id and run_id:
        body["task_id"] = task_id
        body["run_id"] = run_id
    code, data = _call("POST", f"/api/sessions/{sid}/skills/candidates", body=body)
    return _render(code, data, action="propose skill candidate")


@mcp.tool(name="list_pending")
def list_pending() -> str:
    """List skill candidates awaiting human review."""
    code, data = _call("GET", "/api/skills/candidates", params={"status": "pending"})
    return _render(code, data, action="list pending skill candidates")


@mcp.tool(name="diff")
def diff(candidate_id: str) -> str:
    """Show a candidate's proposed content and its diff against whatever is
    currently landed at that slug (empty baseline for a brand-new slug)."""
    code, data = _call("GET", f"/api/skills/candidates/{candidate_id}")
    return _render(code, data, action="diff skill candidate")


if __name__ == "__main__":
    mcp.run()
