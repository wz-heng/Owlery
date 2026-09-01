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
async def test_resolve_plugin_dir_none_before_any_approval(registry):
    reg, _db, repo, agent_id = registry
    assert await reg.resolve_plugin_dir(agent_id=agent_id, working_dir=str(repo)) is None


@pytest.mark.asyncio
async def test_resolve_plugin_dir_returns_the_landed_dir_after_approval(registry):
    reg, _db, repo, agent_id = registry
    candidate = await _propose(reg)
    await reg.approve(candidate["id"])

    plugin_dir = await reg.resolve_plugin_dir(agent_id=agent_id, working_dir=str(repo))
    assert plugin_dir is not None
    assert (Path(plugin_dir) / "skills" / "hermes-pr-flow" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_resolve_plugin_dir_none_for_a_different_repository(registry, tmp_path):
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
        is None
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
