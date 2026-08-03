"""Regression: process crash between journaling `handoff` and executing
`begin_deployment_switching` (docs/plans/local-deploy.md §7.2 steps 2→3).

The deployment row never left `staged` — the CAS to `switching` (§7.2 step 3)
never ran — but the journal already has a `handoff` line naming this op's
`to_slot`/`new_sha`. Boot reconciliation (§8) must still locate that `staged`
row via the journal-derived `target_slot`/`target_sha` fallback and drive it
to a terminal state; it must never finish the op while leaving the deployment
row orphaned in `staged`.
"""
from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

import server.switcher as switcher
from server.config import settings
from server.database import Database
from server.task_board.repository import TaskRepository
from server.task_board.workspaces import prepare_workspace

from tests.test_task_deploy_switch import (
    FakeStageRunner,
    _coord,
    _init_layout,
    _init_repo,
    _manager,
)


@pytest.fixture
async def store(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "task_workspaces_dir", str(tmp_path / "wsroot"))
    db_path = tmp_path / "owlery.db"
    db = Database(str(db_path))
    await db.initialize()
    repo = TaskRepository(str(db_path))
    await repo.initialize()
    agent_id = (await db.load_agents())[0]["id"]
    yield db, repo, tmp_path, agent_id
    await repo.close()
    await db.close()


async def _staged(db, repo, tmp, agent, monkeypatch):
    src = tmp / "src"
    _init_repo(src)
    board = await repo.create_board(
        name=f"D-{uuid.uuid4().hex[:8]}", working_dir=str(src),
        default_workspace_mode="git_worktree", allow_local_deploy=True,
    )
    task = await repo.create_task(
        board_id=board.id, title="Ship it", status="todo", assignee_agent_id=agent
    )
    run_id = uuid.uuid4().hex[:12]
    planned = str(Path(settings.resolved_task_workspaces_dir) / task.id / run_id)
    run = await repo.claim_ready(
        task.id, workspace_mode="git_worktree", workspace_path=planned, run_id=run_id
    )
    prepared = await prepare_workspace(
        mode="git_worktree", source_dir=str(src), task_id=task.id,
        run_id=run.id, attempt_no=run.attempt_no,
    )
    wt = Path(prepared.path)
    (wt / "g.txt").write_text("worker change\n")
    subprocess.run(["git", "add", "."], cwd=str(wt), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "worker change"], cwd=str(wt), check=True, capture_output=True
    )
    await repo.set_run_metadata(task.id, run.id, {"prepared": prepared.metadata})
    await repo.complete_run(task.id, run.id, summary="did the work")
    coord = _coord(db, repo)
    delivery = await coord.accept(task.id, run.id)
    layout = _init_layout(tmp / "deploy", monkeypatch)
    delivery = await coord.deploy_stage(
        task.id, run.id, server_root=layout.slot_path("a"), stage_runner=FakeStageRunner()
    )
    staged = await repo.get_staged_deployment_for_delivery(delivery.id)
    assert staged is not None and staged.slot == "b" and staged.state == "staged"
    return task, await repo.get_run(run.id), delivery, layout, coord, staged


async def _crash_before_bind(db, repo, tmp, agent, monkeypatch):
    """Replay §7.2 steps 1-2 (snapshot + journal `handoff`) and then simulate a
    crash exactly before step 3 (`begin_deployment_switching`) ever runs — the
    deployment row is left `staged`, still bound to the earlier `deploy_stage`
    op id, while the journal already names this switch op's `to_slot`/`new_sha`.
    """
    task, run, delivery, layout, coord, staged = await _staged(db, repo, tmp, agent, monkeypatch)
    pre_bind_op_id = staged.op_id  # the deploy_stage op, never a switch op

    op_id = await coord._plan_and_start_switch_op(delivery, "user", None)
    op = next(o for o in await repo.list_delivery_ops(delivery.id) if o.id == op_id)

    # Step 1 (§7.2): checkpoint + snapshot.
    busy, _, _ = await db.wal_checkpoint_truncate()
    assert busy == 0
    layout.snapshots_path.mkdir(exist_ok=True)
    snapshot_path = layout.snapshots_path / f"{op.id}.db"
    shutil.copyfile(db.path, str(snapshot_path))

    # Step 2 (§7.2): journal `handoff` BEFORE the deployment CAS.
    handoff_detail = {
        "from_slot": "a", "to_slot": staged.slot,
        "old_sha": "oldsha0000", "new_sha": staged.sha,
        "old_pid": 1234, "host": "127.0.0.1", "port": 8000,
        "db_path": db.path, "snapshot_path": str(snapshot_path),
        "serve_argv": ["serve"], "serve_env": {},
    }
    journal = switcher.Journal(str(layout.journal_path))
    journal.append(op.id, switcher.STEP_HANDOFF, handoff_detail)

    # CRASH: step 3 (`begin_deployment_switching`) never runs. The deployment
    # row is exactly as `deploy_stage` left it.
    still_staged = await repo.get_deployment(staged.id)
    assert still_staged.state == "staged"
    assert still_staged.op_id == pre_bind_op_id  # never bound to the switch op

    return task, run, delivery, layout, coord, staged, op


@pytest.mark.asyncio
async def test_reconcile_handoff_before_bind_settles_orphaned_deployment(store, monkeypatch):
    """Boot after a handoff-only journal tail (no `flip_done` line ever
    written, matching the switcher not having started — or having died before
    its first write) must find the still-`staged`, never-bound deployment row
    via the journal fallback and finalize it — never leave a `staged`/
    `switching` row behind while the op itself goes terminal."""
    db, repo, tmp, agent = store
    task, run, delivery, layout, coord, staged, op = await _crash_before_bind(
        db, repo, tmp, agent, monkeypatch
    )
    m = _manager(db, repo)

    probation = await m.reconcile_deploy_switch_ops()

    assert probation is None
    op_row = next(o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_switch")
    assert op_row.state == "interrupted"
    dep = await repo.get_deployment(staged.id)
    assert dep.state == "failed"  # never left staged/switching
    assert dep.op_id == op.id  # rebound to the switch op that owns this terminal
    d = await repo.get_delivery(delivery.id)
    assert d.status == "blocked" and d.reason_kind == "interrupted"


@pytest.mark.asyncio
async def test_reconcile_after_rolled_back_before_bind(store, monkeypatch):
    """Same crash window (deployment never bound), but the journal tail is
    `rolled_back` rather than handoff-only — exercising the `target_slot`/
    `target_sha` fallback through `_apply_switch_terminal`'s terminal-step
    branches (§8), not just `reconcile_deploy_switch_ops`'s own interrupted
    branch. Reconciliation must still land the never-bound row on
    `rolled_back`, never leave it orphaned `staged` while the op itself goes
    terminal (§8: op and deployment must never disagree)."""
    db, repo, tmp, agent = store
    task, run, delivery, layout, coord, staged, op = await _crash_before_bind(
        db, repo, tmp, agent, monkeypatch
    )
    journal = switcher.Journal(str(layout.journal_path))
    journal.append(op.id, switcher.STEP_ROLLED_BACK, {"reason": "health_timeout"})
    m = _manager(db, repo)

    probation = await m.reconcile_deploy_switch_ops()

    assert probation is None
    op_row = next(o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_switch")
    assert op_row.state == "failed"
    dep = await repo.get_deployment(staged.id)
    assert dep.state == "rolled_back"
    assert dep.op_id == op.id
    d = await repo.get_delivery(delivery.id)
    assert d.status == "blocked" and d.reason_kind == "health_failed"
