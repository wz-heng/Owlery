"""Coordinator tests for the Git delivery closure — real isolated temp git
repos, a fake hosting platform (no network), covering accept/commit/push/PR/
merge/teardown, the destructive guards, connector resolution (B3), and the
off-critical-path interrupted-PR reconcile (S3).
"""
from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path

import pytest

from server.config import settings
from server.database import Database
from server.deploy_admission import DeployAdmissionGate
from server.session_manager import SessionManager
from server.task_board import delivery as delivery_module
from server.task_board.delivery import DeliveryCoordinator
from server.task_board.manager import TaskBoardManager
from server.task_board.models import DeliveryConfirmationRequired, TaskConflictError
from server.task_board.repository import TaskRepository
from server.task_board.workspaces import prepare_workspace


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


class FakeConnectors:
    def __init__(self, installs: dict[str, list[tuple[str, str, bool]]]):
        self.installs = installs
        self.pr_calls: list[dict] = []

    async def get_agent_connector_ids(self, agent_id):
        return [iid for iid, _, _ in self.installs.get(agent_id, [])]

    async def get_installation(self, iid):
        for lst in self.installs.values():
            for i, kind, nr in lst:
                if i == iid:
                    return {"id": i, "kind": kind, "needs_reconnect": nr}
        return None

    async def get_access_token(self, iid):
        return {"access_token": f"tok-{iid}"}


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


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "a@b.c")
    _git(path, "config", "user.name", "T")
    (path / "f.txt").write_text("base\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    _git(path, "branch", "-M", "main")
    return path.name


async def _ready_delivery(db, repo, tmp, agent, *, worker_commits=True, origin_session_id=None):
    src = tmp / "src"
    _init_repo(src)
    board = await repo.create_board(
        name=f"D-{uuid.uuid4().hex[:8]}",
        working_dir=str(src),
        default_workspace_mode="git_worktree",
    )
    task = await repo.create_task(
        board_id=board.id, title="Ship it", status="todo", assignee_agent_id=agent,
        origin_session_id=origin_session_id,
    )
    run_id = uuid.uuid4().hex[:12]
    planned = str((Path(settings.resolved_task_workspaces_dir) / task.id / run_id))
    run = await repo.claim_ready(
        task.id, workspace_mode="git_worktree", workspace_path=planned, run_id=run_id
    )
    prepared = await prepare_workspace(
        mode="git_worktree", source_dir=str(src), task_id=task.id,
        run_id=run.id, attempt_no=run.attempt_no,
    )
    assert prepared.path == planned
    wt = Path(prepared.path)
    (wt / "g.txt").write_text("worker change\n")
    if worker_commits:
        _git(wt, "add", ".")
        _git(wt, "commit", "-qm", "worker change")
    await repo.set_run_metadata(task.id, run.id, {"prepared": prepared.metadata})
    await repo.complete_run(task.id, run.id, summary="did the work")
    return board, task, await repo.get_run(run.id), src, wt


def _coord(db, repo, connectors):
    c = DeliveryCoordinator()
    c.bind(db=db, connectors=connectors, notify_terminal=None, repo=repo)
    return c


@pytest.fixture
async def bound_manager(store):
    """A DeliveryCoordinator wired exactly like production — through
    `TaskBoardManager.bind` — so the real terminal-notification pipeline
    (source-key derivation, bypass isolation) is exercised end to end, not a
    `notify_terminal=None` stub (task-board-gaps open-pr-500.md)."""
    db, repo, tmp, agent = store
    sessions = SessionManager()
    await sessions.initialize(db)
    sessions.pause_session_injection_dispatch()
    manager = TaskBoardManager()
    # A fresh coordinator, never the process-wide `delivery_coordinator`
    # singleton `TaskBoardManager.__init__` defaults to — this test mutates
    # `create_pr`/`find_pr` test seams and must not leak them across tests.
    manager.delivery = DeliveryCoordinator()
    manager.bind(session_mgr=sessions, db=db, repo=repo)
    try:
        yield manager, sessions
    finally:
        sessions.remove_broadcast(manager.BROADCAST_KEY)


def _bind_admission(coord: DeliveryCoordinator, gate: DeployAdmissionGate) -> None:
    async def _broadcast(_payload):
        return None

    coord.bind_deploy(
        quiesce=object(),
        broadcast_restarting=_broadcast,
        request_shutdown=lambda: None,
        admission_gate=gate,
    )


@pytest.mark.asyncio
async def test_accept_captures_baseline(store):
    db, repo, tmp, agent = store
    board, task, run, src, wt = await _ready_delivery(db, repo, tmp, agent)
    coord = _coord(db, repo, FakeConnectors({}))
    d = await coord.accept(task.id, run.id)
    assert d.status == "ready"
    assert d.base_ref == "main" and d.base_head
    assert d.commits_ahead == 1 and d.dirty is False
    assert d.diffstat and d.diffstat["files"] == 1


@pytest.mark.asyncio
async def test_commit_dirty_then_ready(store):
    db, repo, tmp, agent = store
    board, task, run, src, wt = await _ready_delivery(db, repo, tmp, agent, worker_commits=False)
    coord = _coord(db, repo, FakeConnectors({}))
    d = await coord.accept(task.id, run.id)
    assert d.dirty is True and d.commits_ahead == 0
    d = await coord.deliver_op(task.id, run.id, kind="commit")
    assert d.status == "ready" and d.dirty is False and d.commits_ahead == 1
    # commit authorship is the owned Owlery identity.
    head_author = subprocess.run(
        ["git", "log", "-1", "--format=%an"], cwd=str(wt), capture_output=True, text=True
    ).stdout.strip()
    assert head_author.startswith("Owlery Task")


@pytest.mark.asyncio
async def test_closed_admission_rejects_delivery_op_before_it_is_planned(store):
    db, repo, tmp, agent = store
    _board, task, run, _src, _wt = await _ready_delivery(
        db, repo, tmp, agent, worker_commits=False
    )
    coord = _coord(db, repo, FakeConnectors({}))
    gate = DeployAdmissionGate()
    _bind_admission(coord, gate)
    await coord.accept(task.id, run.id)
    await gate.close()

    with pytest.raises(TaskConflictError, match="deploy admission is closed"):
        await coord.deliver_op(task.id, run.id, kind="commit")

    delivery = await repo.get_delivery_by_run(run.id)
    assert await repo.list_delivery_ops(delivery.id) == []


@pytest.mark.asyncio
async def test_closed_admission_rejects_teardown_before_retention_write(store):
    db, repo, tmp, agent = store
    _board, task, run, _src, _wt = await _ready_delivery(db, repo, tmp, agent)
    coord = _coord(db, repo, FakeConnectors({}))
    gate = DeployAdmissionGate()
    _bind_admission(coord, gate)
    before = await coord.accept(task.id, run.id)
    await gate.close()

    with pytest.raises(TaskConflictError, match="deploy admission is closed"):
        await coord.teardown(task.id, run.id, retention="keep")

    after = await repo.get_delivery(before.id)
    assert after.retention == before.retention
    assert await repo.list_delivery_ops(after.id) == []


@pytest.mark.asyncio
async def test_close_waits_for_delivery_claim_then_census_sees_running_op(store, monkeypatch):
    db, repo, tmp, agent = store
    _board, task, run, _src, _wt = await _ready_delivery(
        db, repo, tmp, agent, worker_commits=False
    )
    coord = _coord(db, repo, FakeConnectors({}))
    gate = DeployAdmissionGate()
    _bind_admission(coord, gate)
    await coord.accept(task.id, run.id)

    entered_start = asyncio.Event()
    release_start = asyncio.Event()
    commit_started = asyncio.Event()
    release_commit = asyncio.Event()
    original_start = repo.start_op
    original_commit = delivery_module.ws.commit_all

    async def blocked_start(*args, **kwargs):
        entered_start.set()
        await release_start.wait()
        return await original_start(*args, **kwargs)

    async def blocked_commit(*args, **kwargs):
        commit_started.set()
        await release_commit.wait()
        return await original_commit(*args, **kwargs)

    monkeypatch.setattr(repo, "start_op", blocked_start)
    monkeypatch.setattr(delivery_module.ws, "commit_all", blocked_commit)
    operation = asyncio.create_task(coord.deliver_op(task.id, run.id, kind="commit"))
    try:
        await asyncio.wait_for(entered_start.wait(), timeout=1)
        closing = asyncio.create_task(gate.close())
        await asyncio.sleep(0)
        assert not closing.done()

        release_start.set()
        await asyncio.wait_for(commit_started.wait(), timeout=1)
        await asyncio.wait_for(closing, timeout=1)
        delivery = await repo.get_delivery_by_run(run.id)
        ops = await repo.list_running_delivery_ops()
        assert [(op.delivery_id, op.kind) for op in ops] == [(delivery.id, "commit")]
    finally:
        release_start.set()
        release_commit.set()
        await operation


@pytest.mark.asyncio
async def test_push_to_local_remote_and_force_guard(store):
    db, repo, tmp, agent = store
    board, task, run, src, wt = await _ready_delivery(db, repo, tmp, agent)
    bare = tmp / "remote.git"
    _git(tmp, "init", "-q", "--bare", str(bare))
    _git(src, "remote", "add", "origin", str(bare))
    coord = _coord(db, repo, FakeConnectors({}))
    await coord.accept(task.id, run.id)
    d = await coord.deliver_op(task.id, run.id, kind="push")
    assert d.status == "delivered" and d.pushed_ref == f"refs/heads/{d.attempt_branch}"

    # Nothing-to-deliver: a zero-commit delivery cannot push.
    board2, task2, run2, src2, wt2 = await _ready_delivery(
        db, repo, tmp / "b", agent, worker_commits=False
    )
    _git(tmp / "b" / "src", "remote", "add", "origin", str(bare))
    coord2 = _coord(db, repo, FakeConnectors({}))
    await coord2.accept(task2.id, run2.id)
    with pytest.raises(TaskConflictError):
        await coord2.deliver_op(task2.id, run2.id, kind="push")


@pytest.mark.asyncio
async def test_push_repeat_trigger_returns_existing_result_without_pushing_again(
    store, monkeypatch
):
    """task-board-gaps.md §3.5: a repeat trigger of an already-succeeded
    external op (a second "Push" click) surfaces the existing result instead
    of re-attempting it — no second git push, no bare conflict."""
    db, repo, tmp, agent = store
    board, task, run, src, wt = await _ready_delivery(db, repo, tmp, agent)
    bare = tmp / "remote.git"
    _git(tmp, "init", "-q", "--bare", str(bare))
    _git(src, "remote", "add", "origin", str(bare))
    coord = _coord(db, repo, FakeConnectors({}))
    await coord.accept(task.id, run.id)
    first = await coord.deliver_op(task.id, run.id, kind="push")
    assert first.status == "delivered"

    push_calls = {"count": 0}
    original_push = delivery_module.ws.push_branch

    async def counting_push(*args, **kwargs):
        push_calls["count"] += 1
        return await original_push(*args, **kwargs)

    monkeypatch.setattr(delivery_module.ws, "push_branch", counting_push)
    second = await coord.deliver_op(task.id, run.id, kind="push")
    assert push_calls["count"] == 0
    assert second.status == "delivered"
    assert second.pushed_ref == first.pushed_ref
    ops = await repo.list_delivery_ops(first.id)
    assert sum(1 for op in ops if op.kind == "push") == 1  # no new op planned


@pytest.mark.asyncio
async def test_connector_resolution_cases(store):
    db, repo, tmp, agent = store
    coord = _coord(db, repo, FakeConnectors({
        "a1": [("i1", "github", False)],
        "a2": [("i2", "github", False), ("i3", "github", False)],
        "a3": [("i4", "gmail", False)],
        "a4": [("i5", "github", True)],
    }))
    assert (await coord._resolve_connector("a1", None))["installation_id"] == "i1"
    assert (await coord._resolve_connector("a2", None))["error"] == "ambiguous_connector"
    assert (await coord._resolve_connector("a3", None))["error"] == "no_connector"
    assert (await coord._resolve_connector("a4", None))["error"] == "no_connector"  # needs_reconnect
    # Explicit selection among ambiguous.
    assert (await coord._resolve_connector("a2", "i3"))["installation_id"] == "i3"
    # A caller cannot borrow an install that is not the run-agent's.
    assert (await coord._resolve_connector("a1", "i2"))["error"] == "no_connector"


@pytest.mark.asyncio
async def test_pull_request_via_fake_platform(store):
    db, repo, tmp, agent = store
    board, task, run, src, wt = await _ready_delivery(db, repo, tmp, agent)
    connectors = FakeConnectors({agent: [("gh1", "github", False)]})
    coord = _coord(db, repo, connectors)

    captured = {}
    call_count = {"n": 0}

    async def fake_create_pr(token, owner, r, *, title, body, head, base, draft):
        call_count["n"] += 1
        captured.update(token=token, owner=owner, repo=r, base=base, head=head, draft=draft)
        return {"number": 42, "url": "https://github.com/acme/widgets/pull/42", "state": "open"}

    coord.create_pr = fake_create_pr
    await coord.accept(task.id, run.id)
    # Simulate a completed push against a github remote (PR needs pushed_ref + github url).
    await repo.conn.execute(
        "UPDATE task_deliveries SET status='delivered', pushed_ref='refs/heads/x', "
        "remote_url='https://github.com/acme/widgets.git' WHERE run_id=?",
        (run.id,),
    )
    await repo.conn.commit()
    d = await coord.deliver_op(task.id, run.id, kind="pull_request")
    assert d.status == "delivered" and d.pr_number == 42
    assert captured["owner"] == "acme" and captured["repo"] == "widgets"
    assert captured["base"] == "main" and captured["token"] == "tok-gh1"
    assert call_count["n"] == 1

    # Repeat trigger (task-board-gaps.md §3.5): a second "Open PR" click
    # surfaces the existing PR (number + link) instead of re-POSTing to
    # GitHub — no bare conflict, no duplicate-PR 422 from the platform.
    second = await coord.deliver_op(task.id, run.id, kind="pull_request")
    assert second.status == "delivered"
    assert second.pr_number == 42 and second.pr_url == d.pr_url
    assert call_count["n"] == 1  # fake_create_pr not invoked again
    ops = await repo.list_delivery_ops(d.id)
    assert sum(1 for op in ops if op.kind == "pull_request") == 1


@pytest.mark.asyncio
async def test_pull_request_no_connector_blocks(store):
    db, repo, tmp, agent = store
    board, task, run, src, wt = await _ready_delivery(db, repo, tmp, agent)
    coord = _coord(db, repo, FakeConnectors({}))  # agent has no connector
    await coord.accept(task.id, run.id)
    await repo.conn.execute(
        "UPDATE task_deliveries SET status='delivered', pushed_ref='refs/heads/x', "
        "remote_url='https://github.com/acme/widgets.git' WHERE run_id=?",
        (run.id,),
    )
    await repo.conn.commit()
    d = await coord.deliver_op(task.id, run.id, kind="pull_request")
    assert d.status == "blocked" and d.reason_kind == "no_connector"


@pytest.mark.asyncio
async def test_interrupted_pr_reconcile_reads_never_creates(store):
    db, repo, tmp, agent = store
    board, task, run, src, wt = await _ready_delivery(db, repo, tmp, agent)
    connectors = FakeConnectors({agent: [("gh1", "github", False)]})
    coord = _coord(db, repo, connectors)
    await coord.accept(task.id, run.id)
    d = await repo.get_delivery_by_run(run.id)
    await repo.conn.execute(
        "UPDATE task_deliveries SET remote_url='https://github.com/acme/widgets.git', "
        "base_ref='main', pushed_ref='refs/heads/x' WHERE id=?", (d.id,)
    )
    await repo.conn.commit()
    # A PR op that got interrupted mid-POST.
    op = await repo.plan_op(d.id, kind="pull_request", source_key="k:pr",
                            request={"installation_id": "gh1"}, actor_kind="user")
    await repo.start_op(d.id, op.id)
    (interrupted,) = await repo.interrupt_running_delivery_ops(reason="restart")
    assert interrupted[0].status == "blocked" and interrupted[0].reason_kind == "interrupted"

    created = {"count": 0}

    async def fake_find(token, owner, r, *, head_owner, branch):
        return {"number": 7, "url": "https://github.com/acme/widgets/pull/7", "state": "open"}

    async def fake_create(*a, **k):
        created["count"] += 1
        raise AssertionError("reconcile must never create a PR")

    coord.find_pr = fake_find
    coord.create_pr = fake_create
    reconciled, settle_op_id = await coord.reconcile_interrupted_pr(await repo.get_delivery(d.id))
    assert reconciled.status == "delivered" and reconciled.pr_number == 7
    assert created["count"] == 0
    # The reconcile settles the interrupted op itself, not just the delivery
    # row — so a later terminal-notification lookup finds a genuine succeeded
    # event instead of treating this as having no settle at all.
    assert settle_op_id == op.id
    settled_ops = await repo.list_delivery_ops(d.id)
    (settled_pr_op,) = [o for o in settled_ops if o.kind == "pull_request"]
    assert settled_pr_op.state == "succeeded"


@pytest.mark.asyncio
async def test_record_pr_reconcile_is_a_noop_once_the_delivery_is_no_longer_interrupted(store):
    """open-pr-500.md §4 should-1 (Snape round-2): a concurrent path (e.g. a
    live PR retry that independently creates the same PR) can resolve the
    delivery to `delivered` before this reconcile's own write runs. The
    caller must not infer "I settled this" from `updated.status != <stale
    snapshot>.status` — that stale comparison would be true even though this
    call never touched the op row. `record_pr_reconcile` must report
    `reconciled=False` and leave the (already-succeeded, by the concurrent
    path) op row alone."""
    db, repo, tmp, agent = store
    board, task, run, src, wt = await _ready_delivery(db, repo, tmp, agent)
    coord = _coord(db, repo, FakeConnectors({}))
    await coord.accept(task.id, run.id)
    d = await repo.get_delivery_by_run(run.id)
    op = await repo.plan_op(d.id, kind="pull_request", source_key="k:pr",
                            request={"installation_id": "gh1"}, actor_kind="user")
    await repo.start_op(d.id, op.id)
    await repo.interrupt_running_delivery_ops(reason="restart")

    # Simulate the concurrent live retry: it independently created the same
    # PR through its OWN (different) op and already settled the delivery.
    other_op = await repo.plan_op(d.id, kind="pull_request", source_key="k:pr:retry:1",
                                  request={"installation_id": "gh1"}, actor_kind="user")
    await repo.start_op(d.id, other_op.id, allowed_statuses=frozenset({"blocked"}))
    await repo.finish_op(
        d.id, other_op.id, state="succeeded", delivery_status="delivered",
        delivery_fields={"pr_number": 7, "pr_url": "https://github.com/acme/widgets/pull/7",
                         "pr_state": "open"},
        result={"number": 7},
    )

    # The reconcile for the STALE interrupted op now runs, unaware the
    # delivery already resolved.
    updated, reconciled = await repo.record_pr_reconcile(
        d.id, op_id=op.id,
        pr_number=7, pr_url="https://github.com/acme/widgets/pull/7", pr_state="open",
    )
    assert reconciled is False
    assert updated.status == "delivered" and updated.pr_number == 7

    stale_op = next(o for o in await repo.list_delivery_ops(d.id) if o.id == op.id)
    assert stale_op.state == "interrupted"  # untouched — this call never wrote to it


@pytest.mark.asyncio
async def test_record_pr_reconcile_never_folds_the_delivery_without_settling_the_op(store):
    """A mismatched `op_id` (or one that isn't the interrupted `pull_request`
    op) must leave BOTH rows untouched — the op ledger and the delivery row
    move together, never one without the other."""
    db, repo, tmp, agent = store
    board, task, run, src, wt = await _ready_delivery(db, repo, tmp, agent)
    coord = _coord(db, repo, FakeConnectors({}))
    await coord.accept(task.id, run.id)
    d = await repo.get_delivery_by_run(run.id)
    op = await repo.plan_op(d.id, kind="pull_request", source_key="k:pr",
                            request={"installation_id": "gh1"}, actor_kind="user")
    await repo.start_op(d.id, op.id)
    await repo.interrupt_running_delivery_ops(reason="restart")

    updated, reconciled = await repo.record_pr_reconcile(
        d.id, op_id="does-not-exist",
        pr_number=7, pr_url="https://github.com/acme/widgets/pull/7", pr_state="open",
    )
    assert reconciled is False
    assert updated.status == "blocked" and updated.pr_number is None

    unchanged_op = next(o for o in await repo.list_delivery_ops(d.id) if o.id == op.id)
    assert unchanged_op.state == "interrupted"


@pytest.mark.asyncio
async def test_teardown_removes_worktree_and_dirty_guard(store):
    db, repo, tmp, agent = store
    board, task, run, src, wt = await _ready_delivery(db, repo, tmp, agent)
    coord = _coord(db, repo, FakeConnectors({}))
    await coord.accept(task.id, run.id)
    # Make the worktree live-dirty AFTER accept; teardown must re-inspect (S1).
    (wt / "late.txt").write_text("late uncommitted\n")
    with pytest.raises(DeliveryConfirmationRequired) as exc:
        await coord.teardown(
            task.id, run.id, retention="remove_worktree_keep_branch"
        )
    assert exc.value.confirmation == "force_discard_dirty"
    d = await coord.teardown(
        task.id,
        run.id,
        retention="remove_worktree_keep_branch",
        confirmations={"force_discard_dirty": True},
    )
    assert d.retention == "remove_worktree_keep_branch"
    assert not wt.exists()
    # Worktree is deregistered, not just deleted.
    listing = subprocess.run(
        ["git", "worktree", "list"], cwd=str(src), capture_output=True, text=True
    ).stdout
    assert str(wt) not in listing


@pytest.mark.asyncio
async def test_teardown_keep_policy_preserves_worktree(store):
    db, repo, tmp, agent = store
    _board, task, run, _src, wt = await _ready_delivery(db, repo, tmp, agent)
    coord = _coord(db, repo, FakeConnectors({}))
    await coord.accept(task.id, run.id)

    d = await coord.teardown(task.id, run.id, retention="keep")

    assert d.retention == "keep"
    assert wt.is_dir()
    assert await repo.list_delivery_ops(d.id) == []


@pytest.mark.asyncio
async def test_teardown_after_push_and_pr_republishes_under_the_pr_op_key(
    bound_manager, store,
):
    """open-pr-500.md §4 blocker-1 (Snape round-2): teardown republishes
    whatever terminal status the delivery already reached, using one atomic
    (delivery, settle_op_id) snapshot (`current_terminal_snapshot`) rather
    than two separate reads that could straddle a concurrent goal op. This
    exercises the wiring end to end: after a real push-then-PR settle, tear
    down the delivery and confirm the republished notification is scoped to
    the PR op's key — never the push op's key, never the old unscoped key."""
    manager, sessions = bound_manager
    db, repo, tmp, agent = store
    origin = await sessions.create_session(agent, name="origin")
    board, task, run, src, wt = await _ready_delivery(
        db, repo, tmp, agent, origin_session_id=origin.id
    )
    bare = tmp / "remote.git"
    _git(tmp, "init", "-q", "--bare", str(bare))
    _git(src, "remote", "add", "origin", str(bare))
    manager.delivery.connectors = FakeConnectors({agent: [("gh1", "github", False)]})

    async def fake_create_pr(token, owner, r, *, title, body, head, base, draft):
        return {"number": 42, "url": "https://github.com/acme/widgets/pull/42", "state": "open"}

    manager.delivery.create_pr = fake_create_pr

    await manager.delivery.accept(task.id, run.id)
    await manager.delivery.deliver_op(task.id, run.id, kind="push")
    await repo.conn.execute(
        "UPDATE task_deliveries SET remote_url='https://github.com/acme/widgets.git' "
        "WHERE run_id=?", (run.id,),
    )
    await repo.conn.commit()
    opened = await manager.delivery.deliver_op(task.id, run.id, kind="pull_request")
    assert opened.status == "delivered" and opened.pr_number == 42

    ops = await repo.list_delivery_ops(opened.id)
    push_op = next(o for o in ops if o.kind == "push" and o.state == "succeeded")
    pr_op = next(o for o in ops if o.kind == "pull_request" and o.state == "succeeded")

    pr_key = f"task:{task.id}:run:{run.id}:delivery:terminal:{pr_op.id}"
    push_key = f"task:{task.id}:run:{run.id}:delivery:terminal:{push_op.id}"
    unscoped_key = f"task:{task.id}:run:{run.id}:delivery:terminal"
    # Push's own settle and PR's own settle each already fired their own
    # notification live, before teardown runs at all.
    pr_injection_before = await db.get_session_injection_by_source(pr_key)
    assert pr_injection_before is not None

    await manager.delivery.teardown(
        task.id, run.id, retention="remove_worktree_keep_branch",
        confirmations={"force_discard_dirty": True},
    )

    # Teardown republishes the delivery's CURRENT terminal status (still the
    # PR's) — it must land on the SAME pr_key (idempotent, no new row), never
    # fall back to push's key or the old unscoped key.
    pr_injection_after = await db.get_session_injection_by_source(pr_key)
    assert pr_injection_after is not None
    assert pr_injection_after["id"] == pr_injection_before["id"]
    assert await db.get_session_injection_by_source(unscoped_key) is None
    assert await db.get_session_injection_by_source(push_key) is not None


@pytest.mark.asyncio
async def test_merge_fast_forward_and_conflict(store):
    db, repo, tmp, agent = store
    board, task, run, src, wt = await _ready_delivery(db, repo, tmp, agent)
    coord = _coord(db, repo, FakeConnectors({}))
    await coord.accept(task.id, run.id)
    # ff-only merge into the clean base repo (on main).
    d = await coord.deliver_op(task.id, run.id, kind="merge", merge_strategy="fast_forward_only")
    assert d.status == "delivered" and d.merge_strategy == "fast_forward_only"
    assert (src / "g.txt").exists()  # merged into base working tree


@pytest.mark.asyncio
async def test_merge_conflict_reports_conflicted(store):
    db, repo, tmp, agent = store
    board, task, run, src, wt = await _ready_delivery(db, repo, tmp, agent)
    # Diverge the base so a fast-forward is impossible.
    (src / "f.txt").write_text("diverged base\n")
    _git(src, "commit", "-aqm", "diverge")
    coord = _coord(db, repo, FakeConnectors({}))
    await coord.accept(task.id, run.id)
    d = await coord.deliver_op(task.id, run.id, kind="merge", merge_strategy="fast_forward_only")
    assert d.status == "conflicted" and d.reason_kind == "conflict"


# --------------------------------------------------------------------------- #
# Supersede derivation (docs/plans/task-board-overhaul.md §3.1). These build
# delivery rows directly (bypassing the worktree-driven accept flow, which
# isn't what's under test) against a real shared git repo, so the coordinator's
# `git merge-base --is-ancestor` judgment runs against real history.
# --------------------------------------------------------------------------- #


def _rev(cwd, ref="HEAD") -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


async def _task_and_completed_run(repo, board, agent, workspace_path):
    task = await repo.create_task(
        board_id=board.id, title=f"Task {uuid.uuid4().hex[:6]}", status="todo",
        assignee_agent_id=agent,
    )
    run = await repo.claim_ready(
        task.id, workspace_mode="git_worktree", workspace_path=str(workspace_path)
    )
    await repo.complete_run(task.id, run.id, summary="did the work")
    return task, await repo.get_run(run.id)


async def _accept_with_head(
    repo,
    task,
    run,
    *,
    repository,
    attempt_branch,
    base_ref,
    base_head,
    attempt_head,
    dirty=False,
    commits_ahead=1,
):
    delivery = await repo.create_delivery(
        run.id,
        repository=repository,
        attempt_branch=attempt_branch,
        base_ref=base_ref,
        base_head=base_head,
    )
    await repo.start_accept(delivery.id)
    return await repo.record_baseline(
        delivery.id,
        status="ready",
        attempt_head=attempt_head,
        dirty=dirty,
        commits_ahead=commits_ahead,
    )


@pytest.mark.asyncio
async def test_supersession_direct_chain(store):
    db, repo, tmp, agent = store
    src = tmp / "src"
    _init_repo(src)
    base_head = _rev(src, "main")
    board = await repo.create_board(
        name="Chain", working_dir=str(src), default_workspace_mode="git_worktree"
    )
    coord = _coord(db, repo, FakeConnectors({}))

    _git(src, "checkout", "-b", "attemptA")
    (src / "a.txt").write_text("a\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "A")
    head_a = _rev(src)

    _git(src, "checkout", "-b", "attemptB")
    (src / "b.txt").write_text("b\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "B")
    head_b = _rev(src)
    _git(src, "checkout", "main")

    task_a, run_a = await _task_and_completed_run(repo, board, agent, tmp / "wt-a")
    await _accept_with_head(
        repo, task_a, run_a, repository=str(src), attempt_branch="attemptA",
        base_ref="main", base_head=base_head, attempt_head=head_a,
    )
    await coord.recompute_supersession(task_a.id, run_a.id)

    task_b, run_b = await _task_and_completed_run(repo, board, agent, tmp / "wt-b")
    await _accept_with_head(
        repo, task_b, run_b, repository=str(src), attempt_branch="attemptB",
        base_ref="main", base_head=base_head, attempt_head=head_b,
    )
    d_b = await coord.recompute_supersession(task_b.id, run_b.id)

    d_a = await repo.get_delivery_by_run(run_a.id)
    assert d_a.superseded_by_delivery_id == d_b.id
    assert d_b.superseded_by_delivery_id is None

    # The reverse lookup the delivery panel's batch-teardown affordance uses
    # (task-board-overhaul.md §3.1): B's own record of who it collapsed.
    assert [d.id for d in await repo.list_superseded_by(d_b.id)] == [d_a.id]
    assert await repo.list_superseded_by(d_a.id) == []


@pytest.mark.asyncio
async def test_supersession_diamond_neither_branch_supersedes_the_other(store):
    db, repo, tmp, agent = store
    src = tmp / "src"
    _init_repo(src)
    base_head = _rev(src, "main")
    board = await repo.create_board(
        name="Diamond", working_dir=str(src), default_workspace_mode="git_worktree"
    )
    coord = _coord(db, repo, FakeConnectors({}))

    _git(src, "checkout", "-b", "attemptA")
    (src / "a.txt").write_text("a\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "A")
    head_a = _rev(src)

    _git(src, "checkout", "-b", "attemptB", "attemptA")
    (src / "b.txt").write_text("b\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "B")
    head_b = _rev(src)

    _git(src, "checkout", "-b", "attemptC", "attemptA")
    (src / "c.txt").write_text("c\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "C")
    head_c = _rev(src)
    _git(src, "checkout", "main")

    task_a, run_a = await _task_and_completed_run(repo, board, agent, tmp / "wt-a")
    await _accept_with_head(
        repo, task_a, run_a, repository=str(src), attempt_branch="attemptA",
        base_ref="main", base_head=base_head, attempt_head=head_a,
    )
    await coord.recompute_supersession(task_a.id, run_a.id)

    task_b, run_b = await _task_and_completed_run(repo, board, agent, tmp / "wt-b")
    d_b = await _accept_with_head(
        repo, task_b, run_b, repository=str(src), attempt_branch="attemptB",
        base_ref="main", base_head=base_head, attempt_head=head_b,
    )
    await coord.recompute_supersession(task_b.id, run_b.id)

    task_c, run_c = await _task_and_completed_run(repo, board, agent, tmp / "wt-c")
    d_c = await _accept_with_head(
        repo, task_c, run_c, repository=str(src), attempt_branch="attemptC",
        base_ref="main", base_head=base_head, attempt_head=head_c,
    )
    await coord.recompute_supersession(task_c.id, run_c.id)

    d_a = await repo.get_delivery_by_run(run_a.id)
    d_b = await repo.get_delivery_by_run(run_b.id)
    d_c = await repo.get_delivery_by_run(run_c.id)
    # A is contained in both B and C; the first to settle wins its pointer.
    assert d_a.superseded_by_delivery_id == d_b.id
    # B and C are independent branches — neither collapses the other.
    assert d_b.superseded_by_delivery_id is None
    assert d_c.superseded_by_delivery_id is None


@pytest.mark.asyncio
async def test_supersession_dirty_delivery_never_marked_superseded(store):
    db, repo, tmp, agent = store
    src = tmp / "src"
    _init_repo(src)
    base_head = _rev(src, "main")
    board = await repo.create_board(
        name="Dirty", working_dir=str(src), default_workspace_mode="git_worktree"
    )
    coord = _coord(db, repo, FakeConnectors({}))

    _git(src, "checkout", "-b", "attemptA")
    (src / "a.txt").write_text("a\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "A")
    head_a = _rev(src)

    _git(src, "checkout", "-b", "attemptB")
    (src / "b.txt").write_text("b\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "B")
    head_b = _rev(src)
    _git(src, "checkout", "main")

    task_a, run_a = await _task_and_completed_run(repo, board, agent, tmp / "wt-a")
    await _accept_with_head(
        repo, task_a, run_a, repository=str(src), attempt_branch="attemptA",
        base_ref="main", base_head=base_head, attempt_head=head_a, dirty=True,
    )
    await coord.recompute_supersession(task_a.id, run_a.id)

    task_b, run_b = await _task_and_completed_run(repo, board, agent, tmp / "wt-b")
    await _accept_with_head(
        repo, task_b, run_b, repository=str(src), attempt_branch="attemptB",
        base_ref="main", base_head=base_head, attempt_head=head_b,
    )
    await coord.recompute_supersession(task_b.id, run_b.id)

    # A's committed head really is an ancestor of B, but A is dirty — its
    # uncommitted work is not contained in B's history, so it must never
    # collapse behind an Accept-only card.
    d_a = await repo.get_delivery_by_run(run_a.id)
    assert d_a.superseded_by_delivery_id is None


@pytest.mark.asyncio
async def test_supersession_survives_worktree_and_branch_teardown(store):
    """The judgment compares against the shared repository, not the run's
    worktree, so it keeps working after the worktree is removed — and even
    after the superseded delivery's OWN branch ref is deleted, since its
    commit stays reachable through the branch that contains it."""
    db, repo, tmp, agent = store
    src = tmp / "src"
    _init_repo(src)
    base_head = _rev(src, "main")
    board = await repo.create_board(
        name="Teardown", working_dir=str(src), default_workspace_mode="git_worktree"
    )
    coord = _coord(db, repo, FakeConnectors({}))

    _git(src, "checkout", "-b", "attemptA")
    (src / "a.txt").write_text("a\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "A")
    head_a = _rev(src)

    _git(src, "checkout", "-b", "attemptB")
    (src / "b.txt").write_text("b\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "B")
    head_b = _rev(src)
    _git(src, "checkout", "main")

    wt_a = tmp / "wt-a"
    task_a, run_a = await _task_and_completed_run(repo, board, agent, wt_a)
    await _accept_with_head(
        repo, task_a, run_a, repository=str(src), attempt_branch="attemptA",
        base_ref="main", base_head=base_head, attempt_head=head_a,
    )
    await coord.recompute_supersession(task_a.id, run_a.id)

    task_b, run_b = await _task_and_completed_run(repo, board, agent, tmp / "wt-b")
    await _accept_with_head(
        repo, task_b, run_b, repository=str(src), attempt_branch="attemptB",
        base_ref="main", base_head=base_head, attempt_head=head_b,
    )
    d_b = await coord.recompute_supersession(task_b.id, run_b.id)
    d_a = await repo.get_delivery_by_run(run_a.id)
    assert d_a.superseded_by_delivery_id == d_b.id

    # Simulate a completed teardown of the superseded delivery: its worktree
    # directory never exists (this test never created one — matching a real
    # `worktree_remove` outcome) and its own branch ref has been deleted
    # (retention policy remove_all after a successful collapse).
    assert not wt_a.exists()
    _git(src, "branch", "-D", "attemptA")

    reconciled = await coord.recompute_supersession(task_a.id, run_a.id)
    assert reconciled.superseded_by_delivery_id == d_b.id


@pytest.mark.asyncio
async def test_supersession_reverts_to_null_when_target_branch_deleted(store):
    """The false-revert bug T-C caught: deleting the CONVERGING party's
    (the target's) branch must not leave a stale collapse in place just
    because the commit object hasn't been garbage-collected yet. Unlike
    ``test_supersession_survives_worktree_and_branch_teardown`` — which
    deletes the SUPERSEDED delivery's own branch and expects the pointer to
    survive — this deletes the TARGET's branch with no other live ref
    (main/release) reaching its head, so the pointer must snap back to null.
    """
    db, repo, tmp, agent = store
    src = tmp / "src"
    _init_repo(src)
    base_head = _rev(src, "main")
    board = await repo.create_board(
        name="TargetTeardown", working_dir=str(src), default_workspace_mode="git_worktree"
    )
    coord = _coord(db, repo, FakeConnectors({}))

    _git(src, "checkout", "-b", "attemptA")
    (src / "a.txt").write_text("a\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "A")
    head_a = _rev(src)

    _git(src, "checkout", "-b", "attemptB")
    (src / "b.txt").write_text("b\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "B")
    head_b = _rev(src)
    _git(src, "checkout", "main")

    task_a, run_a = await _task_and_completed_run(repo, board, agent, tmp / "wt-a")
    await _accept_with_head(
        repo, task_a, run_a, repository=str(src), attempt_branch="attemptA",
        base_ref="main", base_head=base_head, attempt_head=head_a,
    )
    await coord.recompute_supersession(task_a.id, run_a.id)

    task_b, run_b = await _task_and_completed_run(repo, board, agent, tmp / "wt-b")
    await _accept_with_head(
        repo, task_b, run_b, repository=str(src), attempt_branch="attemptB",
        base_ref="main", base_head=base_head, attempt_head=head_b,
    )
    d_b = await coord.recompute_supersession(task_b.id, run_b.id)
    d_a = await repo.get_delivery_by_run(run_a.id)
    assert d_a.superseded_by_delivery_id == d_b.id

    # Delete the TARGET's (B's) branch — never merged into main/release, and
    # never pushed anywhere else, so no live ref reaches head_b anymore. The
    # commit object is still present (no gc ran), so a naive ancestor-only
    # check would keep reporting A as collapsed into B.
    _git(src, "branch", "-D", "attemptB")

    reconciled = await coord.recompute_supersession(task_a.id, run_a.id)
    assert reconciled.superseded_by_delivery_id is None


@pytest.mark.asyncio
async def test_supersession_survives_target_branch_deleted_after_merge_to_release_ref(store):
    """A deleted target branch is not automatically a false collapse: if the
    target's head was merged into the board's release ref (main, by default —
    task-board-overhaul.md §3.1) before its branch was cleaned up, that head
    is still live and the collapse must hold."""
    db, repo, tmp, agent = store
    src = tmp / "src"
    _init_repo(src)
    base_head = _rev(src, "main")
    board = await repo.create_board(
        name="ReleaseRef", working_dir=str(src), default_workspace_mode="git_worktree"
    )
    coord = _coord(db, repo, FakeConnectors({}))

    _git(src, "checkout", "-b", "attemptA")
    (src / "a.txt").write_text("a\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "A")
    head_a = _rev(src)

    _git(src, "checkout", "-b", "attemptB")
    (src / "b.txt").write_text("b\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "B")
    head_b = _rev(src)
    _git(src, "checkout", "main")

    task_a, run_a = await _task_and_completed_run(repo, board, agent, tmp / "wt-a")
    await _accept_with_head(
        repo, task_a, run_a, repository=str(src), attempt_branch="attemptA",
        base_ref="main", base_head=base_head, attempt_head=head_a,
    )
    await coord.recompute_supersession(task_a.id, run_a.id)

    task_b, run_b = await _task_and_completed_run(repo, board, agent, tmp / "wt-b")
    await _accept_with_head(
        repo, task_b, run_b, repository=str(src), attempt_branch="attemptB",
        base_ref="main", base_head=base_head, attempt_head=head_b,
    )
    d_b = await coord.recompute_supersession(task_b.id, run_b.id)
    d_a = await repo.get_delivery_by_run(run_a.id)
    assert d_a.superseded_by_delivery_id == d_b.id

    # Merge B into main (fast-forward), then delete B's branch — the standard
    # accept-and-clean-up flow. head_b is now reachable only via "main".
    _git(src, "merge", "--ff-only", "attemptB")
    _git(src, "branch", "-D", "attemptB")

    reconciled = await coord.recompute_supersession(task_a.id, run_a.id)
    assert reconciled.superseded_by_delivery_id == d_b.id


@pytest.mark.asyncio
async def test_supersession_forward_propagation_skips_non_live_target(store):
    """The forward-propagation pass (delivery.py's own settle collapsing
    still-open siblings it contains) must gate on ITS OWN liveness too, not
    just the reverse-lookup paths: if the delivery being recomputed has
    already lost its only live ref before its first settle, it must not
    collapse A into it (Snape's T-E review)."""
    db, repo, tmp, agent = store
    src = tmp / "src"
    _init_repo(src)
    base_head = _rev(src, "main")
    board = await repo.create_board(
        name="ForwardGate", working_dir=str(src), default_workspace_mode="git_worktree"
    )
    coord = _coord(db, repo, FakeConnectors({}))

    _git(src, "checkout", "-b", "attemptA")
    (src / "a.txt").write_text("a\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "A")
    head_a = _rev(src)

    _git(src, "checkout", "-b", "attemptB")
    (src / "b.txt").write_text("b\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "B")
    head_b = _rev(src)
    _git(src, "checkout", "main")

    task_a, run_a = await _task_and_completed_run(repo, board, agent, tmp / "wt-a")
    await _accept_with_head(
        repo, task_a, run_a, repository=str(src), attempt_branch="attemptA",
        base_ref="main", base_head=base_head, attempt_head=head_a,
    )
    await coord.recompute_supersession(task_a.id, run_a.id)

    task_b, run_b = await _task_and_completed_run(repo, board, agent, tmp / "wt-b")
    await _accept_with_head(
        repo, task_b, run_b, repository=str(src), attempt_branch="attemptB",
        base_ref="main", base_head=base_head, attempt_head=head_b,
    )

    # B's branch is gone — never merged to main — before its first settle
    # ever runs. head_b is still a strict ancestor-containing commit of
    # nothing relevant here, but head_a IS an ancestor of head_b, so a naive
    # forward-propagation pass would collapse A into B regardless.
    _git(src, "branch", "-D", "attemptB")

    await coord.recompute_supersession(task_b.id, run_b.id)
    d_a = await repo.get_delivery_by_run(run_a.id)
    assert d_a.superseded_by_delivery_id is None


@pytest.mark.asyncio
async def test_supersession_reverts_to_null_when_ancestry_breaks(store):
    """If the superseding branch's history is rewritten so it no longer
    contains the collapsed delivery's commit, the next reconcile must snap
    the stale pointer back to null rather than leave a dangling collapse."""
    db, repo, tmp, agent = store
    src = tmp / "src"
    _init_repo(src)
    base_head = _rev(src, "main")
    board = await repo.create_board(
        name="Rewrite", working_dir=str(src), default_workspace_mode="git_worktree"
    )
    coord = _coord(db, repo, FakeConnectors({}))

    _git(src, "checkout", "-b", "attemptA")
    (src / "a.txt").write_text("a\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "A")
    head_a = _rev(src)

    _git(src, "checkout", "-b", "attemptB")
    (src / "b.txt").write_text("b\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "B")
    head_b = _rev(src)
    _git(src, "checkout", "main")

    task_a, run_a = await _task_and_completed_run(repo, board, agent, tmp / "wt-a")
    await _accept_with_head(
        repo, task_a, run_a, repository=str(src), attempt_branch="attemptA",
        base_ref="main", base_head=base_head, attempt_head=head_a,
    )
    await coord.recompute_supersession(task_a.id, run_a.id)

    task_b, run_b = await _task_and_completed_run(repo, board, agent, tmp / "wt-b")
    delivery_b = await _accept_with_head(
        repo, task_b, run_b, repository=str(src), attempt_branch="attemptB",
        base_ref="main", base_head=base_head, attempt_head=head_b,
    )
    await coord.recompute_supersession(task_b.id, run_b.id)
    d_a = await repo.get_delivery_by_run(run_a.id)
    assert d_a.superseded_by_delivery_id is not None

    # Rewrite attemptB's history so it no longer descends from attemptA's tip.
    _git(src, "checkout", "attemptB")
    _git(src, "reset", "--hard", "main")
    (src / "rewritten.txt").write_text("rewritten\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "rewritten")
    new_head_b = _rev(src)
    _git(src, "checkout", "main")
    assert new_head_b != head_b

    await repo.start_accept(delivery_b.id)
    await repo.record_baseline(
        delivery_b.id, status="ready", attempt_head=new_head_b,
        dirty=False, commits_ahead=1,
    )
    await coord.recompute_supersession(task_b.id, run_b.id)
    reconciled_a = await coord.recompute_supersession(task_a.id, run_a.id)
    assert reconciled_a.superseded_by_delivery_id is None


# --- Open-PR-500 regression coverage (task-board-gaps open-pr-500.md) -------
#
# Forensics: push settling `delivered` fires a terminal notification under a
# source_key fixed per task+run; PR settling the SAME delivery `delivered`
# reused that identical key with a different payload, so the injection
# layer's replay guard raised — and, uncaught past the router, turned an
# already-successful PR op into a bare 500. The four tests below cover the
# ticket's four required fixes: a settle-scoped idempotency key (so two real
# events never share one key), bypass isolation (a notify failure never fails
# the op), an `_op_pr` idempotency guard + 422-already-exists reconcile, and
# the boot self-heal for the two rows that shape of bug had already corrupted.


@pytest.mark.asyncio
async def test_push_then_pr_terminal_notifications_both_land_no_500(bound_manager, store):
    """The root-cause repro: push settles `delivered`, then PR settles the
    SAME delivery `delivered` again with different content. Both must reach
    the origin session as distinct messages — neither call may raise."""
    manager, sessions = bound_manager
    db, repo, tmp, agent = store
    origin = await sessions.create_session(agent, name="origin")
    board, task, run, src, wt = await _ready_delivery(
        db, repo, tmp, agent, origin_session_id=origin.id
    )
    bare = tmp / "remote.git"
    _git(tmp, "init", "-q", "--bare", str(bare))
    _git(src, "remote", "add", "origin", str(bare))
    manager.delivery.connectors = FakeConnectors({agent: [("gh1", "github", False)]})

    async def fake_create_pr(token, owner, r, *, title, body, head, base, draft):
        return {"number": 42, "url": "https://github.com/acme/widgets/pull/42", "state": "open"}

    manager.delivery.create_pr = fake_create_pr

    await manager.delivery.accept(task.id, run.id)
    pushed = await manager.delivery.deliver_op(task.id, run.id, kind="push")
    assert pushed.status == "delivered"

    # `push_branch` against a bare local remote leaves a non-GitHub
    # `remote_url`; force it GitHub-shaped the way the PR op requires (same
    # as the other PR tests in this file — no real network involved).
    await repo.conn.execute(
        "UPDATE task_deliveries SET remote_url='https://github.com/acme/widgets.git' WHERE id=?",
        (pushed.id,),
    )
    await repo.conn.commit()

    opened = await manager.delivery.deliver_op(task.id, run.id, kind="pull_request")
    assert opened.status == "delivered" and opened.pr_number == 42

    ops = await repo.list_delivery_ops(opened.id)
    push_op = next(o for o in ops if o.kind == "push" and o.state == "succeeded")
    pr_op = next(o for o in ops if o.kind == "pull_request" and o.state == "succeeded")
    assert push_op.id != pr_op.id

    push_key = f"task:{task.id}:run:{run.id}:delivery:terminal:{push_op.id}"
    pr_key = f"task:{task.id}:run:{run.id}:delivery:terminal:{pr_op.id}"
    push_injection = await db.get_session_injection_by_source(push_key)
    pr_injection = await db.get_session_injection_by_source(pr_key)
    assert push_injection is not None and pr_injection is not None
    assert push_injection["id"] != pr_injection["id"]
    assert "PR #42" not in push_injection["prompt"]
    assert "PR #42" in pr_injection["prompt"]


@pytest.mark.asyncio
async def test_notify_terminal_exception_does_not_fail_the_delivery_op(store):
    """Bypass isolation: a delivery op that already settled durably must
    return normally even if the terminal-notification layer raises for any
    reason at all — the notification is best-effort, the op is not."""
    db, repo, tmp, agent = store
    board, task, run, src, wt = await _ready_delivery(db, repo, tmp, agent)
    bare = tmp / "remote.git"
    _git(tmp, "init", "-q", "--bare", str(bare))
    _git(src, "remote", "add", "origin", str(bare))

    async def boom(task, delivery, settle_op_id):
        raise ValueError("simulated idempotency-key collision")

    coord = DeliveryCoordinator()
    coord.bind(db=db, connectors=FakeConnectors({}), notify_terminal=boom, repo=repo)
    await coord.accept(task.id, run.id)
    result = await coord.deliver_op(task.id, run.id, kind="push")
    assert result.status == "delivered"


@pytest.mark.asyncio
async def test_pull_request_422_already_exists_reconciles_instead_of_failing(store):
    db, repo, tmp, agent = store
    board, task, run, src, wt = await _ready_delivery(db, repo, tmp, agent)
    connectors = FakeConnectors({agent: [("gh1", "github", False)]})
    coord = _coord(db, repo, connectors)
    await coord.accept(task.id, run.id)
    await repo.conn.execute(
        "UPDATE task_deliveries SET status='delivered', pushed_ref='refs/heads/x', "
        "remote_url='https://github.com/acme/widgets.git' WHERE run_id=?",
        (run.id,),
    )
    await repo.conn.commit()

    async def fake_create_pr_422(token, owner, r, *, title, body, head, base, draft):
        raise delivery_module.PullRequestAlreadyExistsError(
            "GitHub PR creation failed (422): "
            '{"message":"Validation Failed","errors":[{"message":'
            '"A pull request already exists for acme:owlery/task-delivery-run."}]}'
        )

    async def fake_find(token, owner, r, *, head_owner, branch):
        return {"number": 99, "url": "https://github.com/acme/widgets/pull/99", "state": "open"}

    coord.create_pr = fake_create_pr_422
    coord.find_pr = fake_find
    d = await coord.deliver_op(task.id, run.id, kind="pull_request")
    assert d.status == "delivered"
    assert d.pr_number == 99 and d.pr_url == "https://github.com/acme/widgets/pull/99"
    ops = await repo.list_delivery_ops(d.id)
    pr_op = next(o for o in ops if o.kind == "pull_request")
    assert pr_op.state == "succeeded"


@pytest.mark.asyncio
async def test_pull_request_422_already_exists_without_a_findable_pr_blocks(store):
    """If GitHub insists a PR already exists but the read-only reconcile can't
    find one, this must settle a normal `blocked(op_failed)` — never crash,
    never silently drop the op."""
    db, repo, tmp, agent = store
    board, task, run, src, wt = await _ready_delivery(db, repo, tmp, agent)
    connectors = FakeConnectors({agent: [("gh1", "github", False)]})
    coord = _coord(db, repo, connectors)
    await coord.accept(task.id, run.id)
    await repo.conn.execute(
        "UPDATE task_deliveries SET status='delivered', pushed_ref='refs/heads/x', "
        "remote_url='https://github.com/acme/widgets.git' WHERE run_id=?",
        (run.id,),
    )
    await repo.conn.commit()

    async def fake_create_pr_422(token, owner, r, *, title, body, head, base, draft):
        raise delivery_module.PullRequestAlreadyExistsError("GitHub PR creation failed (422): already exists")

    async def fake_find_none(token, owner, r, *, head_owner, branch):
        return None

    coord.create_pr = fake_create_pr_422
    coord.find_pr = fake_find_none
    d = await coord.deliver_op(task.id, run.id, kind="pull_request")
    assert d.status == "blocked" and d.reason_kind == "op_failed"
    assert d.pr_number is None


@pytest.mark.asyncio
async def test_pull_request_idempotency_guard_skips_repost_when_pr_number_already_set(store):
    """§3's first half: a delivery with `pr_number` already recorded must
    never re-POST, even if its `status` was left corrupted (e.g. `blocked`)
    by an unrelated failure on top of that success — the exact corruption
    shape the §4 boot self-heal repairs."""
    db, repo, tmp, agent = store
    board, task, run, src, wt = await _ready_delivery(db, repo, tmp, agent)
    connectors = FakeConnectors({agent: [("gh1", "github", False)]})
    coord = _coord(db, repo, connectors)
    await coord.accept(task.id, run.id)
    await repo.conn.execute(
        "UPDATE task_deliveries SET status='blocked', pushed_ref='refs/heads/x', "
        "remote_url='https://github.com/acme/widgets.git', pr_number=7, "
        "pr_url='https://github.com/acme/widgets/pull/7', pr_state='open', "
        "reason_kind='op_failed', reason_detail='stale' WHERE run_id=?",
        (run.id,),
    )
    await repo.conn.commit()

    called = {"n": 0}

    async def fail_if_called(*a, **k):
        called["n"] += 1
        raise AssertionError("must never re-POST when pr_number is already set")

    coord.create_pr = fail_if_called
    result = await coord.deliver_op(task.id, run.id, kind="pull_request")
    assert called["n"] == 0
    assert result.pr_number == 7
    # The guard only refuses the re-POST; correcting the stale `blocked`
    # status itself is the boot self-heal's job (§4), not this call's.
    assert result.status == "blocked"


@pytest.mark.asyncio
async def test_pull_request_pr_number_guard_skips_the_pushed_ref_precondition_too(store):
    """§3/should-2: `pr_number` already set means a PURE read of the existing
    result — it must short-circuit BEFORE `_op_pr` even checks its normal
    preconditions (a recorded push, a GitHub remote, a resolvable connector),
    not just before the create-PR call. A row missing `pushed_ref`/
    `remote_url` entirely (never possible for a delivery that genuinely went
    through push) still must not raise, proving the guard is a top-of-function
    short-circuit and not merely "skip the network call"."""
    db, repo, tmp, agent = store
    board, task, run, src, wt = await _ready_delivery(db, repo, tmp, agent)
    coord = _coord(db, repo, FakeConnectors({}))  # no connector at all
    await coord.accept(task.id, run.id)
    await repo.conn.execute(
        "UPDATE task_deliveries SET status='blocked', pr_number=7, "
        "pr_url='https://github.com/acme/widgets/pull/7', pr_state='open', "
        "reason_kind='op_failed', reason_detail='stale' WHERE run_id=?",
        (run.id,),
    )
    await repo.conn.commit()
    delivery = await repo.get_delivery_by_run(run.id)
    assert delivery.pushed_ref is None and delivery.remote_url is None

    result = await coord.deliver_op(task.id, run.id, kind="pull_request")
    assert result.pr_number == 7 and result.status == "blocked"


def _github_create_pr_fake_client(status_code: int, text: str):
    class _FakeResponse:
        def __init__(self):
            self.status_code = status_code
            self.text = text

        def json(self):
            return {}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, *, headers=None, json=None):
            return _FakeResponse()

    return _FakeClient


@pytest.mark.asyncio
async def test_github_create_pr_classifies_422_already_exists(monkeypatch):
    body = (
        '{"message":"Validation Failed","errors":[{"resource":"PullRequest",'
        '"message":"A pull request already exists for acme:widgets-branch."}]}'
    )
    monkeypatch.setattr(
        delivery_module.httpx, "AsyncClient", _github_create_pr_fake_client(422, body)
    )
    with pytest.raises(delivery_module.PullRequestAlreadyExistsError):
        await delivery_module._github_create_pr(
            "tok", "acme", "widgets", title="t", body="b", head="h", base="main", draft=False,
        )


@pytest.mark.asyncio
async def test_github_create_pr_other_422_stays_a_generic_failure(monkeypatch):
    body = '{"message":"Validation Failed","errors":[{"message":"No commits between main and h."}]}'
    monkeypatch.setattr(
        delivery_module.httpx, "AsyncClient", _github_create_pr_fake_client(422, body)
    )
    with pytest.raises(delivery_module.ws.WorkspaceError) as exc_info:
        await delivery_module._github_create_pr(
            "tok", "acme", "widgets", title="t", body="b", head="h", base="main", draft=False,
        )
    assert not isinstance(exc_info.value, delivery_module.PullRequestAlreadyExistsError)


@pytest.mark.asyncio
async def test_reconcile_blocked_deliveries_with_pr_self_heals(store):
    """§4: a delivery stuck `blocked` despite already carrying a `pr_number`
    is definitionally wrong — self-heal it to `delivered` unconditionally and
    idempotently. A genuinely blocked delivery with no PR is left untouched."""
    db, repo, tmp, agent = store
    board, task, run, src, wt = await _ready_delivery(db, repo, tmp / "d1", agent)
    coord = _coord(db, repo, FakeConnectors({}))
    await coord.accept(task.id, run.id)
    corrupted = await repo.get_delivery_by_run(run.id)
    await repo.conn.execute(
        "UPDATE task_deliveries SET status='blocked', pushed_ref='refs/heads/x', "
        "pr_number=9, pr_url='https://github.com/acme/widgets/pull/9', pr_state='open', "
        "reason_kind='op_failed', "
        "reason_detail='GitHub PR creation failed (422): already exists' WHERE id=?",
        (corrupted.id,),
    )
    await repo.conn.commit()

    board2, task2, run2, src2, wt2 = await _ready_delivery(db, repo, tmp / "d2", agent)
    coord2 = _coord(db, repo, FakeConnectors({}))
    await coord2.accept(task2.id, run2.id)
    genuinely_blocked = await repo.get_delivery_by_run(run2.id)
    await repo.conn.execute(
        "UPDATE task_deliveries SET status='blocked', reason_kind='no_connector', "
        "reason_detail='the run''s Agent has no live GitHub connector' WHERE id=?",
        (genuinely_blocked.id,),
    )
    await repo.conn.commit()

    fixed = await repo.reconcile_blocked_deliveries_with_pr()
    assert [f.id for f in fixed] == [corrupted.id]
    reconciled = await repo.get_delivery(corrupted.id)
    assert reconciled.status == "delivered"
    assert reconciled.reason_kind is None and reconciled.reason_detail is None
    assert reconciled.pr_number == 9

    still_blocked = await repo.get_delivery(genuinely_blocked.id)
    assert still_blocked.status == "blocked" and still_blocked.reason_kind == "no_connector"

    # Idempotent: nothing left to fix on a second pass.
    assert await repo.reconcile_blocked_deliveries_with_pr() == []
