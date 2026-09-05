"""Skill candidate review queue (experience-consolidation.md §3.4/§5,
experience-consolidation-v2.md §3).

Hermes-style shape: an agent PROPOSES a candidate SKILL.md (optionally with a
bundle of extra files); it starts `pending`; a human APPROVES or REJECTS it —
never the proposing agent (§4: "no auto-generated skill takes effect"). An
`approved` row IS the landed skill; there is no separate "skills" table, and
use_count/last_used accrue on the same row as later sessions invoke it,
mirroring hermes' single skill entity with a `.usage.json` sidecar.

Approval lands the skill THREE ways:

1. A git branch off the target repo (`_land`) — the durable, human-auditable
   diff/commit. Never pushed or merged automatically: that stays a human's
   separate, explicit action, exactly like any other Owlery-authored branch
   (CLAUDE.md: "commit to the branch, default no push"). This alone is NOT
   enough to make the skill invocable: nothing checks that branch out, so a
   future session's real `claude` process never sees the file (Snape review:
   the old e2e touchstone papered over this with a hardcoded fake `Skill`
   tool_use instead of proving real discovery).
2. A real file under the proposing agent's skills plugin directory
   (`_materialize_plugin`) — `--plugin-dir` loads this for real on every
   subsequent Claude turn (`resolve_plugin_dir`, wired in
   `session_manager._run_backend`). Scoped by (agent, `scope`[, repository])
   — `agent-global` gets one cross-repo directory per agent; `agent+repo`
   (the v1 default) is fingerprinted per repository, matching the axis
   use_count needs to disambiguate two independently-landed same-slug skills
   (see `get_latest_approved_skill_by_slug`).
3. A real file under the proposing agent's Codex-canonical skill store
   (`_materialize_codex_canonical`), later projected per-turn into the
   invoking session's actual `$CODEX_HOME/skills` by `sync_codex_skills_dir`
   — Codex's own still-supported (if "deprecated") user-skills discovery
   root (confirmed against `codex-rs/core-skills/src/loader.rs`; Codex has
   no `codex exec`-reachable equivalent of Claude's repeatable
   `--plugin-dir`, so unlike Claude this is a live per-turn filesystem sync
   rather than an argv flag — experience-consolidation-v2.md §3④).

A bundle (experience-consolidation-v2.md §3③) is an optional
`{relative_path: content}` map of extra files (scripts/templates/examples/
tests) alongside SKILL.md, written by the SAME propose() call — the worker
is in the room and has the content in hand, so there is no separate
artifact-upload pipeline (§4: "不做" — no artifact-ID/content-hash pipeline).
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

SCOPES = ("agent-global", "agent+repo")

# Bundle file references a static lint scans body_markdown for — a plausible
# path under one of the conventional bundle subdirectories. Deliberately a
# loose heuristic (not a markdown-link parser): it exists to catch the
# common "wrote the script, forgot to attach it" slip, not to be exhaustive.
_BUNDLE_REF_RE = re.compile(
    r"\b((?:scripts|templates|examples|tests)/[A-Za-z0-9_][A-Za-z0-9_./-]*)"
)

_RESERVED_BUNDLE_PATH = "SKILL.md"

# Every Owlery-materialized Claude plugin is named with this prefix
# (_materialize_plugin) — a real Skill tool_use reports
# "<plugin-name>:<slug>" (confirmed against a real spawn, 2026-09-02), and
# session_manager.py's use_count extraction checks this prefix before
# trusting the namespace belongs to Owlery, so an unrelated user-installed
# plugin's same-named skill can never be misattributed to an Owlery
# candidate (Snape review).
OWLERY_PLUGIN_NAME_PREFIX = "owlery-skills-"

# Written into every Codex-canonical skill dir sync_codex_skills_dir lands —
# the ownership check that gates a destructive rmtree/overwrite there (never
# manifest membership alone; see that method's docstring).
_OWLERY_OWNED_MARKER = ".owlery-owned"


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
    ever landed an approved skill for gets one subdirectory here, plus the
    `_global` cross-repo one and the `codex/` canonical Codex store."""
    return agent_state_dir(agent_id) / "skills"


def _validate_scope(scope: str) -> str:
    if scope not in SCOPES:
        raise SkillValidationError(f"scope must be one of {SCOPES}, got {scope!r}")
    return scope


