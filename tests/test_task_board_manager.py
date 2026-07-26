from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from server.config import settings
from server.database import Database
from server.session_manager import SessionManager
from server.task_board.manager import TaskBoardManager
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


async def _ready_task(db, repo, sessions, root: Path, *, origin: bool = False):
    agent = await db.get_default_agent()
    origin_session = None
    if origin:
        origin_session = await sessions.create_session(agent["id"], name="origin")
    board = await repo.create_board(name="Board", working_dir=str(root))
    task = await repo.create_task(
        board_id=board.id,
        title="Write durable report",
        body="Create report.txt and finish through the task tool.",
        status="todo",
        assignee_agent_id=agent["id"],
        origin_session_id=origin_session.id if origin_session else None,
    )
    assert task.status == "ready"
    return board, task, origin_session


@pytest.mark.asyncio
async def test_dispatch_creates_trusted_task_worker(task_runtime):
    db, repo, sessions, manager, root = task_runtime
    _board, task, _ = await _ready_task(db, repo, sessions, root)
    sessions.start_message = AsyncMock()

    await manager._dispatch_task(task)

    current = await repo.get_task(task.id)
    run = await repo.get_run(current.current_run_id)
    worker = sessions.get_session(run.session_id)
    assert current.status == "running"
    assert worker.origin == "task"
    assert worker.task_id == task.id
    assert worker.task_run_id == run.id
    assert worker.working_dir == str(root.resolve())
    prompt = sessions.start_message.await_args.args[1]
    assert "mcp__tasks__show()" in prompt
    config = sessions._run_config(worker)
    assert config.task_id == task.id
    assert config.task_run_id == run.id


@pytest.mark.asyncio
async def test_worker_complete_captures_artifact_and_enqueues_origin(task_runtime):
    db, repo, sessions, manager, root = task_runtime
    _board, task, origin = await _ready_task(db, repo, sessions, root, origin=True)
    sessions.start_message = AsyncMock()
    await manager._dispatch_task(task)
    current = await repo.get_task(task.id)
    run = await repo.get_run(current.current_run_id)
    (root / "report.txt").write_text("evidence")

    result = await manager.complete_worker(
        task.id,
        run.id,
        run.session_id,
        summary="Report written",
        artifacts=[{"path": "report.txt"}],
    )

    assert result["task"]["status"] == "done"
    artifacts = await repo.list_artifacts(task.id)
    assert len(artifacts) == 1
    assert Path(artifacts[0].stored_path).read_text() == "evidence"
    injection = await db.get_session_injection_by_source(
        f"task:{task.id}:run:{run.id}:terminal"
    )
    assert injection is not None
    assert injection["session_id"] == origin.id
    assert injection["status"] == "pending"


@pytest.mark.asyncio
async def test_restart_interrupts_run_notifies_origin_and_archives_worker(task_runtime):
    db, repo, sessions, manager, root = task_runtime
    _board, task, origin = await _ready_task(db, repo, sessions, root, origin=True)
    sessions.start_message = AsyncMock()
    await manager._dispatch_task(task)
    running_task = await repo.get_task(task.id)
    run = await repo.get_run(running_task.current_run_id)

    recovered = await manager.recover_phase1()
    assert len(recovered) == 1
    assert recovered[0][1].state == "interrupted"
    await manager.recover_phase2()

    final = await repo.get_task(task.id)
    assert final.status == "blocked"
    assert final.blocked_kind == "interrupted"
    assert sessions.get_session(run.session_id) is None
    injection = await db.get_session_injection_by_source(
        f"task:{task.id}:run:{run.id}:terminal"
    )
    assert injection is not None
    assert injection["session_id"] == origin.id


@pytest.mark.asyncio
async def test_persisted_bg_work_keeps_worker_live(task_runtime):
    db, repo, sessions, manager, root = task_runtime
    _board, task, _ = await _ready_task(db, repo, sessions, root)
    sessions.start_message = AsyncMock()
    await manager._dispatch_task(task)
    running_task = await repo.get_task(task.id)
    run = await repo.get_run(running_task.current_run_id)
    await db.create_bg_task(
        "bg1",
        run.session_id,
        "sleep 100",
        "long work",
        str(root),
        datetime.now(timezone.utc).isoformat(),
    )

    assert await manager.worker_has_pending_work(run.session_id) is True


@pytest.mark.asyncio
async def test_shutdown_cancels_idle_protocol_check_before_interrupting_run(task_runtime):
    _db, repo, sessions, manager, root = task_runtime
    _board, task, _ = await _ready_task(_db, repo, sessions, root)
    sessions.start_message = AsyncMock()
    await manager._dispatch_task(task)
    running_task = await repo.get_task(task.id)
    run = await repo.get_run(running_task.current_run_id)
    worker = sessions.get_session(run.session_id)

    # Reproduce the shutdown race: the idle checker is waiting for the
    # just-finished queue driver when shutdown cancels it. Cancellation must
    # escape the checker, otherwise failed(protocol) can beat interrupted.
    driver = asyncio.create_task(asyncio.sleep(60))
    worker._active_task = driver
    check = asyncio.create_task(manager._check_idle_protocol(run.session_id))
    manager._idle_checks[run.session_id] = check
    await asyncio.sleep(0)

    await manager.shutdown()

    final_task = await repo.get_task(task.id)
    final_run = await repo.get_run(run.id)
    assert final_task.status == "blocked"
    assert final_task.blocked_kind == "interrupted"
    assert final_run.state == "interrupted"
    assert check.done()
