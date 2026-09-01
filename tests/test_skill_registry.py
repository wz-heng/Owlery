from __future__ import annotations

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


SKILL_BODY = """---
name: hermes-pr-flow
description: How to open a PR against an external repo through hermes.
---

Body content.
"""


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
    kwargs = dict(
        session_id="session-1",
        slug="hermes-pr-flow",
        title="Hermes PR flow",
        description="How to open a PR against an external repo.",
        body_markdown=SKILL_BODY,
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


@pytest.mark.asyncio
async def test_get_candidate_not_found(registry):
    reg, _db, _repo, _agent_id = registry
    with pytest.raises(SkillNotFoundError):
        await reg.get_candidate("missing")