def agent_skills_plugin_dir(
    agent_id: str, repository: str | None = None, *, scope: str = "agent+repo"
) -> Path:
    """The real Claude `--plugin-dir` target for `agent_id` at this `scope`
    (experience-consolidation.md §3.4, experience-consolidation-v2.md §3③,
    Snape review point 1). A Claude Code plugin directory:
    `.claude-plugin/plugin.json` + `skills/<slug>/SKILL.md` per landed
    skill. Materialized on approve, read by
    `resolve_plugin_dir`/`build_turn_argv` on every later turn — no git
    merge or checkout involved, so a brand-new session for this agent
    discovers the skill through Claude Code's own native plugin-skill
    loading, exactly as required for approval to actually take effect.

    `scope='agent-global'` returns one directory per agent, loaded by every
    repository that agent works in. `scope='agent+repo'` (default, the v1
    behavior) fingerprints by `repository` — required in that case — so two
    repositories never collide on disk even if they share a skill slug."""
    _validate_scope(scope)
    if scope == "agent-global":
        return agent_skills_plugin_root(agent_id) / "_global"
    if repository is None:
        raise SkillValidationError("agent+repo scope requires a repository")
    return agent_skills_plugin_root(agent_id) / _repo_fingerprint(repository)


def agent_codex_skills_dir(
    agent_id: str, repository: str | None = None, *, scope: str = "agent+repo"
) -> Path:
    """The Owlery-owned canonical Codex-materialized store for `agent_id` at
    this `scope` — NOT itself a Codex discovery path. `sync_codex_skills_dir`
    projects the relevant subset of this tree into the invoking session's
    actual `$CODEX_HOME/skills` per turn (experience-consolidation-v2.md
    §3④): Codex has no `codex exec`-reachable equivalent of Claude's
    repeatable `--plugin-dir`, so this canonical store — written once, at
    approve time, independent of any particular session's credential — is
    what a live per-turn sync reads from."""
    _validate_scope(scope)
    root = agent_skills_plugin_root(agent_id) / "codex"
    if scope == "agent-global":
        return root / "_global"
    if repository is None:
        raise SkillValidationError("agent+repo scope requires a repository")
    return root / _repo_fingerprint(repository)


def _validate_bundle_files(bundle_files: dict[str, str] | None) -> dict[str, str]:
    """Reject path traversal / absolute paths / a collision with the
    reserved `SKILL.md` name; otherwise pass the map through unchanged."""
    if not bundle_files:
        return {}
    cleaned: dict[str, str] = {}
    for relpath, content in bundle_files.items():
        if not isinstance(relpath, str) or not relpath.strip():
            raise SkillValidationError(
                "bundle_files keys must be non-empty relative paths"
            )
        if relpath == _RESERVED_BUNDLE_PATH:
            raise SkillValidationError(
                "'SKILL.md' is reserved for body_markdown, not a bundle file"
            )
        if relpath.startswith("/") or ".." in Path(relpath).parts:
            raise SkillValidationError(
                f"bundle file path {relpath!r} must be a relative path that "
                "stays within the skill directory"
            )
        if not isinstance(content, str):
            raise SkillValidationError(
                f"bundle file {relpath!r} content must be a string"
            )
        cleaned[relpath] = content
    return cleaned


