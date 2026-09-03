from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from server import agent_memory
from server.config import settings
from server.database import Database
from server.session_manager import SessionManager
from server.task_board.manager import TaskBoardManager
from server.task_board.models import TaskRetrospectiveRequiredError, TaskValidationError
from server.task_board.repository import TaskRepository


@pytest.fixture
async def task_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = str(tmp_path / "task-manager.db")
    monkeypatch.setattr(settings, "task_workspaces_dir", str(tmp_path / "workspaces"))
    monkeypatch.setattr(settings, "task_artifacts_dir", str(tmp_path / "artifacts"))
    db = Database(db_path)
    await db.initialize()
    repo = TaskRepository(db_path)
    await repo.initialize()
    sessions = SessionManager()
    await sessions.initialize(db)
    sessions.pause_session_injection_dispatch()
    manager = TaskBoardManager()
    manager.bind(session_mgr=sessions, db=db, repo=repo)
    try:
        yield db, repo, sessions, manager, tmp_path
    finally:
        sessions.remove_broadcast(manager.BROADCAST_KEY)
        await repo.close()
        await db.close()


async def _ready_task(db, repo, root: Path):
    agent = await db.get_default_agent()
    board = await repo.create_board(name="Board", working_dir=str(root))
    task = await repo.create_task(
        board_id=board.id,
        title="Write durable report",
        body="Create report.txt and finish through the task tool.",
        status="todo",
        assignee_agent_id=agent["id"],
    )
    assert task.status == "ready"
    return board, task


async def _dispatch(manager, sessions, repo, task):
    sessions.start_message = AsyncMock()
    await manager._dispatch_task(task)
    current = await repo.get_task(task.id)
    run = await repo.get_run(current.current_run_id)
    return current, run


@pytest.mark.asyncio
async def test_clean_first_pass_completes_without_a_retrospective(task_runtime):
    db, repo, sessions, manager, root = task_runtime
    _board, task = await _ready_task(db, repo, root)
    _current, run = await _dispatch(manager, sessions, repo, task)

    result = await manager.complete_worker(
        task.id, run.id, run.session_id, summary="Done on the first try."
    )
    assert result["task"]["status"] == "done"
    assert not await repo.has_retrospective(run.id)


@pytest.mark.asyncio
async def test_reusable_outcome_on_a_clean_pass_requires_a_retrospective_first(
    task_runtime,
):
    """experience-consolidation-v2.md §3①: a clean-pass worker MAY
    voluntarily self-report the run as worth distilling — doing so asks for
    the same retrospective a non-clean-pass run requires, without touching
    the trigger condition for non-clean passes at all."""
    db, repo, sessions, manager, root = task_runtime
    _board, task = await _ready_task(db, repo, root)
    _current, run = await _dispatch(manager, sessions, repo, task)

    with pytest.raises(TaskRetrospectiveRequiredError):
        await manager.complete_worker(
            task.id, run.id, run.session_id,
            summary="Done on the first try, and it's worth capturing.",
            reusable_outcome=True,
        )
    # A clean pass with the default False still completes without a gate.
    assert not await repo.is_non_clean_pass(task.id, run.id)

    await manager.submit_retrospective(
        task.id, run.id, run.session_id,
        nothing_note="Actually walked this a hundred times before, nothing new.",
    )
    result = await manager.complete_worker(
        task.id, run.id, run.session_id,
        summary="Done on the first try, and it's worth capturing.",
        reusable_outcome=True,
    )
    assert result["task"]["status"] == "done"
    assert result["run"]["metadata"]["reusable_outcome"] is True


@pytest.mark.asyncio
async def test_reusable_outcome_false_never_gates_a_clean_pass(task_runtime):
    db, repo, sessions, manager, root = task_runtime
    _board, task = await _ready_task(db, repo, root)
    _current, run = await _dispatch(manager, sessions, repo, task)

    result = await manager.complete_worker(
        task.id, run.id, run.session_id, summary="Done on the first try.",
        reusable_outcome=False,
    )
    assert result["task"]["status"] == "done"
    assert not await repo.has_retrospective(run.id)
    assert result["run"]["metadata"]["reusable_outcome"] is False


