from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from server.config import settings
from server.database import Database
from server.deploy_admission import DeployAdmissionClosedError, DeployAdmissionGate
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


async def _terminal_delivery(db, repo, sessions, root: Path):
    _board, task, origin = await _ready_task(
        db, repo, sessions, root, origin=True
    )
    run = await repo.claim_ready(
        task.id,
        workspace_mode="git_worktree",
        workspace_path=str(root / "delivery-worktree"),
        run_id="delivery-run",
    )
    await repo.complete_run(task.id, run.id, summary="Worker finished")
    delivery = await repo.create_delivery(
        run.id,
        repository=str(root),
        attempt_branch="owlery/task-delivery-run",
        base_ref="main",
        base_head="a" * 40,
    )
    await repo.start_accept(delivery.id)
    delivery = await repo.record_baseline(
        delivery.id,
        status="ready",
        attempt_head="b" * 40,
        dirty=False,
        commits_ahead=1,
        diffstat={"files": 1, "insertions": 1, "deletions": 0},
        remote_name="origin",
        remote_url="/tmp/remote.git",
    )
    return task, run, origin, delivery


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
async def test_dispatch_claim_is_atomic_with_deploy_admission_close(task_runtime):
    _db, repo, sessions, manager, root = task_runtime
    _board, task, _ = await _ready_task(_db, repo, sessions, root)
    sessions.start_message = AsyncMock()

    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingGate(DeployAdmissionGate):
        @asynccontextmanager
        async def admit(self):
            async with self._lock:
                if self._closed:
                    raise DeployAdmissionClosedError("deploy admission is closed")
                entered.set()
                await release.wait()
                yield

    gate = BlockingGate()
    sessions.set_deploy_admission_gate(gate)
    dispatch = asyncio.create_task(manager._dispatch_task(task))
    await entered.wait()
    close = asyncio.create_task(gate.close())
    await asyncio.sleep(0)
    assert not close.done()

    release.set()
    await dispatch
    await close

    claimed = await repo.get_task(task.id)
    assert claimed.status == "running"
    assert gate.closed is True


@pytest.mark.asyncio
async def test_dispatch_does_not_claim_when_deploy_admission_is_closed(task_runtime):
    _db, repo, sessions, manager, root = task_runtime
    _board, task, _ = await _ready_task(_db, repo, sessions, root)
    sessions.start_message = AsyncMock()
    await sessions.deploy_admission_gate.close()

    with pytest.raises(DeployAdmissionClosedError):
        await manager._dispatch_task(task)

    assert (await repo.get_task(task.id)).status == "ready"


@pytest.mark.asyncio
async def test_claimed_dispatch_starts_while_deploy_admission_is_closed(
    task_runtime, monkeypatch: pytest.MonkeyPatch
):
    _db, repo, sessions, manager, root = task_runtime
    _board, task, _ = await _ready_task(_db, repo, sessions, root)
    started = asyncio.Event()
    keep_running = asyncio.Event()

    async def hold_worker(_session_id, _queued):
        started.set()
        await keep_running.wait()

    original_attach = repo.attach_run_session

    async def close_after_claim(task_id, run_id, session_id):
        await sessions.deploy_admission_gate.close()
        return await original_attach(task_id, run_id, session_id)

    monkeypatch.setattr(sessions, "_drive_messages", hold_worker)
    monkeypatch.setattr(repo, "attach_run_session", close_after_claim)
    try:
        await manager._dispatch_task(task)
        await asyncio.wait_for(started.wait(), timeout=1)

        current = await repo.get_task(task.id)
        run = await repo.get_run(current.current_run_id)
        worker = sessions.get_session(run.session_id)
        assert current.status == "running"
        assert run.state == "running"
        assert sessions.deploy_admission_gate.closed is True
        assert worker._active_task is not None and not worker._active_task.done()
    finally:
        keep_running.set()
        worker = next(
            (session for session in sessions.sessions.values() if session.task_id == task.id),
            None,
        )
        if worker is not None and worker._active_task is not None:
            await asyncio.wait_for(worker._active_task, timeout=1)


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
async def test_cancel_running_task_reaches_terminal_cancelled_in_one_call(task_runtime):
    """task-board-gaps.md §3.4: cancelling a RUNNING task stops the run
    (which lands it in `blocked`) and finishes the SAME cancel request
    through to the terminal `cancelled` status — one user action, not a
    second click needed once it's blocked. Chaining this is what keeps a
    cancel-while-running from reproducing the "looks blocked, is actually
    long since cancelled" card the §3.4 migration is cleaning up."""
    db, repo, sessions, manager, root = task_runtime
    _board, task, _ = await _ready_task(db, repo, sessions, root)
    sessions.start_message = AsyncMock()
    await manager._dispatch_task(task)
    running = await repo.get_task(task.id)
    assert running.status == "running"

    result = await manager.cancel_task(task.id, reason="no longer needed")

    assert result["status"] == "cancelled"
    final = await repo.get_task(task.id)
    assert final.status == "cancelled"
    assert final.blocked_kind is None
    run = (await repo.list_runs(task.id))[0]
    assert run.state == "cancelled"


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