def _scan_bundle_refs(body_markdown: str, bundle_files: dict[str, str]) -> list[str]:
    """Static lint (experience-consolidation-v2.md §3②): a plausible bundle
    file path mentioned in body_markdown that isn't actually in the bundle —
    the "wrote the script, forgot to attach it" slip. Heuristic, not a
    markdown-link parser; never blocks propose(), only surfaces on the
    review page."""
    issues: list[str] = []
    matches = {m.rstrip(".,;:!?)]}'\"") for m in _BUNDLE_REF_RE.findall(body_markdown)}
    for match in sorted(matches):
        if match and match not in bundle_files:
            issues.append(
                f"body_markdown references {match!r}, which is not in bundle_files"
            )
    return issues


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
        scope: str = "agent+repo",
        bundle_files: dict[str, str] | None = None,
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
        _validate_scope(scope)
        clean_bundle = _validate_bundle_files(bundle_files)
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

        # Static lint (experience-consolidation-v2.md §3②): informational
        # only, computed once and stored so the review page never has to
        # recompute it against a body that may have since been superseded.
        slug_conflict = await self.db.skill_candidates_with_slug_exist(slug)
        bundle_ref_issues = _scan_bundle_refs(body_markdown, clean_bundle)
        issues: list[str] = list(bundle_ref_issues)
        if slug_conflict:
            issues.append(
                f"slug {slug!r} conflicts with an existing pending/approved "
                "candidate — approving this proposes a REPLACEMENT"
            )
        lint_results = {
            "frontmatter_valid": True,  # hard-enforced above; recorded for display
            "slug_conflict": slug_conflict,
            "bundle_refs_valid": not bundle_ref_issues,
            "issues": issues,
        }

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
            scope=scope,
            bundle_files=clean_bundle or None,
            lint_results=lint_results,
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

    @staticmethod
    def _unified_diff(before: str, after: str, relpath: str) -> str:
        return "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{relpath}" if before else "/dev/null",
                tofile=f"b/{relpath}",
            )
        )

    async def diff(self, candidate_id: str) -> dict[str, Any]:
        """The candidate's full evidence chain (experience-consolidation-v2.md
        §3②): a per-file diff (SKILL.md + every bundle file) against the
        currently landed skill at the same slug, if any (empty baseline for
        a brand-new slug or a brand-new bundle file) — compared against the
        DB's own record of what was landed, not the filesystem, since the
        landing branch is deliberately never merged/checked out
        automatically. Also resolves the source task/run/session and (for
        an approved candidate) its invocation history, so a reviewer never
        approves on title+diff alone."""
        candidate = await self.get_candidate(candidate_id)
        landed = await self.db.get_latest_approved_skill_by_slug(
            candidate["slug"],
            agent_id=candidate["proposed_by_agent_id"],
            repository=candidate["repository"],
        )
        is_self = landed is not None and landed["id"] == candidate_id
        before_body = "" if is_self or landed is None else landed["body_markdown"]
        before_bundle: dict[str, str] = (
            {} if is_self or landed is None else (landed.get("bundle_files") or {})
        )
        after_bundle = candidate.get("bundle_files") or {}

        rel_root = f".claude/skills/{candidate['slug']}"
        file_diffs = {
            "SKILL.md": self._unified_diff(
                before_body, candidate["body_markdown"], f"{rel_root}/SKILL.md"
            )
        }
        for relpath in sorted(set(before_bundle) | set(after_bundle)):
            file_diffs[relpath] = self._unified_diff(
                before_bundle.get(relpath, ""),
                after_bundle.get(relpath, ""),
                f"{rel_root}/{relpath}",
            )

        task = (
            await self.db.get_task_summary(candidate["task_id"])
            if candidate["task_id"]
            else None
        )
        run = (
            await self.db.get_run_summary(candidate["run_id"])
            if candidate["run_id"]
            else None
        )
        session = (
            await self.db.get_session_summary(candidate["proposed_by_session_id"])
            if candidate["proposed_by_session_id"]
            else None
        )
        # Use `landed`'s own (scope, agent, repository) identity, not the
        # pending candidate's — `landed` was resolved above via the
        # ambiguous repo-scoped-wins-else-global heuristic (no explicit
        # `scope` passed), so a same-slug replacement proposed at a
        # DIFFERENT scope than its actual prior (e.g. an `agent-global`
        # prior with an `agent+repo` replacement pending) would otherwise
        # query the invocation lineage under the WRONG scope and silently
        # hide the prior's usage history (Snape review). Falls back to the
        # candidate's own identity only when there's no prior to inherit
        # from (a brand-new slug — invocations are empty either way).
        lineage_source = landed if landed is not None else candidate
        invocations = await self.db.list_skill_invocations_for_lineage(
            slug=candidate["slug"],
            scope=lineage_source["scope"],
            agent_id=lineage_source["proposed_by_agent_id"],
            repository=lineage_source["repository"],
        )

        return {
            "candidate": candidate,
            "diff": file_diffs["SKILL.md"],  # backward-compat single-file field
            "file_diffs": file_diffs,
            "task": task,
            "run": run,
            "session": session,
            "invocations": invocations,
        }

    async def approve(
        self,
        candidate_id: str,
        *,
        review_note: str | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        self._require_bound()
        candidate = await self.get_candidate(candidate_id)
        if candidate["status"] != "pending":
            raise SkillConflictError(f"candidate is already {candidate['status']}")
        final_scope = _validate_scope(scope) if scope else candidate["scope"]
        bundle_files = candidate.get("bundle_files")

        # Supersession (Snape review, three rounds): find whatever candidate
        # this REPOSITORY currently sees as the active landed view of this
        # slug (repository OR agent-global — the same lookup diff()/
        # record_usage() use). Only actually supersede it when `prior` was
        # ALSO proposed from this exact repository — i.e. it's the same
        # proposing context choosing a new location for "its" skill, not an
        # unrelated global candidate some OTHER repository's session
        # happens to also see via the OR-fallback. Without this repository
        # check, approving a repo-scoped candidate for repo B would delete
        # an agent-global skill proposed from repo A — silently breaking it
        # for repos C/D/... that never touched B's approval at all.
        #
        # This lookup is READ-ONLY — it only decides WHETHER a supersession
        # is needed. Acting on it (removing prior's materialized copy,
        # marking its row superseded) is deferred until after `_land()` and
        # both materialize calls below have all succeeded: those are the
        # failure-prone steps (a `_land()` no-op — nothing to commit — is a
        # real, reachable path), and if any of them raises, the prior
        # candidate's active skill must still be fully intact and its DB row
        # still active, so a failed `approve()` can simply be retried
        # without having already torn down the thing it was replacing.
        prior_to_supersede: dict[str, Any] | None = None
        if candidate["proposed_by_agent_id"]:
            prior = await self.db.get_latest_approved_skill_by_slug(
                candidate["slug"], agent_id=candidate["proposed_by_agent_id"],
                repository=candidate["repository"],
            )
            if prior is not None and prior["repository"] == candidate["repository"]:
                prior_key = (
                    "_global" if prior["scope"] == "agent-global"
                    else _repo_fingerprint(prior["repository"])
                )
                new_key = (
                    "_global" if final_scope == "agent-global"
                    else _repo_fingerprint(candidate["repository"])
                )
                if prior_key != new_key:
                    prior_to_supersede = prior

        landed = await self._land(
            candidate["repository"], candidate["slug"], candidate["body_markdown"],
            candidate_id, bundle_files=bundle_files,
        )
        materialized_backends: list[str] = []
        if candidate["proposed_by_agent_id"]:
            self._materialize_plugin(
                candidate["proposed_by_agent_id"],
                candidate["repository"],
                final_scope,
                candidate["slug"],
                candidate["body_markdown"],
                bundle_files=bundle_files,
            )
            materialized_backends.append("claude")
            self._materialize_codex_canonical(
                candidate["proposed_by_agent_id"],
                candidate["repository"],
                final_scope,
                candidate["slug"],
                candidate["body_markdown"],
                bundle_files=bundle_files,
            )
            materialized_backends.append("codex")

        if prior_to_supersede is not None:
            self._remove_materialized(
                candidate["proposed_by_agent_id"], prior_to_supersede["repository"],
                prior_to_supersede["scope"], candidate["slug"],
            )
            await self.db.mark_skill_candidate_superseded(
                prior_to_supersede["id"], superseded_at=_now_iso()
            )

        result = await self.db.review_skill_candidate(
            candidate_id,
            status="approved",
            review_note=review_note,
            reviewed_at=_now_iso(),
            scope=final_scope,
            landed_path=landed["path"],
            landed_branch=landed["branch"],
            landed_commit=landed["commit"],
            materialized_backends=materialized_backends or None,
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
        self,
        slug: str,
        *,
        agent_id: str | None = None,
        repository: str | None = None,
        scope: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        backend: str | None = None,
    ) -> None:
        """Best-effort use_count/last_used tracking plus an invocation-log
        row naming the consuming run/session (§5): never raises, so a skill
        invocation can never break the turn that made it.

        `agent_id`/`repository` scope the lookup to the exact candidate the
        invoking session actually has loaded (Snape review point 2) —
        without them, two independently-landed same-slug skills (different
        repos, or different agents) collide and use_count gets attributed to
        whichever was approved most recently instead of the one that was
        really invoked. `scope`, when the caller already knows it (T-B
        review round 2: namespace→scope misattribution), pins the lookup to
        an exact scope match instead of `get_latest_approved_skill_by_slug`'s
        (repository OR agent-global) heuristic — a real `agent-global`
        invocation must never fall through to that heuristic and collide
        with an `agent+repo` candidate at the same slug for the same
        agent+repository. `session_id`/`task_id`/`run_id`/`backend` are
        purely additive — the invocation log they feed (experience-
        consolidation-v2.md §3⑤) is a foreign key and a display list only,
        never aggregated into a rate or threshold (§4 "不做")."""
        if self.db is None:
            return
        try:
            candidate = await self.db.get_latest_approved_skill_by_slug(
                slug, agent_id=agent_id, repository=repository, scope=scope
            )
            if candidate is None:
                return
            now = _now_iso()
            await self.db.record_skill_candidate_usage(candidate["id"], used_at=now)
            await self.db.create_skill_invocation(
                invocation_id=uuid.uuid4().hex[:12],
                candidate_id=candidate["id"],
                agent_id=agent_id,
                repository=repository,
                session_id=session_id,
                task_id=task_id,
                run_id=run_id,
                backend=backend,
                used_at=now,
            )
        except Exception:
            logger.exception("failed to record skill usage for %r", slug)

    def _materialize_plugin(
        self,
        agent_id: str,
        repository: str,
        scope: str,
        slug: str,
        content: str,
        *,
        bundle_files: dict[str, str] | None = None,
    ) -> Path:
        """Write the real, directly-loadable skill file(s) behind
        `resolve_plugin_dir` (module docstring point 2). Synchronous and
        filesystem-only (no git) — this dir is never committed, so overwrite
        semantics naturally implement "landing a replacement for an existing
        slug"."""
        plugin_dir = agent_skills_plugin_dir(agent_id, repository, scope=scope)
        manifest = plugin_dir / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        if not manifest.exists():
            manifest.write_text(
                json.dumps(
                    {
                        "name": f"{OWLERY_PLUGIN_NAME_PREFIX}{plugin_dir.name}",
                        "description": (
                            "Owlery-approved skill candidates for this agent, "
                            "landed via the human review queue."
                        ),
                    },
                    indent=2,
                )
                + "\n"
            )
        skill_dir = plugin_dir / "skills" / slug
        # Clear before rewriting: a replacement candidate that DROPS a bundle
        # file a prior approval landed must not leave that stale file behind
        # (Snape review) — mkdir(exist_ok=True) alone only ever adds/
        # overwrites, never removes.
        shutil.rmtree(skill_dir, ignore_errors=True)
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content)
        for relpath, file_content in (bundle_files or {}).items():
            path = skill_dir / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(file_content)
        return plugin_dir

    @staticmethod
    def _remove_materialized(
        agent_id: str, repository: str, scope: str, slug: str
    ) -> None:
        """Remove `slug`'s materialized directory at (agent_id, scope,
        repository) from both the Claude plugin dir and the Codex canonical
        store — the cleanup half of a supersession that relocates a slug to
        a different scope/repository (see `approve()`). `repository` is
        ignored by both helpers when `scope == 'agent-global'`."""
        plugin_skill_dir = (
            agent_skills_plugin_dir(agent_id, repository, scope=scope)
            / "skills" / slug
        )
        shutil.rmtree(plugin_skill_dir, ignore_errors=True)
        codex_skill_dir = agent_codex_skills_dir(agent_id, repository, scope=scope) / slug
        shutil.rmtree(codex_skill_dir, ignore_errors=True)

    def _materialize_codex_canonical(
        self,
        agent_id: str,
        repository: str,
        scope: str,
        slug: str,
        content: str,
        *,
        bundle_files: dict[str, str] | None = None,
    ) -> Path:
        """Write the Owlery-owned canonical Codex store this agent's
        approved skill lives at, independent of any particular session's
        credential — `sync_codex_skills_dir` projects it into a real
        `$CODEX_HOME/skills` per turn (module docstring point 3)."""
        skill_dir = agent_codex_skills_dir(agent_id, repository, scope=scope) / slug
        shutil.rmtree(skill_dir, ignore_errors=True)  # see _materialize_plugin
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content)
        for relpath, file_content in (bundle_files or {}).items():
            path = skill_dir / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(file_content)
        return skill_dir

    @staticmethod
    def _agent_codex_sync_token(agent_id: str) -> str:
        """A per-agent secret Owlery keeps OUTSIDE `$CODEX_HOME` (under its
        own already-trusted `agents_dir`) and writes into the CONTENT of
        every `.owlery-owned` marker it creates there. Ownership is proven
        by content match, not just file presence — a predictable, empty
        marker filename could otherwise be pre-planted by some other
        process sharing that same real, credential-owned directory, tricking
        a future sync into treating a directory it doesn't actually own as
        safe to delete/overwrite (Snape review)."""
        token_path = agent_skills_plugin_root(agent_id) / ".codex-sync-token"
        if token_path.is_file():
            token = token_path.read_text().strip()
            if token:
                return token
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        token_path.write_text(token)
        return token

    async def resolve_plugin_dir(
        self, *, agent_id: str, working_dir: str
    ) -> list[str]:
        """The `--plugin-dir` directories this agent's turn in `working_dir`
        should load — zero, one, or two (`agent-global` and/or `agent+repo`,
        both loaded simultaneously when both have content; Claude's
        `--plugin-dir` is repeatable). Cheap on the (overwhelmingly common)
        case of an agent with no landed skills at all: that check is a
        filesystem stat, so the git resolution below — the only part of
        this that spawns a subprocess — only runs for agents that actually
        have something landed."""
        root = agent_skills_plugin_root(agent_id)
        if not root.is_dir() or not any(root.iterdir()):
            return []

        def _non_empty(plugin_dir: Path) -> bool:
            skills_dir = plugin_dir / "skills"
            return skills_dir.is_dir() and any(skills_dir.iterdir())

        dirs: list[str] = []
        global_dir = agent_skills_plugin_dir(agent_id, scope="agent-global")
        if _non_empty(global_dir):
            dirs.append(str(global_dir))
        try:
            repository = await self.resolve_repository(working_dir)
        except SkillRegistryError:
            repository = None
        if repository is not None:
            repo_dir = agent_skills_plugin_dir(agent_id, repository)
            if _non_empty(repo_dir):
                dirs.append(str(repo_dir))
        return dirs

    async def sync_codex_skills_dir(
        self, *, agent_id: str, working_dir: str, codex_home: str
    ) -> None:
        """Best-effort per-turn projection of this agent's Codex-materialized
        skills (global ∪ this repository) into `<codex_home>/skills` — the
        still-supported (if "deprecated") Codex user-skills discovery root
        under `$CODEX_HOME` (confirmed against codex-rs
        `core-skills/src/loader.rs::skill_roots_from_layer_stack_inner`).

        Codex has no `codex exec`-reachable equivalent of Claude's repeatable
        `--plugin-dir` (`set_extra_roots` is app-server/IDE-only), so unlike
        `resolve_plugin_dir` this REWRITES real disk state on every turn to
        keep the projection scoped to (agent, current repository) — without
        that, a skill landed for repo A would keep showing up in repo B's
        turns forever after the first sync.

        Every directory this method creates gets an `.owlery-owned` marker
        file whose CONTENT is a per-agent secret token kept outside
        `$CODEX_HOME` (`_agent_codex_sync_token`) — it will only ever
        overwrite or delete a slug directory whose marker content matches
        that token (Snape review, two rounds: a bare slug-name manifest only
        protects a DIFFERENT slug the user owns, and even a presence-only
        marker is a predictable empty filename something else sharing that
        real directory could pre-plant to defeat the check). A sidecar
        manifest (`.owlery-manifest.json`) is still used to know which slugs
        to consider for removal on the NEXT sync, but the marker check — not
        manifest membership — is what actually gates a destructive
        filesystem op; a corrupted/tampered manifest can at worst cause a
        stale Owlery-owned dir to survive an extra sync, never cause a
        non-Owlery dir to be touched. Manifest slugs (and desired slugs) are
        also filtered through `_SLUG_RE` before ever being joined into a
        path, so a malformed manifest entry can't smuggle a `..` segment.

        Never raises: a sync failure must not block the turn it would have
        prepared skills for."""
        try:
            source_root = agent_skills_plugin_root(agent_id) / "codex"
            if not source_root.is_dir():
                return
            try:
                repository = await self.resolve_repository(working_dir)
            except SkillRegistryError:
                repository = None

            desired: dict[str, Path] = {}
            global_dir = source_root / "_global"
            if global_dir.is_dir():
                for slug_dir in global_dir.iterdir():
                    if slug_dir.is_dir() and _SLUG_RE.match(slug_dir.name):
                        desired[slug_dir.name] = slug_dir
            if repository is not None:
                repo_dir = source_root / _repo_fingerprint(repository)
                if repo_dir.is_dir():
                    for slug_dir in repo_dir.iterdir():
                        if slug_dir.is_dir() and _SLUG_RE.match(slug_dir.name):
                            # Repo-scoped wins over global on a slug collision
                            # — more specific takes precedence, matching
                            # Codex's own project-over-user config layering.
                            desired[slug_dir.name] = slug_dir

            target = Path(codex_home) / "skills"
            manifest_path = target / ".owlery-manifest.json"
            previous: list[str] = []
            if manifest_path.is_file():
                try:
                    raw = json.loads(manifest_path.read_text()).get("slugs", [])
                    previous = [s for s in raw if isinstance(s, str) and _SLUG_RE.match(s)]
                except (json.JSONDecodeError, OSError):
                    previous = []

            if not desired and not previous:
                return  # nothing to do, and nothing Owlery-managed to clean up

            token = self._agent_codex_sync_token(agent_id)

            def _owned(dest: Path) -> bool:
                marker = dest / _OWLERY_OWNED_MARKER
                try:
                    return marker.is_file() and marker.read_text().strip() == token
                except OSError:
                    return False

            target.mkdir(parents=True, exist_ok=True)
            for stale_slug in set(previous) - set(desired):
                stale_dest = target / stale_slug
                if _owned(stale_dest):
                    shutil.rmtree(stale_dest, ignore_errors=True)

            managed: list[str] = []
            for slug, source_dir in desired.items():
                dest = target / slug
                if dest.exists() and not _owned(dest):
                    # A real, non-Owlery directory already lives at this
                    # slug (a user's own skill) — never touch it. This Owlery
                    # candidate silently loses the naming collision rather
                    # than clobbering something it doesn't own.
                    logger.warning(
                        "skipping Codex skill sync for slug %r: a non-Owlery "
                        "directory already exists at %s", slug, dest,
                    )
                    continue
                shutil.rmtree(dest, ignore_errors=True)
                shutil.copytree(source_dir, dest)
                (dest / _OWLERY_OWNED_MARKER).write_text(token)
                managed.append(slug)
            manifest_path.write_text(json.dumps({"slugs": sorted(managed)}))
        except Exception:
            logger.exception(
                "failed to sync Codex skills for agent %r into %r",
                agent_id, codex_home,
            )

    async def _land(
        self,
        repository: str,
        slug: str,
        content: str,
        candidate_id: str,
        *,
        bundle_files: dict[str, str] | None = None,
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
            skill_dir = Path(scratch) / ".claude" / "skills" / slug
            # Clear first: the worktree is checked out at HEAD, which already
            # has whatever a PRIOR landing committed for this slug — without
            # this, a bundle file a replacement candidate drops would never
            # get staged as a deletion (git add -A only sees what's still on
            # disk) and would survive forever in the git-landed copy.
            shutil.rmtree(skill_dir, ignore_errors=True)
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(content)
            for relpath, file_content in (bundle_files or {}).items():
                path = skill_dir / relpath
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(file_content)
            # `commit_all`'s plain `git add -A` respects the TARGET repo's
            # `.gitignore` — a repo that (like Owlery's own) ignores
            # `.claude/` would stage nothing, so `commit_all` sees a "clean"
            # tree and reports `committed=False`, which the caller then
            # turns into a 422 even though the landing worktree genuinely
            # has new content on disk. `commit_paths` forces past that,
            # scoped to exactly the directory this call wrote — never the
            # whole `.claude/` tree, and never a file this landing didn't
            # touch.
            result = await ws.commit_paths(
                scratch,
                f".claude/skills/{slug}",
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