@pytest.mark.asyncio
async def test_a_retry_after_a_block_refuses_to_complete_without_reflecting(task_runtime):
    db, repo, sessions, manager, root = task_runtime
    _board, task = await _ready_task(db, repo, root)
    _current, run1 = await _dispatch(manager, sessions, repo, task)

    await manager.block_worker(
        task.id, run1.id, run1.session_id, reason="needs a human decision"
    )
    blocked = await repo.get_task(task.id)
    assert blocked.status == "blocked"
    await repo.unblock_task(task.id)
    retried = await repo.get_task(task.id)
    assert retried.status == "ready"

    _current2, run2 = await _dispatch(manager, sessions, repo, retried)
    assert run2.attempt_no == 2

    with pytest.raises(TaskRetrospectiveRequiredError):
        await manager.complete_worker(
            task.id, run2.id, run2.session_id, summary="Done after the retry."
        )

    await manager.submit_retrospective(
        task.id, run2.id, run2.session_id,
        nothing_note="Same transient input issue as usual; nothing new to capture.",
    )

    result = await manager.complete_worker(
        task.id, run2.id, run2.session_id, summary="Done after the retry."
    )
    assert result["task"]["status"] == "done"
    assert await repo.has_retrospective(run2.id)


@pytest.mark.asyncio
async def test_verdict_fail_gates_completion_even_on_attempt_one(task_runtime):
    db, repo, sessions, manager, root = task_runtime
    _board, task = await _ready_task(db, repo, root)
    _current, run = await _dispatch(manager, sessions, repo, task)

    with pytest.raises(TaskRetrospectiveRequiredError):
        await manager.complete_worker(
            task.id, run.id, run.session_id,
            summary="Reviewed upstream work and it does not pass.",
            verdict="fail",
        )

    memory_dir = agent_memory.agent_memory_dir(run.agent_id)
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "reviewer-checklist.md").write_text(
        "Reviewer checklist missed an edge case; noted for next time.\n"
    )
    await manager.submit_retrospective(
        task.id, run.id, run.session_id,
        memory_pointer="reviewer-checklist.md",
    )
    result = await manager.complete_worker(
        task.id, run.id, run.session_id,
        summary="Reviewed upstream work and it does not pass.",
        verdict="fail",
    )
    assert result["task"]["verdict"] == "fail"


@pytest.mark.asyncio
async def test_reflect_requires_at_least_one_field(task_runtime):
    db, repo, sessions, manager, root = task_runtime
    _board, task = await _ready_task(db, repo, root)
    _current, run = await _dispatch(manager, sessions, repo, task)

    with pytest.raises(TaskValidationError):
        await manager.submit_retrospective(task.id, run.id, run.session_id)


@pytest.mark.asyncio
async def test_reflect_rejects_whitespace_only_fields(task_runtime):
    """A blank string must not satisfy the gate — that would defeat the
    entire point of forcing a real retrospective to happen."""
    db, repo, sessions, manager, root = task_runtime
    _board, task = await _ready_task(db, repo, root)
    _current, run = await _dispatch(manager, sessions, repo, task)

    with pytest.raises(TaskValidationError):
        await manager.submit_retrospective(
            task.id, run.id, run.session_id, nothing_note="   "
        )
    with pytest.raises(TaskValidationError):
        await manager.submit_retrospective(
            task.id, run.id, run.session_id, skill_candidate_ids=["  ", ""]
        )


@pytest.mark.asyncio
async def test_reflect_twice_for_the_same_run_conflicts(task_runtime):
    from server.task_board.models import TaskConflictError

    db, repo, sessions, manager, root = task_runtime
    _board, task = await _ready_task(db, repo, root)
    _current, run = await _dispatch(manager, sessions, repo, task)

    await manager.submit_retrospective(
        task.id, run.id, run.session_id, nothing_note="Nothing new."
    )
    with pytest.raises(TaskConflictError):
        await manager.submit_retrospective(
            task.id, run.id, run.session_id, nothing_note="Nothing new, again."
        )


@pytest.mark.asyncio
async def test_reflect_records_skill_candidate_ids(task_runtime):
    db, repo, sessions, manager, root = task_runtime
    _board, task = await _ready_task(db, repo, root)
    _current, run = await _dispatch(manager, sessions, repo, task)

    result = await manager.submit_retrospective(
        task.id, run.id, run.session_id, skill_candidate_ids=["candidate-1"],
    )
    assert result["skill_candidate_ids"] == ["candidate-1"]