@pytest.mark.asyncio
async def test_delivery_recovery_interrupts_op_and_rebuilds_terminal_outbox(
    task_runtime,
):
    db, repo, sessions, manager, root = task_runtime
    task, run, origin, delivery = await _terminal_delivery(
        db, repo, sessions, root
    )
    op = await repo.plan_op(
        delivery.id,
        kind="push",
        source_key=f"task:{task.id}:run:{run.id}:push:1",
        actor_kind="user",
    )
    await repo.start_op(delivery.id, op.id)
    manager.delivery.reconcile_interrupted_pr = AsyncMock()

    await manager.recover_deliveries()

    recovered = await repo.get_delivery(delivery.id)
    recovered_op = (await repo.list_delivery_ops(delivery.id))[0]
    assert recovered.status == "blocked"
    assert recovered.reason_kind == "interrupted"
    assert recovered_op.state == "interrupted"
    assert "outcome unknown" in (recovered_op.error or "")
    injection = await db.get_session_injection_by_source(
        f"task:{task.id}:run:{run.id}:delivery:terminal"
    )
    assert injection is not None
    assert injection["session_id"] == origin.id
    assert injection["status"] == "pending"
    # Boot's paused barrier is DB-only; platform reconcile happens later.
    manager.delivery.reconcile_interrupted_pr.assert_not_awaited()

    await manager.recover_deliveries()
    assert (
        await db.get_session_injection_by_source(
            f"task:{task.id}:run:{run.id}:delivery:terminal"
        )
    )["id"] == injection["id"]


@pytest.mark.asyncio
async def test_delivery_recovery_records_missing_origin_once(task_runtime):
    db, repo, sessions, manager, root = task_runtime
    task, _run, origin, delivery = await _terminal_delivery(
        db, repo, sessions, root
    )
    await repo.start_accept(delivery.id)
    await repo.record_baseline(
        delivery.id,
        status="failed",
        reason_kind="op_failed",
        reason_detail="baseline failed",
    )
    assert await sessions.delete_session(origin.id) is True

    await manager.recover_deliveries()
    await manager.recover_deliveries()

    events = await repo.list_task_events(task.id)
    unavailable = [
        event
        for event in events
        if event.kind == "delivery_notification_unavailable"
        and event.payload.get("delivery_id") == delivery.id
    ]
    assert len(unavailable) == 1
    assert await db.get_session_injection_by_source(
        f"task:{task.id}:run:{delivery.run_id}:delivery:terminal"
    ) is None


@pytest.mark.asyncio
async def test_delivery_recovery_self_heals_blocked_delivery_with_pr(task_runtime):
    """task-board-gaps open-pr-500.md §4: boot recovery must repair a delivery
    left `blocked` despite already carrying a `pr_number` — exactly the shape
    the terminal-notification idempotency-key collision produced in
    production (PR #9 / PR #6). Runs through the real `recover_deliveries()`
    entry point, not the repository method directly."""
    db, repo, sessions, manager, root = task_runtime
    task, _run, origin, delivery = await _terminal_delivery(db, repo, sessions, root)
    await repo.conn.execute(
        "UPDATE task_deliveries SET status='blocked', pushed_ref='refs/heads/x', "
        "pr_number=9, pr_url='https://github.com/acme/widgets/pull/9', pr_state='open', "
        "reason_kind='op_failed', "
        "reason_detail='GitHub PR creation failed (422): already exists' WHERE id=?",
        (delivery.id,),
    )
    await repo.conn.commit()

    await manager.recover_deliveries()

    healed = await repo.get_delivery(delivery.id)
    assert healed.status == "delivered"
    assert healed.reason_kind is None and healed.reason_detail is None
    assert healed.pr_number == 9


@pytest.mark.asyncio
async def test_list_release_deployments_and_current_release_pass_through(task_runtime):
    """The Releases panel (task-board-overhaul.md §3.2) needs a paginated
    history page AND the current live/staged rows independent of that page
    window — both manager entry points must reach the repository unchanged."""
    db, repo, sessions, manager, root = task_runtime
    board = await repo.create_board(name="Releases", working_dir=str(root))
    planned = await repo.plan_release_deployment(
        board_id=board.id, source_ref="main", sha="a" * 40,
        source_repo=str(root), actor_kind="user", actor_agent_id=None,
    )

    items, total = await manager.list_release_deployments(board.id, limit=1, offset=0)
    assert total == 1
    assert [item.id for item in items] == [planned.id]

    live, staged = await manager.get_current_release_deployments(board.id)
    assert live is None
    assert staged is None
