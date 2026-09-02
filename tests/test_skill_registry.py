from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.database import Database
from server.skill_registry import (
    SkillConflictError,
    SkillNotFoundError,
    SkillRegistry,
    SkillValidationError,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


_DESCRIPTION = "How to open a PR against an external repo."


def _skill_body(name: str = "hermes-pr-flow", description: str = _DESCRIPTION) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\nBody content.\n"


SKILL_BODY = _skill_body()


@pytest.fixture
async def registry(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")

    db = Database(str(tmp_path / "db.sqlite"))
    await db.initialize()
    agent = await db.get_default_agent()

    session = SimpleNamespace(agent_id=agent["id"], working_dir=str(repo))
    session_mgr = SimpleNamespace(sessions={"session-1": session})
    # A real DB row too — the evidence-chain lookup (get_session_summary)
    # reads the `sessions` table, distinct from the in-memory session_mgr
    # dict the propose()/record_usage() live-session lookups use.
    await db.save_session(
        "session-1", "Test session", str(repo), "2026-01-01T00:00:00+00:00",
        agent_id=agent["id"],
    )

    reg = SkillRegistry()
    reg.bind(db=db, session_mgr=session_mgr)
    try:
        yield reg, db, repo, agent["id"]
    finally:
        await db.close()


async def _propose(reg: SkillRegistry, **overrides):
    slug = overrides.get("slug", "hermes-pr-flow")
    description = overrides.get("description", _DESCRIPTION)
    kwargs = dict(
        session_id="session-1",
        slug=slug,
        title="Hermes PR flow",
        description=description,
        body_markdown=_skill_body(slug, description),
        rationale="Walked this once, hit real friction, will recur.",
    )
    kwargs.update(overrides)
    return await reg.propose(**kwargs)


@pytest.mark.asyncio
async def test_propose_creates_pending_candidate(registry):
    reg, _db, _repo, agent_id = registry
    candidate = await _propose(reg)
    assert candidate["status"] == "pending"
    assert candidate["slug"] == "hermes-pr-flow"
    assert candidate["use_count"] == 0
    assert candidate["proposed_by_agent_id"] == agent_id
    assert candidate["proposed_by_session_id"] == "session-1"
    assert candidate["scope"] == "agent+repo"
    assert candidate["bundle_files"] is None
    assert candidate["lint_results"]["frontmatter_valid"] is True
    assert candidate["lint_results"]["slug_conflict"] is False
    assert candidate["lint_results"]["bundle_refs_valid"] is True
    assert candidate["lint_results"]["issues"] == []


@pytest.mark.asyncio
async def test_propose_rejects_bad_slug(registry):
    reg, _db, _repo, _agent_id = registry
    with pytest.raises(SkillValidationError):
        await _propose(reg, slug="Not Kebab Case")


@pytest.mark.asyncio
async def test_propose_rejects_frontmatter_name_slug_mismatch(registry):
    """Usage tracking looks candidates up by slug; a frontmatter `name:` that
    disagrees with it would silently break use_count for a landed skill."""
    reg, _db, _repo, _agent_id = registry
    with pytest.raises(SkillValidationError):
        await _propose(reg, slug="hermes-pr-flow", body_markdown=_skill_body("other-name"))


@pytest.mark.asyncio
async def test_propose_rejects_body_markdown_without_frontmatter(registry):
    reg, _db, _repo, _agent_id = registry
    with pytest.raises(SkillValidationError):
        await _propose(reg, body_markdown="No frontmatter here, just body text.\n")


@pytest.mark.asyncio
async def test_propose_rejects_frontmatter_description_mismatch(registry):
    """The frontmatter `description:` is what a future session actually sees
    when deciding whether to load the skill — it must be the same text as
    the `description` argument, not a stray copy that can drift."""
    reg, _db, _repo, _agent_id = registry
    with pytest.raises(SkillValidationError):
        await _propose(
            reg,
            description="How to open a PR against an external repo.",
            body_markdown=_skill_body(description="A completely different description."),
        )


@pytest.mark.asyncio
async def test_propose_requires_a_live_session(registry):
    reg, _db, _repo, _agent_id = registry
    with pytest.raises(SkillValidationError):
        await _propose(reg, session_id="does-not-exist")


@pytest.mark.asyncio
async def test_propose_rejects_blank_fields(registry):
    reg, _db, _repo, _agent_id = registry
    with pytest.raises(SkillValidationError):
        await _propose(reg, title="   ")
    with pytest.raises(SkillValidationError):
        await _propose(reg, rationale="")


@pytest.mark.asyncio
async def test_approve_persists_even_when_worktree_cleanup_fails(registry, monkeypatch):
    """A cleanup failure after a successful commit must not strand the
    candidate `pending` while the branch+commit already exist — that would
    make a retry collide with the branch this attempt created and leave the
    candidate stuck forever."""
    from server import skill_registry as sr_module

    reg, _db, repo, _agent_id = registry
    candidate = await _propose(reg)

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated worktree cleanup failure")

    monkeypatch.setattr(sr_module.ws, "remove_git_worktree", _boom)

    approved = await reg.approve(candidate["id"])
    assert approved["status"] == "approved"
    assert approved["landed_branch"]

    branches = subprocess.run(
        ["git", "branch", "--list", approved["landed_branch"]],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout
    assert approved["landed_branch"] in branches


@pytest.mark.asyncio
async def test_approve_no_op_leaves_no_orphan_branch(registry):
    """When the target file already matches on HEAD (nothing to commit),
    `_land` raises before the candidate is marked approved — but the branch
    `worktree add -b` created must not survive that failure, or a retry
    (e.g. after the human rejects and re-proposes under the same slug) would
    collide with a branch that never actually landed anything."""
    reg, _db, repo, _agent_id = registry
    skill_dir = repo / ".claude" / "skills" / "hermes-pr-flow"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_BODY)
    _git(repo, "add", ".claude")
    _git(repo, "commit", "-q", "-m", "pre-existing skill")

    candidate = await _propose(reg)
    with pytest.raises(SkillValidationError):
        await reg.approve(candidate["id"])

    fresh = await reg.get_candidate(candidate["id"])
    assert fresh["status"] == "pending"

    branches = subprocess.run(
        ["git", "branch", "--list", "owlery/skill-hermes-pr-flow-*"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout
    assert branches.strip() == ""


@pytest.mark.asyncio
async def test_approve_no_op_branch_delete_failure_surfaces(registry, monkeypatch):
    """Unlike worktree cleanup, a failure deleting the orphan branch on the
    no-op path must not vanish — it must replace the generic "nothing to
    commit" message with something actionable, or the candidate is stuck
    `pending` with no visible explanation for why every retry fails."""
    from server import skill_registry as sr_module

    reg, _db, repo, _agent_id = registry
    skill_dir = repo / ".claude" / "skills" / "hermes-pr-flow"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_BODY)
    _git(repo, "add", ".claude")
    _git(repo, "commit", "-q", "-m", "pre-existing skill")

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated branch delete failure")

    monkeypatch.setattr(sr_module.ws, "delete_branch", _boom)

    candidate = await _propose(reg)
    with pytest.raises(SkillValidationError, match="also failed"):
        await reg.approve(candidate["id"])

    fresh = await reg.get_candidate(candidate["id"])
    assert fresh["status"] == "pending"


@pytest.mark.asyncio
async def test_approve_lands_file_on_a_new_branch_without_pushing(registry):
    reg, _db, repo, _agent_id = registry
    candidate = await _propose(reg)
    approved = await reg.approve(candidate["id"], review_note="looks good")
    assert approved["status"] == "approved"
    assert approved["landed_path"] == ".claude/skills/hermes-pr-flow/SKILL.md"
    assert approved["landed_branch"]
    assert approved["landed_commit"]
    assert approved["materialized_backends"] == ["claude", "codex"]

    branches = subprocess.run(
        ["git", "branch", "--list", approved["landed_branch"]],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout
    assert approved["landed_branch"] in branches

    # Never pushed (no remote configured at all) and the checked-out branch's
    # own working tree is untouched — only a new local branch appeared.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout
    assert status.strip() == ""
    worktrees = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout
    assert "skill" not in worktrees  # the scratch worktree was torn down

    show = subprocess.run(
        ["git", "show", f"{approved['landed_branch']}:.claude/skills/hermes-pr-flow/SKILL.md"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout
    assert "hermes-pr-flow" in show


@pytest.mark.asyncio
async def test_approve_twice_conflicts(registry):
    reg, _db, _repo, _agent_id = registry
    candidate = await _propose(reg)
    await reg.approve(candidate["id"])
    with pytest.raises(SkillConflictError):
        await reg.approve(candidate["id"])


@pytest.mark.asyncio
async def test_reject_requires_a_note(registry):
    reg, _db, _repo, _agent_id = registry
    candidate = await _propose(reg)
    with pytest.raises(SkillValidationError):
        await reg.reject(candidate["id"], review_note="")
    rejected = await reg.reject(candidate["id"], review_note="not general enough")
    assert rejected["status"] == "rejected"


@pytest.mark.asyncio
async def test_reject_twice_conflicts(registry):
    reg, _db, _repo, _agent_id = registry
    candidate = await _propose(reg)
    await reg.reject(candidate["id"], review_note="no")
    with pytest.raises(SkillConflictError):
        await reg.reject(candidate["id"], review_note="no again")


@pytest.mark.asyncio
async def test_diff_against_empty_baseline_for_new_slug(registry):
    reg, _db, _repo, _agent_id = registry
    candidate = await _propose(reg)
    result = await reg.diff(candidate["id"])
    assert result["candidate"]["id"] == candidate["id"]
    assert "/dev/null" in result["diff"]
    assert "hermes-pr-flow" in result["diff"]
    assert result["file_diffs"] == {"SKILL.md": result["diff"]}
    assert result["task"] is None
    assert result["run"] is None
    assert result["invocations"] == []


@pytest.mark.asyncio
async def test_diff_against_landed_baseline_after_approval(registry):
    reg, _db, _repo, _agent_id = registry
    candidate = await _propose(reg)
    await reg.approve(candidate["id"])
    replacement = await _propose(reg, body_markdown=SKILL_BODY + "\nExtra line.\n")
    result = await reg.diff(replacement["id"])
    assert "/dev/null" not in result["diff"]
    assert "Extra line." in result["diff"]


@pytest.mark.asyncio
async def test_diff_includes_session_summary(registry):
    reg, db, _repo, _agent_id = registry
    candidate = await _propose(reg)
    result = await reg.diff(candidate["id"])
    assert result["session"]["id"] == "session-1"


@pytest.mark.asyncio
async def test_list_candidates_filters_by_status(registry):
    reg, _db, _repo, _agent_id = registry
    a = await _propose(reg, slug="skill-a")
    b = await _propose(reg, slug="skill-b")
    await reg.approve(a["id"])
    pending = await reg.list_candidates(status="pending")
    approved = await reg.list_candidates(status="approved")
    assert [c["id"] for c in pending] == [b["id"]]
    assert [c["id"] for c in approved] == [a["id"]]


@pytest.mark.asyncio
async def test_record_usage_increments_use_count_on_approved_candidate(registry):
    reg, _db, _repo, _agent_id = registry
    candidate = await _propose(reg)
    approved = await reg.approve(candidate["id"])
    assert approved["use_count"] == 0
    await reg.record_usage("hermes-pr-flow")
    await reg.record_usage("hermes-pr-flow")
    after = await reg.get_candidate(candidate["id"])
    assert after["use_count"] == 2
    assert after["last_used_at"]


@pytest.mark.asyncio
async def test_record_usage_unknown_slug_is_a_noop(registry):
    reg, _db, _repo, _agent_id = registry
    await reg.record_usage("does-not-exist")  # must not raise


# --- Real skill discovery (Snape review point 1): approval must make the
# skill invocable through a real loading path a future session's own
# `claude` process actually reads — not just leave a commit on an orphan
# branch nobody checks out. ------------------------------------------------


@pytest.mark.asyncio
async def test_approve_materializes_a_real_plugin_file_for_the_agent(registry):
    from server.skill_registry import agent_skills_plugin_dir

    reg, _db, repo, agent_id = registry
    candidate = await _propose(reg)
    approved = await reg.approve(candidate["id"])

    repository = await reg.resolve_repository(str(repo))
    plugin_dir = agent_skills_plugin_dir(agent_id, repository)
    manifest = plugin_dir / ".claude-plugin" / "plugin.json"
    skill_file = plugin_dir / "skills" / "hermes-pr-flow" / "SKILL.md"
    assert manifest.is_file()
    assert json.loads(manifest.read_text())["name"]
    assert skill_file.is_file()
    assert skill_file.read_text() == approved["body_markdown"]


@pytest.mark.asyncio
async def test_resolve_plugin_dir_empty_before_any_approval(registry):
    reg, _db, repo, agent_id = registry
    assert await reg.resolve_plugin_dir(agent_id=agent_id, working_dir=str(repo)) == []


@pytest.mark.asyncio
async def test_resolve_plugin_dir_returns_the_landed_dir_after_approval(registry):
    reg, _db, repo, agent_id = registry
    candidate = await _propose(reg)
    await reg.approve(candidate["id"])

    plugin_dirs = await reg.resolve_plugin_dir(agent_id=agent_id, working_dir=str(repo))
    assert len(plugin_dirs) == 1
    assert (Path(plugin_dirs[0]) / "skills" / "hermes-pr-flow" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_resolve_plugin_dir_empty_for_a_different_repository(registry, tmp_path):
    """A skill landed for one repository must not silently show up in a
    different repository's session — no merge action ever moves it there."""
    reg, _db, repo, agent_id = registry
    candidate = await _propose(reg)
    await reg.approve(candidate["id"])

    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    _git(other_repo, "init", "-q")
    _git(other_repo, "config", "user.name", "Test")
    _git(other_repo, "config", "user.email", "test@example.com")
    (other_repo / "README.md").write_text("base\n")
    _git(other_repo, "add", "README.md")
    _git(other_repo, "commit", "-q", "-m", "base")

    assert (
        await reg.resolve_plugin_dir(agent_id=agent_id, working_dir=str(other_repo))
        == []
    )


@pytest.mark.asyncio
async def test_record_usage_scoped_by_repository_two_repos_same_slug(tmp_path):
    """Two different repositories independently land a skill under the same
    slug (each proposed and approved on its own). `record_usage` must
    attribute a use to the candidate whose repository actually matches the
    invoking session, not whichever same-slug candidate was approved most
    recently — the bug Snape review point 2 flagged in the old
    `get_latest_approved_skill_by_slug(slug)` (no scope at all)."""
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    for repo in (repo_a, repo_b):
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "config", "user.name", "Test")
        _git(repo, "config", "user.email", "test@example.com")
        (repo / "README.md").write_text("base\n")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-q", "-m", "base")

    db = Database(str(tmp_path / "db.sqlite"))
    await db.initialize()
    agent = await db.get_default_agent()

    session_a = SimpleNamespace(agent_id=agent["id"], working_dir=str(repo_a))
    session_b = SimpleNamespace(agent_id=agent["id"], working_dir=str(repo_b))
    session_mgr = SimpleNamespace(
        sessions={"session-a": session_a, "session-b": session_b}
    )

    reg = SkillRegistry()
    reg.bind(db=db, session_mgr=session_mgr)
    try:
        candidate_a = await reg.propose(
            session_id="session-a", slug="hermes-pr-flow", title="A",
            description=_DESCRIPTION, body_markdown=SKILL_BODY,
            rationale="Walked this once in repo A.",
        )
        candidate_b = await reg.propose(
            session_id="session-b", slug="hermes-pr-flow", title="B",
            description=_DESCRIPTION, body_markdown=SKILL_BODY,
            rationale="Walked this once in repo B.",
        )
        await reg.approve(candidate_a["id"])
        # Approved AFTER a — the trap: an unscoped "latest approved by slug"
        # lookup would attribute EVERY subsequent use to b, even one that
        # really happened in repo a's session.
        await reg.approve(candidate_b["id"])

        repository_a = await reg.resolve_repository(str(repo_a))
        await reg.record_usage(
            "hermes-pr-flow", agent_id=agent["id"], repository=repository_a
        )

        after_a = await reg.get_candidate(candidate_a["id"])
        after_b = await reg.get_candidate(candidate_b["id"])
        assert after_a["use_count"] == 1
        assert after_b["use_count"] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_candidate_not_found(registry):
    reg, _db, _repo, _agent_id = registry
    with pytest.raises(SkillNotFoundError):
        await reg.get_candidate("missing")


# --- v2 §3③: bundle files + dual scope -------------------------------------


@pytest.mark.asyncio
async def test_propose_with_bundle_files_stores_them(registry):
    reg, _db, _repo, _agent_id = registry
    candidate = await _propose(
        reg,
        body_markdown=SKILL_BODY + "\nSee scripts/run.sh.\n",
        bundle_files={"scripts/run.sh": "#!/bin/sh\necho hi\n"},
    )
    assert candidate["bundle_files"] == {"scripts/run.sh": "#!/bin/sh\necho hi\n"}
    assert candidate["lint_results"]["bundle_refs_valid"] is True


@pytest.mark.asyncio
async def test_propose_lints_a_dangling_bundle_reference(registry):
    """A path referenced in body_markdown but missing from bundle_files is
    surfaced as an issue — informational only, never blocks propose()."""
    reg, _db, _repo, _agent_id = registry
    candidate = await _propose(
        reg, body_markdown=SKILL_BODY + "\nSee scripts/run.sh.\n",
    )
    assert candidate["status"] == "pending"  # not blocked
    assert candidate["lint_results"]["bundle_refs_valid"] is False
    assert any("scripts/run.sh" in issue for issue in candidate["lint_results"]["issues"])


@pytest.mark.asyncio
async def test_propose_lints_a_slug_conflict(registry):
    reg, _db, _repo, _agent_id = registry
    await _propose(reg, slug="dup-skill")
    second = await _propose(reg, slug="dup-skill")
    assert second["lint_results"]["slug_conflict"] is True


@pytest.mark.asyncio
async def test_propose_rejects_a_bundle_path_that_escapes_the_skill_dir(registry):
    reg, _db, _repo, _agent_id = registry
    with pytest.raises(SkillValidationError):
        await _propose(reg, bundle_files={"../escape.sh": "x"})
    with pytest.raises(SkillValidationError):
        await _propose(reg, bundle_files={"/etc/passwd": "x"})


@pytest.mark.asyncio
async def test_propose_rejects_skill_md_as_a_bundle_key(registry):
    reg, _db, _repo, _agent_id = registry
    with pytest.raises(SkillValidationError):
        await _propose(reg, bundle_files={"SKILL.md": "x"})


@pytest.mark.asyncio
async def test_propose_rejects_unknown_scope(registry):
    reg, _db, _repo, _agent_id = registry
    with pytest.raises(SkillValidationError):
        await _propose(reg, scope="not-a-real-scope")


@pytest.mark.asyncio
async def test_approve_materializes_bundle_files_alongside_skill_md(registry):
    from server.skill_registry import agent_skills_plugin_dir

    reg, _db, repo, agent_id = registry
    candidate = await _propose(
        reg, bundle_files={"scripts/run.sh": "#!/bin/sh\necho hi\n"},
    )
    approved = await reg.approve(candidate["id"])

    repository = await reg.resolve_repository(str(repo))
    plugin_dir = agent_skills_plugin_dir(agent_id, repository)
    script = plugin_dir / "skills" / "hermes-pr-flow" / "scripts" / "run.sh"
    assert script.is_file()
    assert script.read_text() == "#!/bin/sh\necho hi\n"

    landed = subprocess.run(
        [
            "git", "show",
            f"{approved['landed_branch']}:.claude/skills/hermes-pr-flow/scripts/run.sh",
        ],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout
    assert landed == "#!/bin/sh\necho hi\n"


@pytest.mark.asyncio
async def test_agent_global_scope_lands_in_a_cross_repo_directory(registry):
    from server.skill_registry import agent_skills_plugin_dir

    reg, _db, repo, agent_id = registry
    candidate = await _propose(reg, scope="agent-global")
    approved = await reg.approve(candidate["id"])
    assert approved["scope"] == "agent-global"

    global_dir = agent_skills_plugin_dir(agent_id, scope="agent-global")
    assert (global_dir / "skills" / "hermes-pr-flow" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_agent_global_skill_resolves_in_a_different_repository(registry, tmp_path):
    """Touchstone B (experience-consolidation-v2.md §5): an approved
    agent-global candidate must be loadable from a DIFFERENT repository."""
    reg, _db, repo, agent_id = registry
    candidate = await _propose(reg, scope="agent-global")
    await reg.approve(candidate["id"])

    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    _git(other_repo, "init", "-q")
    _git(other_repo, "config", "user.name", "Test")
    _git(other_repo, "config", "user.email", "test@example.com")
    (other_repo / "README.md").write_text("base\n")
    _git(other_repo, "add", "README.md")
    _git(other_repo, "commit", "-q", "-m", "base")

    dirs = await reg.resolve_plugin_dir(agent_id=agent_id, working_dir=str(other_repo))
    assert len(dirs) == 1
    assert (Path(dirs[0]) / "skills" / "hermes-pr-flow" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_resolve_plugin_dir_returns_both_global_and_repo_scoped(registry):
    reg, _db, repo, agent_id = registry
    global_candidate = await _propose(reg, slug="global-skill", scope="agent-global")
    await reg.approve(global_candidate["id"])
    repo_candidate = await _propose(reg, slug="repo-skill")
    await reg.approve(repo_candidate["id"])

    dirs = await reg.resolve_plugin_dir(agent_id=agent_id, working_dir=str(repo))
    assert len(dirs) == 2


@pytest.mark.asyncio
async def test_approve_can_override_scope_at_review_time(registry):
    """Reviewer choice wins over the proposer's default
    (experience-consolidation-v2.md §3③: "提名时选定,人审可改")."""
    from server.skill_registry import agent_skills_plugin_dir

    reg, _db, repo, agent_id = registry
    candidate = await _propose(reg)  # defaults to agent+repo
    approved = await reg.approve(candidate["id"], scope="agent-global")
    assert approved["scope"] == "agent-global"

    global_dir = agent_skills_plugin_dir(agent_id, scope="agent-global")
    assert (global_dir / "skills" / "hermes-pr-flow" / "SKILL.md").is_file()
    repository = await reg.resolve_repository(str(repo))
    repo_dir = agent_skills_plugin_dir(agent_id, repository)
    assert not (repo_dir / "skills" / "hermes-pr-flow").exists()


# --- v2 §3④: Codex activation adapter --------------------------------------


@pytest.mark.asyncio
async def test_approve_materializes_a_codex_canonical_copy(registry):
    from server.skill_registry import agent_codex_skills_dir

    reg, _db, repo, agent_id = registry
    candidate = await _propose(
        reg, bundle_files={"scripts/run.sh": "#!/bin/sh\necho hi\n"},
    )
    approved = await reg.approve(candidate["id"])
    assert "codex" in approved["materialized_backends"]

    repository = await reg.resolve_repository(str(repo))
    codex_dir = agent_codex_skills_dir(agent_id, repository)
    assert (codex_dir / "hermes-pr-flow" / "SKILL.md").is_file()
    assert (codex_dir / "hermes-pr-flow" / "scripts" / "run.sh").is_file()


@pytest.mark.asyncio
async def test_sync_codex_skills_dir_projects_into_codex_home(registry, tmp_path):
    reg, _db, repo, agent_id = registry
    candidate = await _propose(reg)
    await reg.approve(candidate["id"])

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    await reg.sync_codex_skills_dir(
        agent_id=agent_id, working_dir=str(repo), codex_home=str(codex_home)
    )

    skill_file = codex_home / "skills" / "hermes-pr-flow" / "SKILL.md"
    assert skill_file.is_file()
    assert skill_file.read_text() == candidate["body_markdown"]
    manifest = json.loads((codex_home / "skills" / ".owlery-manifest.json").read_text())
    assert manifest["slugs"] == ["hermes-pr-flow"]


@pytest.mark.asyncio
async def test_sync_codex_skills_dir_removes_stale_managed_entries(registry, tmp_path):
    """Switching from a repo with a landed skill to one with none must not
    leave the old repo's skill visible forever — Codex has no per-turn
    directory-selection flag, so the sync itself must clean up."""
    reg, _db, repo, agent_id = registry
    candidate = await _propose(reg)
    await reg.approve(candidate["id"])

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    await reg.sync_codex_skills_dir(
        agent_id=agent_id, working_dir=str(repo), codex_home=str(codex_home)
    )
    assert (codex_home / "skills" / "hermes-pr-flow").is_dir()

    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    _git(other_repo, "init", "-q")
    _git(other_repo, "config", "user.name", "Test")
    _git(other_repo, "config", "user.email", "test@example.com")
    (other_repo / "README.md").write_text("base\n")
    _git(other_repo, "add", "README.md")
    _git(other_repo, "commit", "-q", "-m", "base")

    await reg.sync_codex_skills_dir(
        agent_id=agent_id, working_dir=str(other_repo), codex_home=str(codex_home)
    )
    assert not (codex_home / "skills" / "hermes-pr-flow").exists()


@pytest.mark.asyncio
async def test_sync_codex_skills_dir_leaves_a_users_own_file_alone(registry, tmp_path):
    """A slug Owlery never managed (a user's own personal skill dropped into
    the same real, credential-owned directory) must survive a sync."""
    reg, _db, repo, agent_id = registry

    codex_home = tmp_path / "codex-home"
    (codex_home / "skills" / "my-own-skill").mkdir(parents=True)
    (codex_home / "skills" / "my-own-skill" / "SKILL.md").write_text(
        "---\nname: my-own-skill\ndescription: mine\n---\n\nBody.\n"
    )

    candidate = await _propose(reg)
    await reg.approve(candidate["id"])
    await reg.sync_codex_skills_dir(
        agent_id=agent_id, working_dir=str(repo), codex_home=str(codex_home)
    )

    assert (codex_home / "skills" / "my-own-skill" / "SKILL.md").is_file()
    assert (codex_home / "skills" / "hermes-pr-flow" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_sync_codex_skills_dir_never_raises_on_bad_codex_home(registry):
    reg, _db, repo, agent_id = registry
    candidate = await _propose(reg)
    await reg.approve(candidate["id"])
    # A file where a directory is expected — write attempts fail loudly if
    # not caught.
    import tempfile

    with tempfile.NamedTemporaryFile() as f:
        await reg.sync_codex_skills_dir(
            agent_id=agent_id, working_dir=str(repo), codex_home=f.name
        )  # must not raise


# --- v2 §3⑤: invocation log with a run/session foreign key -----------------


@pytest.mark.asyncio
async def test_record_usage_logs_an_invocation_with_session_and_backend(registry):
    """task_id/run_id are real foreign keys (skill_invocations references
    tasks/task_runs) — a genuine task/run pairing is covered by
    test_experience_consolidation.py's
    test_record_usage_logs_a_real_task_and_run_on_the_invocation, which has
    a real dispatched run to reference. This one covers the ordinary
    (non-task-board) session case, where task_id/run_id are legitimately
    None."""
    reg, db, _repo, agent_id = registry
    candidate = await _propose(reg)
    await reg.approve(candidate["id"])

    await reg.record_usage(
        "hermes-pr-flow",
        agent_id=agent_id,
        session_id="session-1",
        backend="claude-code",
    )

    invocations = await db.list_skill_invocations(candidate["id"])
    assert len(invocations) == 1
    assert invocations[0]["session_id"] == "session-1"
    assert invocations[0]["task_id"] is None
    assert invocations[0]["run_id"] is None
    assert invocations[0]["backend"] == "claude-code"


@pytest.mark.asyncio
async def test_diff_surfaces_invocation_history_for_an_approved_candidate(registry):
    reg, _db, _repo, agent_id = registry
    candidate = await _propose(reg)
    await reg.approve(candidate["id"])
    await reg.record_usage("hermes-pr-flow", agent_id=agent_id, session_id="session-1")

    result = await reg.diff(candidate["id"])
    assert len(result["invocations"]) == 1
    assert result["invocations"][0]["session_id"] == "session-1"