@pytest.mark.asyncio
async def test_record_usage_logs_a_real_task_and_run_on_the_invocation(task_runtime):
    """experience-consolidation-v2.md §3⑤: skill_invocations.task_id/run_id
    are real foreign keys — this proves record_usage works against a
    GENUINE dispatched task/run pairing (task_board's own repo/db), not
    just a shape check with fabricated ids."""
    from server.skill_registry import SkillRegistry

    db, repo, sessions, manager, root = task_runtime
    _board, task = await _ready_task(db, repo, root)
    _current, run = await _dispatch(manager, sessions, repo, task)

    reg = SkillRegistry()
    reg.bind(db=db, session_mgr=sessions)
    candidate = await db.create_skill_candidate(
        candidate_id="cand-real-fk",
        slug="real-fk-flow",
        title="Real FK flow",
        description="d",
        body_markdown="---\nname: real-fk-flow\ndescription: d\n---\nBody.\n",
        repository=str(root),
        rationale="r",
        proposed_by_agent_id=run.agent_id,
        proposed_by_session_id=run.session_id,
        task_id=None,
        run_id=None,
        scope="agent+repo",
        bundle_files=None,
        lint_results=None,
        created_at="2026-01-01T00:00:00+00:00",
    )
    await db.review_skill_candidate(
        candidate["id"], status="approved", review_note=None,
        reviewed_at="2026-01-01T00:00:00+00:00",
    )

    await reg.record_usage(
        "real-fk-flow",
        agent_id=run.agent_id,
        session_id=run.session_id,
        task_id=task.id,
        run_id=run.id,
        backend="claude-code",
    )

    invocations = await db.list_skill_invocations(candidate["id"])
    assert len(invocations) == 1
    assert invocations[0]["task_id"] == task.id
    assert invocations[0]["run_id"] == run.id


@pytest.mark.asyncio
async def test_is_non_clean_pass_true_for_a_prior_blocked_run(task_runtime):
    db, repo, sessions, manager, root = task_runtime
    _board, task = await _ready_task(db, repo, root)
    _current, run1 = await _dispatch(manager, sessions, repo, task)
    await manager.block_worker(task.id, run1.id, run1.session_id, reason="x")
    await repo.unblock_task(task.id)
    retried = await repo.get_task(task.id)
    _current2, run2 = await _dispatch(manager, sessions, repo, retried)

    assert await repo.is_non_clean_pass(task.id, run2.id) is True
    assert await repo.is_non_clean_pass(task.id, run1.id) is False


# --- memory_pointer / claude_md_note are gated by a real artifact, not free
# text (Snape review point 3: a DB string alone is checkbox theater). -------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


async def _git_ready_task(db, repo, root: Path):
    """A git_worktree-mode board/task whose source repo already has a
    committed CLAUDE.md — real `_dispatch` (not the DB-only `claim_ready`
    shortcut other Task Board tests use) so `run.workspace_path` is an
    actual git worktree `_verify_claude_md_artifact` can `git diff` on.

    The repo lives in its OWN subdirectory of `root`, not `root` itself —
    `root` is also where the fixture's sqlite db file lives, and
    `git_worktree` dispatch refuses a dirty source repo."""
    source = root / "repo"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.name", "Test")
    _git(source, "config", "user.email", "test@example.com")
    (source / "CLAUDE.md").write_text("# Rules\n\nExisting rule.\n")
    _git(source, "add", "CLAUDE.md")
    _git(source, "commit", "-q", "-m", "base")
    _git(source, "branch", "-M", "main")

    agent = await db.get_default_agent()
    board = await repo.create_board(
        name="Git Board", working_dir=str(source), default_workspace_mode="git_worktree"
    )
    task = await repo.create_task(
        board_id=board.id,
        title="Nominate a CLAUDE.md rule",
        body="Land a real CLAUDE.md edit.",
        status="todo",
        assignee_agent_id=agent["id"],
    )
    assert task.status == "ready"
    return board, task


@pytest.mark.asyncio
async def test_reflect_rejects_a_memory_pointer_with_no_file_behind_it(task_runtime):
    db, repo, sessions, manager, root = task_runtime
    _board, task = await _ready_task(db, repo, root)
    _current, run = await _dispatch(manager, sessions, repo, task)

    with pytest.raises(TaskValidationError):
        await manager.submit_retrospective(
            task.id, run.id, run.session_id, memory_pointer="never-written.md",
        )


