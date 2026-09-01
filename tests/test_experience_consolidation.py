from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

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

    await manager.submit_retrospective(
        task.id, run.id, run.session_id,
        memory_note="Reviewer checklist missed an edge case; noted for next time.",
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
