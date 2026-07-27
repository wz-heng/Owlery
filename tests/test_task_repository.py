from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from server.database import Database
from server.task_board.models import (
    TaskCapacityError,
    TaskConflictError,
    TaskNotFoundError,
    TaskValidationError,
)
from server.task_board.repository import TaskRepository


@pytest.fixture
async def task_store(tmp_path: Path):
    db_path = tmp_path / "owlery.db"
    db = Database(str(db_path))
    await db.initialize()
    repo = TaskRepository(str(db_path))
    await repo.initialize()
    agent_id = (await db.load_agents())[0]["id"]
    yield db, repo, tmp_path, agent_id
    await repo.close()
    await db.close()


async def _board(repo: TaskRepository, root: Path, **kwargs):
    return await repo.create_board(name=kwargs.pop("name", "Board"), working_dir=str(root), **kwargs)


async def _task(repo: TaskRepository, board_id: str, agent_id: str, **kwargs):
    return await repo.create_task(
        board_id=board_id,
        title=kwargs.pop("title", "Task"),
        status=kwargs.pop("status", "todo"),
        assignee_agent_id=kwargs.pop("assignee_agent_id", agent_id),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_schema_busy_timeout_and_tasks_mcp_backfill(task_store):
    db, repo, _, _ = task_store
    main_timeout = await (await db.conn.execute("PRAGMA busy_timeout")).fetchone()
    repo_timeout = await (await repo.conn.execute("PRAGMA busy_timeout")).fetchone()
    assert main_timeout[0] == repo_timeout[0] == 5000
    tables = {
        row[0]
        for row in await (
            await db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'task_%'"
            )
        ).fetchall()
    }
    assert {
        "task_boards",
        "task_dependencies",
        "task_runs",
        "task_comments",
        "task_events",
        "task_artifacts",
        "task_deliveries",
        "task_delivery_ops",
    } <= tables
    agents = await db.load_agents()
    assert "tasks" in agents[0]["mcp_servers"]
    version = await (await db.conn.execute("PRAGMA user_version")).fetchone()
    assert version[0] >= 2


@pytest.mark.asyncio
async def test_transcript_write_releases_lock_for_repository(task_store):
    db, repo, root, _ = task_store
    await db.save_session(
        "worker-session",
        "Worker",
        str(root),
        "2026-07-26T00:00:00+00:00",
    )
    await db.append_message(
        "worker-session",
        seq=0,
        role="user",
        type="text",
        content="start work",
    )

    # No explicit db.flush(): append_message's persistence boundary must make
    # the second writer immediately usable during an active model turn.
    board = await repo.create_board(name="Concurrent", working_dir=str(root))
    assert board.name == "Concurrent"


@pytest.mark.asyncio
async def test_board_git_delivery_defaults_are_configurable(task_store):
    _, repo, root, _ = task_store
    board = await _board(
        repo,
        root,
        git_delivery_remote="upstream",
        git_delivery_retention="remove_worktree_keep_branch",
        git_delivery_author_name="Owlery Delivery",
        git_delivery_author_email="delivery@example.test",
        git_delivery_default_draft_pr=False,
        git_delivery_default_merge="fast_forward_only",
    )
    assert board.git_delivery_remote == "upstream"
    assert board.git_delivery_retention == "remove_worktree_keep_branch"
    assert board.git_delivery_author_name == "Owlery Delivery"
    assert board.git_delivery_author_email == "delivery@example.test"
    assert board.git_delivery_default_draft_pr is False
    assert board.git_delivery_default_merge == "fast_forward_only"

    updated = await repo.update_board(
        board.id,
        git_delivery_retention="remove_all",
        git_delivery_default_draft_pr=True,
    )
    assert updated.git_delivery_retention == "remove_all"
    assert updated.git_delivery_default_draft_pr is True

    with pytest.raises(TaskValidationError):
        await repo.update_board(board.id, git_delivery_retention="erase_everything")


