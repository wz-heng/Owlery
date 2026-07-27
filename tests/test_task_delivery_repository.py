"""Repository-layer tests for the Git delivery closure (task-git-delivery.md).

Exercises the delivery/op state machine, both Albus implementation nits, the
one-running-op boundary, and boot-recovery reads — all below the coordinator,
against a real temp-file database.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from server.database import Database
from server.task_board.models import (
    DeliveryConfirmationRequired,
    TaskConflictError,
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


async def _completed_worktree_run(repo: TaskRepository, root: Path, agent_id: str):
    board = await repo.create_board(
        name="Delivery", working_dir=str(root), default_workspace_mode="git_worktree"
    )
    task = await repo.create_task(
        board_id=board.id, title="Ship", status="todo", assignee_agent_id=agent_id
    )
    assert (await repo.get_task(task.id)).status == "ready"
    run = await repo.claim_ready(
        task.id, workspace_mode="git_worktree", workspace_path=str(root / "wt")
    )
    await repo.complete_run(task.id, run.id, summary="did the work")
    return board, task, run


async def _accepted_ready(repo, run, **baseline):
    delivery = await repo.create_delivery(
        run.id,
        repository="/repo",
        attempt_branch=f"owlery/task-x-run-{run.attempt_no}",
        base_ref="main",
        base_head="aaaa",
    )
    await repo.start_accept(delivery.id)
    return await repo.record_baseline(
        delivery.id,
        status="ready",
        attempt_head=baseline.get("attempt_head", "bbbb"),
        dirty=baseline.get("dirty", False),
        commits_ahead=baseline.get("commits_ahead", 1),
        diffstat=baseline.get("diffstat", {"files": 1, "insertions": 3, "deletions": 0}),
    )


@pytest.mark.asyncio
async def test_create_requires_completed_worktree_run(task_store):
    _, repo, root, agent = task_store
    board = await repo.create_board(name="Shared", working_dir=str(root))
    task = await repo.create_task(
        board_id=board.id, title="T", status="todo", assignee_agent_id=agent
    )
    run = await repo.claim_ready(
        task.id, workspace_mode="shared", workspace_path=str(root)
    )
    # Still running, wrong mode → rejected.
    with pytest.raises(TaskConflictError):
        await repo.create_delivery(
            run.id, repository=str(root), attempt_branch="b", base_ref="main", base_head="a"
        )


@pytest.mark.asyncio
async def test_create_delivery_is_idempotent(task_store):
    _, repo, root, agent = task_store
    _, _, run = await _completed_worktree_run(repo, root, agent)
    first = await repo.create_delivery(
        run.id, repository="/repo", attempt_branch="b", base_ref="main", base_head="a"
    )
    assert first.status == "pending"
    second = await repo.create_delivery(
        run.id, repository="/other", attempt_branch="zzz", base_ref="dev", base_head="z"
    )
    # Same row, unchanged — never a second delivery, never a rewrite.
    assert second.id == first.id
    assert second.repository == "/repo" and second.base_ref == "main"


@pytest.mark.asyncio
async def test_accept_baseline_and_reaccept_refreshes(task_store):
    _, repo, root, agent = task_store
    _, _, run = await _completed_worktree_run(repo, root, agent)
    ready = await _accepted_ready(repo, run, commits_ahead=1)
    assert ready.status == "ready" and ready.commits_ahead == 1

    # Nit 1: pending/ready → accept → preparing is a legal re-accept, and it
    # refreshes non-base fields (N1) without touching the base pair.
    await repo.start_accept(ready.id)
    refreshed = await repo.record_baseline(
        ready.id, status="ready", attempt_head="cccc", commits_ahead=2,
        diffstat={"files": 2, "insertions": 9, "deletions": 1},
    )
    assert refreshed.status == "ready"
    assert refreshed.commits_ahead == 2 and refreshed.attempt_head == "cccc"
    assert refreshed.base_ref == "main" and refreshed.base_head == "aaaa"


@pytest.mark.asyncio
async def test_baseline_failed_and_base_ambiguous_resolution(task_store):
    _, repo, root, agent = task_store
    _, _, run = await _completed_worktree_run(repo, root, agent)
    delivery = await repo.create_delivery(
        run.id, repository="/repo", attempt_branch="b", base_ref=None, base_head="a"
    )
    await repo.start_accept(delivery.id)
    blocked = await repo.record_baseline(
        delivery.id, status="blocked", reason_kind="base_ambiguous",
        reason_detail="no captured base branch",
    )
    assert blocked.status == "blocked" and blocked.reason_kind == "base_ambiguous"

    # Operator names a verified base → back to pending for a fresh accept.
    resolved = await repo.resolve_base(delivery.id, base_ref="release")
    assert resolved.status == "pending" and resolved.base_ref == "release"
    assert resolved.reason_kind is None

    # workspace-gone failure path.
    await repo.start_accept(resolved.id)
    failed = await repo.record_baseline(
        resolved.id, status="failed", reason_kind="workspace_gone_no_effect",
        reason_detail="worktree removed, no push",
    )
    assert failed.status == "failed"
    # Nit 2 / §4.1.1: failed may be re-accepted (retry), other terminals cannot.
    assert (await repo.start_accept(failed.id)).status == "preparing"


@pytest.mark.asyncio
async def test_terminal_delivery_never_rewinds_on_reaccept(task_store):
    _, repo, root, agent = task_store
    _, _, run = await _completed_worktree_run(repo, root, agent)
    ready = await _accepted_ready(repo, run)
    op = await repo.plan_op(
        ready.id, kind="push", source_key="k:push", actor_kind="user"
    )
    await repo.start_op(ready.id, op.id)
    delivered, _ = await repo.finish_op(
        ready.id, op.id, state="succeeded", delivery_status="delivered",
        delivery_fields={"pushed_ref": "refs/heads/x"},
    )
    assert delivered.status == "delivered"
    # Nit 2: an idempotent re-run of accept must not roll a terminal backward.
    with pytest.raises(TaskConflictError):
        await repo.start_accept(delivered.id)
    assert (await repo.get_delivery(delivered.id)).status == "delivered"


@pytest.mark.asyncio
async def test_op_at_most_once_and_one_running(task_store):
    _, repo, root, agent = task_store
    _, _, run = await _completed_worktree_run(repo, root, agent)
    ready = await _accepted_ready(repo, run)

    op = await repo.plan_op(
        ready.id, kind="push", source_key="k:push", actor_kind="user"
    )
    # A repeated source_key returns the same row — never a second attempt.
    again = await repo.plan_op(
        ready.id, kind="push", source_key="k:push", actor_kind="user"
    )
    assert again.id == op.id

    other = await repo.plan_op(
        ready.id, kind="commit", source_key="k:commit", actor_kind="user"
    )
    delivery, started = await repo.start_op(ready.id, op.id)
    assert delivery.status == "delivering" and started.state == "running"
    # One running op per delivery.
    with pytest.raises(TaskConflictError):
        await repo.start_op(ready.id, other.id)


@pytest.mark.asyncio
async def test_boot_recovery_interrupt_reset_and_terminal_list(task_store):
    _, repo, root, agent = task_store
    _, task, run = await _completed_worktree_run(repo, root, agent)
    ready = await _accepted_ready(repo, run)
    op = await repo.plan_op(
        ready.id, kind="pull_request", source_key="k:pr", actor_kind="user"
    )
    await repo.start_op(ready.id, op.id)

    # A crash mid-op: recovery interrupts the op and blocks the delivery.
    interrupted = await repo.interrupt_running_delivery_ops(reason="server restarted")
    assert len(interrupted) == 1
    delivery, iop = interrupted[0]
    assert iop.state == "interrupted" and delivery.status == "blocked"
    assert delivery.reason_kind == "interrupted"
    assert not await repo.list_running_delivery_ops()

    # A blocked (terminal) delivery is discoverable for outbox reconstruction.
    terminals = await repo.list_terminal_deliveries()
    assert any(d.id == delivery.id and t.id == task.id for t, d in terminals)


@pytest.mark.asyncio
async def test_reset_preparing_deliveries(task_store):
    _, repo, root, agent = task_store
    _, _, run = await _completed_worktree_run(repo, root, agent)
    delivery = await repo.create_delivery(
        run.id, repository="/repo", attempt_branch="b", base_ref="main", base_head="a"
    )
    await repo.start_accept(delivery.id)
    assert (await repo.get_delivery(delivery.id)).status == "preparing"
    reset = await repo.reset_preparing_deliveries()
    assert reset == 1
    assert (await repo.get_delivery(delivery.id)).status == "pending"


def test_confirmation_required_is_a_conflict():
    exc = DeliveryConfirmationRequired(
        "force push needs confirmation", confirmation="allow_force_push",
        action="push",
    )
    assert isinstance(exc, TaskConflictError)
    assert exc.code == "requires_confirmation"
    assert exc.confirmation == "allow_force_push" and exc.action == "push"
