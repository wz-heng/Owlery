"""Skill candidate review queue (experience-consolidation.md §3.4/§5).

Hermes-style shape: an agent PROPOSES a candidate SKILL.md; it starts
`pending`; a human APPROVES or REJECTS it — never the proposing agent (§4:
"no auto-generated skill takes effect"). An `approved` row IS the landed
skill; there is no separate "skills" table, and use_count/last_used accrue
on the same row as later sessions invoke it, mirroring hermes' single skill
entity with a `.usage.json` sidecar.

Landing (on approve) writes the SKILL.md into a throwaway worktree off the
target repo, commits it there, then removes the worktree — leaving only a
new local branch behind. It deliberately never pushes or opens a PR: that
stays a human's separate, explicit action, exactly like any other
Owlery-authored branch (CLAUDE.md: "commit to the branch, default no push").
"""

from __future__ import annotations

import difflib
import logging
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .task_board import workspaces as ws

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class SkillRegistryError(RuntimeError):
    """Base class for stable errors consumed by REST and MCP."""

    code = "skill_registry_error"


class SkillNotFoundError(SkillRegistryError):
    code = "not_found"


class SkillValidationError(SkillRegistryError):
    code = "validation"


class SkillConflictError(SkillRegistryError):
    code = "conflict"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SkillRegistry:
    """Bound once at boot (main.py), mirrors research_manager/delegation_manager."""

    def __init__(self) -> None:
        self.db: Any = None
        self.session_mgr: Any = None

    def bind(self, *, db: Any, session_mgr: Any) -> None:
        self.db = db
        self.session_mgr = session_mgr

    def _require_bound(self) -> None:
        if self.db is None:
            raise RuntimeError("SkillRegistry is not bound")

    async def _resolve_repository(self, working_dir: str) -> str:
        """The stable repo root shared by every worktree of the same repo,
        resolved fresh so a candidate proposed from an ephemeral Task Board
        worktree still lands correctly after that worktree is torn down."""
        rc, out, err = await ws._git(
            "rev-parse", "--path-format=absolute", "--git-common-dir",
            cwd=working_dir,
        )
        if rc or not out:
            raise SkillValidationError(
                err or "skill candidates require a Git repository"
            )
        return str(Path(out).resolve().parent)

    async def propose(
        self,
        *,
        session_id: str,
        slug: str,
        title: str,
        description: str,
        body_markdown: str,
        rationale: str,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_bound()
        slug = (slug or "").strip().lower()
        if not _SLUG_RE.match(slug):
            raise SkillValidationError(
                "slug must be lowercase kebab-case, e.g. 'hermes-pr-flow'"
            )
        title = (title or "").strip()
        description = (description or "").strip()
        rationale = (rationale or "").strip()
        if not title:
            raise SkillValidationError("title is required")
        if not description:
            raise SkillValidationError("description is required")
        if not (body_markdown or "").strip():
            raise SkillValidationError("body_markdown is required")
        if not rationale:
            raise SkillValidationError("rationale is required")
        session = (
            self.session_mgr.sessions.get(session_id) if self.session_mgr else None
        )
        if session is None:
            raise SkillValidationError(f"session {session_id!r} is not live")
        repository = await self._resolve_repository(session.working_dir)
        candidate_id = uuid.uuid4().hex[:12]
        return await self.db.create_skill_candidate(
            candidate_id=candidate_id,
            slug=slug,
            title=title,
            description=description,
            body_markdown=body_markdown,
            repository=repository,
            rationale=rationale,
            proposed_by_agent_id=session.agent_id,
            proposed_by_session_id=session_id,
            task_id=task_id,
            run_id=run_id,
            created_at=_now_iso(),
        )

    async def list_candidates(
        self, *, status: str | None = None
    ) -> list[dict[str, Any]]:
        self._require_bound()
        return await self.db.list_skill_candidates(status=status)

    async def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        self._require_bound()
        candidate = await self.db.get_skill_candidate(candidate_id)
        if candidate is None:
            raise SkillNotFoundError(f"skill candidate {candidate_id!r} not found")
        return candidate

    async def diff(self, candidate_id: str) -> dict[str, Any]:
        """A unified diff of the candidate's body against the currently
        landed skill at the same slug, if any (empty baseline for a
        brand-new slug). Compares against the DB's own record of what was
        landed, not the filesystem — the landing branch is deliberately
        never merged/checked out automatically, so the live working tree
        would not reflect it."""
        candidate = await self.get_candidate(candidate_id)
        landed = await self.db.get_latest_approved_skill_by_slug(candidate["slug"])
        before = (
            landed["body_markdown"]
            if landed is not None and landed["id"] != candidate_id
            else ""
        )
        after = candidate["body_markdown"]
        rel = f".claude/skills/{candidate['slug']}/SKILL.md"
        diff_text = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{rel}" if before else "/dev/null",
                tofile=f"b/{rel}",
            )
        )
        return {"candidate": candidate, "diff": diff_text}

    async def approve(
        self, candidate_id: str, *, review_note: str | None = None
    ) -> dict[str, Any]:
        self._require_bound()
        candidate = await self.get_candidate(candidate_id)
        if candidate["status"] != "pending":
            raise SkillConflictError(f"candidate is already {candidate['status']}")
        landed = await self._land(
            candidate["repository"], candidate["slug"], candidate["body_markdown"],
            candidate_id,
        )
        result = await self.db.review_skill_candidate(
            candidate_id,
            status="approved",
            review_note=review_note,
            reviewed_at=_now_iso(),
            landed_path=landed["path"],
            landed_branch=landed["branch"],
            landed_commit=landed["commit"],
        )
        assert result is not None
        return result

    async def reject(self, candidate_id: str, *, review_note: str) -> dict[str, Any]:
        self._require_bound()
        candidate = await self.get_candidate(candidate_id)
        if candidate["status"] != "pending":
            raise SkillConflictError(f"candidate is already {candidate['status']}")
        if not (review_note or "").strip():
            raise SkillValidationError("review_note is required to reject a candidate")
        result = await self.db.review_skill_candidate(
            candidate_id,
            status="rejected",
            review_note=review_note.strip(),
            reviewed_at=_now_iso(),
        )
        assert result is not None
        return result

    async def record_usage(self, slug: str) -> None:
        """Best-effort use_count/last_used tracking (§5): never raises, so a
        skill invocation can never break the turn that made it."""
        if self.db is None:
            return
        try:
            candidate = await self.db.get_latest_approved_skill_by_slug(slug)
            if candidate is None:
                return
            await self.db.record_skill_candidate_usage(
                candidate["id"], used_at=_now_iso()
            )
        except Exception:
            logger.exception("failed to record skill usage for %r", slug)

    async def _land(
        self, repository: str, slug: str, content: str, candidate_id: str
    ) -> dict[str, str]:
        branch = f"owlery/skill-{slug}-{candidate_id[:8]}"
        scratch = tempfile.mkdtemp(prefix=f"owlery-skill-{slug}-")
        try:
            rc, _, err = await ws._git(
                "worktree", "add", "-b", branch, scratch, "HEAD", cwd=repository
            )
            if rc:
                raise SkillValidationError(err or "unable to create a landing worktree")
            target = Path(scratch) / ".claude" / "skills" / slug / "SKILL.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            result = await ws.commit_all(
                scratch,
                author_name="Owlery Skill Registry",
                author_email="owlery-skills@localhost",
                message=f"skill: land candidate {slug} ({candidate_id})",
            )
            if not result["committed"]:
                raise SkillValidationError(
                    "approving this candidate would not change anything on disk"
                )
            commit = result["head"]
        finally:
            await ws.remove_git_worktree(repository, scratch, force=True)
        return {
            "path": f".claude/skills/{slug}/SKILL.md",
            "branch": branch,
            "commit": commit,
        }


skill_registry = SkillRegistry()