@pytest.mark.asyncio
async def test_repository_rejects_memory_and_board_paths(tmp_path: Path):
    with pytest.raises(ValueError):
        await TaskRepository(":memory:").initialize()
    db = Database(str(tmp_path / "x.db"))
    await db.initialize()
    repo = TaskRepository(str(tmp_path / "x.db"))
    await repo.initialize()
    with pytest.raises(TaskValidationError):
        await repo.create_board(name="bad", working_dir="relative")
    with pytest.raises(TaskValidationError):
        await repo.create_board(name="bad", working_dir=str(tmp_path / "missing"))
    await repo.close()
    await db.close()


@pytest.mark.asyncio
async def test_board_crud_live_name_pause_and_stale_guard(task_store):
    _, repo, root, _ = task_store
    board = await _board(repo, root)
    with pytest.raises(TaskConflictError):
        await _board(repo, root, name="Board")
    paused = await repo.set_dispatch_enabled(board.id, False)
    assert not paused.dispatch_enabled
    with pytest.raises(TaskConflictError):
        await repo.update_board(board.id, expected_updated_at="stale", description="x")
    changed = await repo.update_board(
        board.id, expected_updated_at=paused.updated_at, description="new"
    )
    assert changed.description == "new"
    archived = await repo.archive_board(board.id)
    assert archived.archived
    replacement = await _board(repo, root, name="Board")
    assert replacement.id != board.id


@pytest.mark.asyncio
async def test_idempotent_create_reports_winner_and_atomic_dependencies(task_store):
    _, repo, root, agent = task_store
    board = await _board(repo, root)
    dependency = await _task(repo, board.id, agent, title="dep")
    first, created = await repo.create_task_result(
        board_id=board.id,
        title="consumer",
        status="todo",
        assignee_agent_id=agent,
        idempotency_key="same",
        dependencies=[dependency.id],
    )
    second, repeated = await repo.create_task_result(
        board_id=board.id,
        title="ignored",
        idempotency_key="same",
    )
    assert created and not repeated and first.id == second.id
    assert [d.depends_on_task_id for d in await repo.list_dependencies(first.id)] == [
        dependency.id
    ]
    with pytest.raises(TaskNotFoundError):
        await repo.create_task(
            board_id=board.id,
            title="rollback",
            status="todo",
            assignee_agent_id=agent,
            dependencies=["missing"],
            task_id="rollback-id",
        )
    with pytest.raises(TaskNotFoundError):
        await repo.get_task("rollback-id")


@pytest.mark.asyncio
async def test_normative_transitions_and_illegal_edges(task_store):
    _, repo, root, agent = task_store
    board = await _board(repo, root)
    task = await repo.create_task(board_id=board.id, title="idea")
    assert task.status == "triage"
    task = await repo.specify_task(task.id, body="specified")
    assert task.status == "todo"
    task = await repo.assign_task(task.id, agent)
    assert task.status == "ready"
    task = await repo.triage_task(task.id)
    assert task.status == "triage"
    task = await repo.specify_task(task.id)
    assert task.status == "ready"
    task = await repo.cancel_task(task.id, reason="no longer needed")
    assert (task.status, task.blocked_kind) == ("blocked", "cancelled")
    task = await repo.unblock_task(task.id, comment="resume")
    assert task.status == "ready"
    assert [comment.body for comment in await repo.list_comments(task.id)] == ["resume"]
    with pytest.raises(TaskConflictError):
        await repo.specify_task(task.id)


@pytest.mark.asyncio
async def test_schedule_and_assignment_reconcile_todo_ready(task_store):
    _, repo, root, agent = task_store
    board = await _board(repo, root)
    task = await repo.create_task(
        board_id=board.id,
        title="later",
        status="todo",
        assignee_agent_id=agent,
        scheduled_at="2999-01-01T00:00:00+00:00",
    )
    assert task.status == "todo"
    task = await repo.update_task(task.id, scheduled_at=None)
    assert task.status == "ready"
    task = await repo.assign_task(task.id, None)
    assert task.status == "todo"
    task = await repo.assign_task(task.id, agent)
    assert task.status == "ready"


