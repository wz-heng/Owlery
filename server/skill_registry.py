"""Skill candidate review queue (experience-consolidation.md §3.4/§5).

Hermes-style shape: an agent PROPOSES a candidate SKILL.md; it starts
`pending`; a human APPROVES or REJECTS it — never the proposing agent (§4:
"no auto-generated skill takes effect"). An `approved` row IS the landed
skill; there is no separate "skills" table, and use_count/last_used accrue
on the same row as later sessions invoke it, mirroring hermes' single skill
entity with a `.usage.json` sidecar.

Approval lands the skill TWO ways:

1. A git branch off the target repo (`_land`) — the durable, human-auditable
   diff/commit. Never pushed or merged automatically: that stays a human's
   separate, explicit action, exactly like any other Owlery-authored branch
   (CLAUDE.md: "commit to the branch, default no push"). This alone is NOT
   enough to make the skill invocable — nothing checks that branch out, so a
   future session's real `claude` process never sees the file (Snape review:
   the old e2e touchstone papered over this with a hardcoded fake `Skill`
   tool_use instead of proving real discovery).
2. A real file under the proposing agent's per-(agent, repository) skills
   plugin directory (`_materialize_plugin`) — `--plugin-dir` loads this for
   real on every subsequent turn for that agent in that repository
   (`resolve_plugin_dir`, wired in `session_manager._run_backend`), so a new
   session finds and can invoke the skill through Claude Code's own native
   plugin-skill discovery, no merge action required. Scoped by (agent,
   repository) rather than agent alone because that is exactly the axis
   use_count needs to disambiguate two independently-landed same-slug skills
   (see `get_latest_approved_skill_by_slug`), and it is the natural
   granularity for a `--plugin-dir` argument built from a session's
   (agent_id, working_dir).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .agent_memory import agent_state_dir
from .task_board import workspaces as ws

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)


def _parse_frontmatter(body_markdown: str) -> dict[str, Any]:
    """The SKILL.md frontmatter as a dict, or {} if missing/unparsable."""
    match = _FRONTMATTER_RE.match(body_markdown)
    if not match:
        return {}
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _frontmatter_str(frontmatter: dict[str, Any], key: str) -> str | None:
    value = frontmatter.get(key)
    return value.strip() if isinstance(value, str) else None


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


def _repo_fingerprint(repository: str) -> str:
    """A stable, filesystem-safe identity for `repository`'s absolute path —
    used to key a per-(agent, repository) plugin dir so two repos never
    collide on disk even if they share a skill slug."""
    return hashlib.sha256(repository.encode()).hexdigest()[:16]


def agent_skills_plugin_root(agent_id: str) -> Path:
    """`<agents_dir>/<agent_id>/skills/` — every repository this agent has
    ever landed an approved skill for gets one subdirectory here."""
    return agent_state_dir(agent_id) / "skills"


def agent_skills_plugin_dir(agent_id: str, repository: str) -> Path:
    """The real `--plugin-dir` target for `agent_id` working in `repository`
    (experience-consolidation.md §3.4, Snape review point 1). A Claude Code
    plugin directory: `.claude-plugin/plugin.json` + `skills/<slug>/SKILL.md`
    per landed skill. Materialized on approve, read by
    `resolve_plugin_dir`/`build_turn_argv` on every later turn — no git
    merge or checkout involved, so a brand-new session for this agent in
    this repository discovers the skill through Claude Code's own native
    plugin-skill loading, exactly as required for approval to actually take
    effect."""
    return agent_skills_plugin_root(agent_id) / _repo_fingerprint(repository)


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

    async def resolve_repository(self, working_dir: str) -> str:
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
        frontmatter = _parse_frontmatter(body_markdown)
        frontmatter_name = _frontmatter_str(frontmatter, "name")
        if frontmatter_name != slug:
            raise SkillValidationError(
                "body_markdown's frontmatter `name:` "
                f"({frontmatter_name!r}) must equal slug ({slug!r}) — usage "
                "tracking looks candidates up by this identity"
            )
        frontmatter_description = _frontmatter_str(frontmatter, "description")
        if frontmatter_description != description:
            raise SkillValidationError(
                "body_markdown's frontmatter `description:` "
                f"({frontmatter_description!r}) must equal the description "
                f"argument ({description!r}) — the frontmatter is what a "
                "future session actually sees when deciding whether to load "
                "this skill, so it must be the same text, not a stray copy"
            )
        session = (
            self.session_mgr.sessions.get(session_id) if self.session_mgr else None
        )
        if session is None:
            raise SkillValidationError(f"session {session_id!r} is not live")
        repository = await self.resolve_repository(session.working_dir)
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
        landed = await self.db.get_latest_approved_skill_by_slug(
            candidate["slug"],
            agent_id=candidate["proposed_by_agent_id"],
            repository=candidate["repository"],
        )
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
        if candidate["proposed_by_agent_id"]:
            self._materialize_plugin(
                candidate["proposed_by_agent_id"],
                candidate["repository"],
                candidate["slug"],
                candidate["body_markdown"],
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

    async def record_usage(
        self, slug: str, *, agent_id: str | None = None, repository: str | None = None
    ) -> None:
        """Best-effort use_count/last_used tracking (§5): never raises, so a
        skill invocation can never break the turn that made it.

        `agent_id`/`repository` scope the lookup to the exact candidate the
        invoking session actually has loaded (Snape review point 2) —
        without them, two independently-landed same-slug skills (different
        repos, or different agents) collide and use_count gets attributed to
        whichever was approved most recently instead of the one that was
        really invoked. The caller (session_manager, on a native `Skill`
        tool_use) always has both, since they are exactly what it used to
        build the `--plugin-dir` the invoking turn ran with."""
        if self.db is None:
            return
        try:
            candidate = await self.db.get_latest_approved_skill_by_slug(
                slug, agent_id=agent_id, repository=repository
            )
            if candidate is None:
                return
            await self.db.record_skill_candidate_usage(
                candidate["id"], used_at=_now_iso()
            )
        except Exception:
            logger.exception("failed to record skill usage for %r", slug)

    def _materialize_plugin(
        self, agent_id: str, repository: str, slug: str, content: str
    ) -> Path:
        """Write the real, directly-loadable skill file behind
        `resolve_plugin_dir` (module docstring point 2). Synchronous and
        filesystem-only (no git) — this dir is never committed, so overwrite
        semantics naturally implement "landing a replacement for an existing
        slug"."""
        plugin_dir = agent_skills_plugin_dir(agent_id, repository)
        manifest = plugin_dir / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        if not manifest.exists():
            manifest.write_text(
                json.dumps(
                    {
                        "name": f"owlery-skills-{_repo_fingerprint(repository)}",
                        "description": (
                            "Owlery-approved skill candidates for this agent, "
                            "landed via the human review queue."
                        ),
                    },
                    indent=2,
                )
                + "\n"
            )
        skill_file = plugin_dir / "skills" / slug / "SKILL.md"
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(content)
        return plugin_dir

    async def resolve_plugin_dir(
        self, *, agent_id: str, working_dir: str
    ) -> str | None:
        """The `--plugin-dir` this agent's turn in `working_dir` should load,
        or None when there is nothing to load. Cheap on the (overwhelmingly
        common) case of an agent with no landed skills at all: that check is
        a filesystem stat, so the git resolution below — the only part of
        this that spawns a subprocess — only runs for agents that actually
        have something landed."""
        root = agent_skills_plugin_root(agent_id)
        if not root.is_dir() or not any(root.iterdir()):
            return None
        try:
            repository = await self.resolve_repository(working_dir)
        except SkillRegistryError:
            return None
        plugin_dir = agent_skills_plugin_dir(agent_id, repository)
        skills_dir = plugin_dir / "skills"
        if not skills_dir.is_dir() or not any(skills_dir.iterdir()):
            return None
        return str(plugin_dir)

    async def _land(
        self, repository: str, slug: str, content: str, candidate_id: str
    ) -> dict[str, str]:
        branch = f"owlery/skill-{slug}-{candidate_id[:8]}"
        scratch = tempfile.mkdtemp(prefix=f"owlery-skill-{slug}-")
        worktree_registered = False
        landed = False
        try:
            rc, _, err = await ws._git(
                "worktree", "add", "-b", branch, scratch, "HEAD", cwd=repository
            )
            if rc:
                raise SkillValidationError(err or "unable to create a landing worktree")
            worktree_registered = True
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
            landed = True
        finally:
            # The worktree itself is unconditionally best-effort: `scratch`
            # is a fresh mkdtemp path every call, so a stray leftover
            # worktree can never collide with a future attempt — and once
            # committed, the branch+commit are the durable result, so a
            # cleanup failure here must never strand that commit with the
            # candidate stuck `pending` (approve() persists the DB row right
            # after this returns; a raise from cleanup would skip that).
            try:
                if worktree_registered:
                    await ws.remove_git_worktree(repository, scratch, force=True)
                elif Path(scratch).exists():
                    shutil.rmtree(scratch, ignore_errors=True)
            except Exception:
                logger.exception(
                    "failed to clean up landing worktree %r for candidate %r "
                    "— continuing anyway", scratch, candidate_id,
                )
            if worktree_registered and not landed:
                # Unlike the worktree, `branch` is a NAME a retry will
                # collide with (`worktree add -b` on an existing branch
                # fails) — so unlike the worktree cleanup above, a failure
                # here must surface rather than vanish, or the candidate is
                # stuck `pending` forever with no visible cause. This raise
                # supersedes whatever exception is already propagating from
                # the `try` block above (normally the informative "would not
                # change anything on disk" no-op message) only when deleting
                # the branch ALSO fails — the common case still surfaces
                # that original message untouched.
                try:
                    await ws.delete_branch(repository, branch, force=True)
                except Exception as exc:
                    raise SkillValidationError(
                        f"landed nothing, and cleaning up the unused branch "
                        f"{branch!r} also failed: {exc}. Delete it manually "
                        "before retrying this candidate."
                    ) from exc
        return {
            "path": f".claude/skills/{slug}/SKILL.md",
            "branch": branch,
            "commit": commit,
        }


skill_registry = SkillRegistry()
