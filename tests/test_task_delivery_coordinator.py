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
from server.task_board import delivery as delivery_module
from server.task_board.delivery import DeliveryCoordinator
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


async def _ready_delivery(db, repo, tmp, agent, *, worker_commits=True):
    src = tmp / "src"
    _init_repo(src)
    board = await repo.create_board(
        name=f"D-{uuid.uuid4().hex[:8]}",
        working_dir=str(src),
        default_workspace_mode="git_worktree",
    )
    task = await repo.create_task(
        board_id=board.id, title="Ship it", status="todo", assignee_agent_id=agent
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

    async def fake_create_pr(token, owner, r, *, title, body, head, base, draft):
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
    reconciled = await coord.reconcile_interrupted_pr(await repo.get_delivery(d.id))
    assert reconciled.status == "delivered" and reconciled.pr_number == 7
    assert created["count"] == 0


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