@pytest.mark.asyncio
async def test_dependency_dag_cross_board_cycle_and_promotion(task_store):
    _, repo, root, agent = task_store
    board = await _board(repo, root)
    other = await _board(repo, root, name="Other")
    first = await _task(repo, board.id, agent, title="first")
    second = await _task(repo, board.id, agent, title="second")
    foreign = await _task(repo, other.id, agent, title="foreign")
    await repo.add_dependency(second.id, first.id)
    assert (await repo.get_task(second.id)).status == "todo"
    assert (await repo.list_dependents(first.id))[0].task_id == second.id
    with pytest.raises(TaskConflictError):
        await repo.add_dependency(first.id, second.id)
    with pytest.raises(TaskValidationError):
        await repo.add_dependency(first.id, foreign.id)
    run = await repo.claim_ready(
        first.id, workspace_mode="copy", workspace_path=str(root / "run-first")
    )
    await repo.complete_run(first.id, run.id, summary="done")
    assert (await repo.get_task(second.id)).status == "ready"


@pytest.mark.asyncio
async def test_tree_cycle_depth_and_atomic_patch_guard(task_store):
    _, repo, root, agent = task_store
    board = await _board(repo, root, max_tree_depth=2)
    parent = await _task(repo, board.id, agent, title="parent")
    child = await _task(repo, board.id, agent, title="child", parent_task_id=parent.id)
    with pytest.raises(TaskCapacityError):
        await _task(repo, board.id, agent, title="too deep", parent_task_id=child.id)
    with pytest.raises(TaskConflictError):
        await repo.update_task(parent.id, parent_task_id=child.id)
    snapshot = await repo.get_task(child.id)
    updated = await repo.update_task(
        child.id,
        expected_updated_at=snapshot.updated_at,
        title="renamed",
        parent_task_id=None,
    )
    assert updated.title == "renamed" and updated.parent_task_id is None
    with pytest.raises(TaskConflictError):
        await repo.update_task(child.id, expected_updated_at=snapshot.updated_at, body="stale")


@pytest.mark.asyncio
async def test_open_task_and_per_run_fanout_guards(task_store):
    _, repo, root, agent = task_store
    board = await _board(repo, root, max_open_tasks=2, max_children_per_run=1)
    owner = await _task(repo, board.id, agent, title="owner")
    run = await repo.claim_ready(
        owner.id, workspace_mode="copy", workspace_path=str(root / "owner-run")
    )
    child = await repo.create_task(
        board_id=board.id,
        title="child",
        created_by_kind="agent",
        created_by_agent_id=agent,
        creator_run_id=run.id,
    )
    assert child.status == "triage"
    with pytest.raises(TaskCapacityError):
        await repo.create_task(
            board_id=board.id,
            title="overflow",
            created_by_kind="agent",
            creator_run_id=run.id,
        )
    await repo.complete_run(owner.id, run.id, summary="done")
    # done tasks no longer count toward the open-task cap, but the creator run
    # remains capped by its append-only task_created event count.
    await repo.create_task(board_id=board.id, title="human replacement")


@pytest.mark.asyncio
async def test_claim_is_single_winner_across_connections(task_store):
    db, repo, root, agent = task_store
    board = await _board(repo, root)
    task = await _task(repo, board.id, agent)
    rival = TaskRepository(db._db_path)
    await rival.initialize()

    async def claim(candidate: TaskRepository, suffix: str):
        try:
            return await candidate.claim_ready(
                task.id,
                run_id=f"run-{suffix}",
                workspace_mode="copy",
                workspace_path=str(root / suffix),
            )
        except TaskConflictError as exc:
            return exc

    results = await asyncio.gather(claim(repo, "one"), claim(rival, "two"))
    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert len(await repo.list_runs(task.id)) == 1
    await rival.close()


@pytest.mark.asyncio
async def test_claim_limits_pause_and_nonexistent_private_workspace(task_store):
    _, repo, root, agent = task_store
    board = await _board(repo, root, max_running=1)
    first = await _task(repo, board.id, agent, title="one")
    second = await _task(repo, board.id, agent, title="two")
    run = await repo.claim_ready(
        first.id,
        workspace_mode="copy",
        workspace_path=str(root / "does-not-exist-yet"),
    )
    assert run.workspace_path.endswith("does-not-exist-yet")
    with pytest.raises(TaskCapacityError):
        await repo.claim_ready(
            second.id, workspace_mode="copy", workspace_path=str(root / "second")
        )
    await repo.cancel_run(first.id, run.id)
    await repo.set_dispatch_enabled(board.id, False)
    with pytest.raises(TaskConflictError):
        await repo.claim_ready(
            second.id, workspace_mode="copy", workspace_path=str(root / "second")
        )