@pytest.mark.asyncio
async def test_reflect_rejects_a_memory_pointer_that_escapes_the_memory_dir(task_runtime):
    db, repo, sessions, manager, root = task_runtime
    _board, task = await _ready_task(db, repo, root)
    _current, run = await _dispatch(manager, sessions, repo, task)

    with pytest.raises(TaskValidationError):
        await manager.submit_retrospective(
            task.id, run.id, run.session_id, memory_pointer="../../etc/passwd",
        )
    with pytest.raises(TaskValidationError):
        await manager.submit_retrospective(
            task.id, run.id, run.session_id, memory_pointer="/etc/passwd",
        )


@pytest.mark.asyncio
async def test_reflect_accepts_a_memory_pointer_to_a_real_file(task_runtime):
    db, repo, sessions, manager, root = task_runtime
    _board, task = await _ready_task(db, repo, root)
    _current, run = await _dispatch(manager, sessions, repo, task)

    memory_dir = agent_memory.agent_memory_dir(run.agent_id)
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "lesson.md").write_text("Retry with backoff next time.\n")

    result = await manager.submit_retrospective(
        task.id, run.id, run.session_id, memory_pointer="lesson.md",
    )
    assert result["memory_pointer"] == "lesson.md"


@pytest.mark.asyncio
async def test_reflect_rejects_a_claude_md_note_on_a_non_git_worktree_run(task_runtime):
    db, repo, sessions, manager, root = task_runtime
    _board, task = await _ready_task(db, repo, root)  # default "shared" mode
    _current, run = await _dispatch(manager, sessions, repo, task)

    with pytest.raises(TaskValidationError):
        await manager.submit_retrospective(
            task.id, run.id, run.session_id,
            claude_md_note="Everyone should know this.",
        )


@pytest.mark.asyncio
async def test_reflect_rejects_a_claude_md_note_with_no_real_diff(task_runtime):
    db, repo, sessions, manager, root = task_runtime
    _board, task = await _git_ready_task(db, repo, root)
    _current, run = await _dispatch(manager, sessions, repo, task)

    with pytest.raises(TaskValidationError):
        await manager.submit_retrospective(
            task.id, run.id, run.session_id,
            claude_md_note="Everyone should know this.",
        )


@pytest.mark.asyncio
async def test_reflect_rejects_a_claude_md_note_backed_only_by_a_nested_file(task_runtime):
    """A commit touching `docs/CLAUDE.md` (or any other nested CLAUDE.md)
    must not satisfy this gate — only the repo's ROOT CLAUDE.md is the
    "loaded for every agent" carrier the channel is about (Snape review)."""
    db, repo, sessions, manager, root = task_runtime
    _board, task = await _git_ready_task(db, repo, root)
    _current, run = await _dispatch(manager, sessions, repo, task)

    workspace = Path(run.workspace_path)
    nested = workspace / "docs" / "CLAUDE.md"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text("Nested rule.\n")
    _git(workspace, "add", "docs/CLAUDE.md")
    _git(
        workspace, "-c", "user.name=Worker", "-c", "user.email=w@example.com",
        "commit", "-q", "-m", "docs: nested claude.md",
    )

    with pytest.raises(TaskValidationError):
        await manager.submit_retrospective(
            task.id, run.id, run.session_id,
            claude_md_note="Everyone should know this.",
        )


@pytest.mark.asyncio
async def test_reflect_accepts_a_claude_md_note_backed_by_a_real_commit(task_runtime):
    db, repo, sessions, manager, root = task_runtime
    _board, task = await _git_ready_task(db, repo, root)
    _current, run = await _dispatch(manager, sessions, repo, task)

    workspace = Path(run.workspace_path)
    claude_md = workspace / "CLAUDE.md"
    claude_md.write_text(claude_md.read_text() + "\nNew rule from the retro.\n")
    _git(workspace, "add", "CLAUDE.md")
    _git(
        workspace, "-c", "user.name=Worker", "-c", "user.email=w@example.com",
        "commit", "-q", "-m", "claude.md: new rule",
    )

    result = await manager.submit_retrospective(
        task.id, run.id, run.session_id,
        claude_md_note="Everyone should know this.",
    )
    assert result["claude_md_note"] == "Everyone should know this."