@pytest.mark.asyncio
async def test_shared_workspace_conflict_is_global_across_boards(task_store):
    _, repo, root, agent = task_store
    first_board = await _board(repo, root)
    second_board = await _board(repo, root, name="Other")
    first = await _task(repo, first_board.id, agent, title="one")
    second = await _task(repo, second_board.id, agent, title="two")
    await repo.claim_ready(first.id, workspace_mode="shared", workspace_path=str(root))
    with pytest.raises(TaskCapacityError):
        await repo.claim_ready(second.id, workspace_mode="shared", workspace_path=str(root))


@pytest.mark.asyncio
async def test_terminal_cas_attempt_history_and_done_immutability(task_store):
    _, repo, root, agent = task_store
    board = await _board(repo, root)
    task = await _task(repo, board.id, agent)
    first = await repo.claim_ready(
        task.id, workspace_mode="copy", workspace_path=str(root / "first")
    )
    blocked, closed = await repo.block_run(
        task.id, first.id, reason="need input", summary="partial"
    )
    assert blocked.blocked_kind == "input" and closed.state == "blocked"
    with pytest.raises(TaskConflictError):
        await repo.complete_run(task.id, first.id, summary="late")
    task = await repo.unblock_task(task.id)
    second = await repo.claim_ready(
        task.id, workspace_mode="copy", workspace_path=str(root / "second")
    )
    done, completed = await repo.complete_run(task.id, second.id, summary="shipped")
    assert done.status == "done" and completed.attempt_no == 2
    with pytest.raises(TaskConflictError):
        await repo.update_task(task.id, body="rewrite history")


@pytest.mark.asyncio
async def test_complete_and_artifact_metadata_are_atomic(task_store):
    _, repo, root, agent = task_store
    board = await _board(repo, root)
    task = await _task(repo, board.id, agent)
    run = await repo.claim_ready(
        task.id, workspace_mode="copy", workspace_path=str(root / "artifact-run")
    )
    with pytest.raises(TaskValidationError):
        await repo.complete_run(
            task.id,
            run.id,
            summary="bad artifact",
            artifacts=[{"name": "x", "size": -1, "sha256": "bad"}],
        )
    assert (await repo.get_task(task.id)).status == "running"
    assert (await repo.get_run(run.id)).state == "running"
    assert await repo.list_artifacts(task.id) == []
    done, _ = await repo.complete_run(
        task.id,
        run.id,
        summary="good",
        artifacts=[
            {
                "id": "artifact-one",
                "name": "report",
                "stored_path": "/durable/report.md",
                "source_path": "report.md",
                "mime_type": "text/markdown",
                "size": 12,
                "sha256": "abc",
            }
        ],
    )
    assert done.status == "done"
    artifact = await repo.get_artifact(task.id, "artifact-one")
    assert artifact.sha256 == "abc"
    tombstone = await repo.delete_artifact(task.id, artifact.id)
    assert tombstone.deleted_at and await repo.list_artifacts(task.id) == []
    assert len(await repo.list_artifacts(task.id, include_deleted=True)) == 1


@pytest.mark.asyncio
async def test_comments_events_board_cursor_and_tree(task_store):
    _, repo, root, agent = task_store
    board = await _board(repo, root)
    parent = await _task(repo, board.id, agent, title="parent")
    child = await _task(repo, board.id, agent, title="child", parent_task_id=parent.id)
    await repo.add_comment(parent.id, "finding", author_kind="agent", author_agent_id=agent)
    events = await repo.list_board_events(board.id)
    assert events == sorted(events, key=lambda event: event.seq)
    after = await repo.list_board_events(board.id, after_seq=events[-2].seq)
    assert [event.seq for event in after] == [events[-1].seq]
    tree = await repo.get_tree(board.id)
    parent_node = next(node for node in tree if node["id"] == parent.id)
    assert parent_node["children"][0]["id"] == child.id
    assert (await repo.list_task_events(parent.id))[-1].kind == "task_comment_added"


@pytest.mark.asyncio
async def test_interrupt_recovery_and_notification_audit_are_idempotent(task_store):
    _, repo, root, agent = task_store
    board = await _board(repo, root)
    task = await repo.create_task(
        board_id=board.id,
        title="originated",
        status="todo",
        assignee_agent_id=agent,
        origin_session_id="origin",
    )
    run = await repo.claim_ready(
        task.id, workspace_mode="copy", workspace_path=str(root / "interrupted")
    )
    closed = await repo.interrupt_all_running(reason="server restarted")
    assert len(closed) == 1
    assert closed[0][0].blocked_kind == "interrupted"
    assert await repo.interrupt_all_running(reason="again") == []
    terminal = await repo.list_terminal_runs()
    assert terminal[0][1].id == run.id
    first = await repo.record_notification_unavailable(task.id, run.id, reason="deleted")
    second = await repo.record_notification_unavailable(task.id, run.id, reason="deleted")
    assert first.seq == second.seq


@pytest.mark.asyncio
async def test_worker_has_persisted_pending_work(task_store):
    db, _, root, agent = task_store
    now = "2026-01-01T00:00:00+00:00"
    session_id = "worker-session"
    await db.save_session(
        session_id,
        "worker",
        str(root),
        now,
        agent_id=agent,
        origin="task",
    )
    assert not await db.worker_has_persisted_pending_work(session_id)
    await db.conn.execute(
        "INSERT INTO session_injections "
        "(id,source_key,session_id,prompt,status,created_at) VALUES (?,?,?,?,?,?)",
        ("inj", "test:inj", session_id, "wake", "pending", now),
    )
    await db.conn.commit()
    assert await db.worker_has_persisted_pending_work(session_id)


@pytest.mark.asyncio
async def test_worker_pending_work_covers_terminal_to_outbox_windows(task_store):
    db, _, root, agent = task_store
    now = "2026-01-01T00:00:00+00:00"
    session_id = "worker-delivery-window"
    await db.save_session(
        session_id,
        "worker",
        str(root),
        now,
        agent_id=agent,
        origin="task",
    )

    await db.create_bg_task(
        "bg-window", session_id, "true", None, str(root), now
    )
    await db.update_bg_task(
        "bg-window", status="completed", exit_code=0, completed_at=now
    )
    assert await db.worker_has_persisted_pending_work(session_id)
    await db.create_session_injection(
        injection_id="bg-window-injection",
        source_key="bg:bg-window",
        session_id=session_id,
        prompt="bg done",
        created_at=now,
    )
    await db.fail_session_injection(
        "bg-window-injection", "delivery intentionally settled for test"
    )
    assert not await db.worker_has_persisted_pending_work(session_id)

    await db.create_research_job("research-window", session_id, "why", now)
    await db.update_research_job(
        "research-window",
        status="completed",
        phase="done",
        completed_at=now,
        report_path=str(root / "report.md"),
    )
    assert await db.worker_has_persisted_pending_work(session_id)
    await db.create_session_injection(
        injection_id="research-window-injection",
        source_key="research:research-window",
        session_id=session_id,
        prompt="research done",
        created_at=now,
    )
    await db.fail_session_injection(
        "research-window-injection", "delivery intentionally settled for test"
    )
    assert not await db.worker_has_persisted_pending_work(session_id)

    child_id = "delegation-child-window"
    await db.save_session(
        child_id,
        "child",
        str(root),
        now,
        agent_id=agent,
        origin="delegation",
        parent_session_id=session_id,
        delegation_request="check",
    )
    await db.create_delegation_run(
        run_id="delegation-run-window",
        delegation_id=child_id,
        round_no=1,
        request="check",
        start_seq=0,
        created_at=now,
        state="completed",
        finished_at=now,
    )
    assert await db.worker_has_persisted_pending_work(session_id)
    await db.create_session_injection(
        injection_id="delegation-window-injection",
        source_key="delegation:delegation-run-window:terminal",
        session_id=session_id,
        prompt="delegation done",
        created_at=now,
    )
    await db.fail_session_injection(
        "delegation-window-injection",
        "delivery intentionally settled for test",
    )
    assert not await db.worker_has_persisted_pending_work(session_id)
