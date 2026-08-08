"""Release-line deploy backend pipeline (docs/plans/release-line-deploy.md
§3, T-A).

Board-level equivalents of `deploy_stage`/`deploy_switch`/`deploy_rollback`:
the source is the board's protected release branch, resolved at its
configured remote, instead of a per-run delivery's attempt branch. Every
mechanism reused from local-deploy.md — `stage_slot`, the snapshot/journal/
switcher handoff, quiesce, probation — is exercised through the SAME fakes as
`test_task_deploy_stage.py`/`test_task_deploy_switch.py`; only the release
entry points and the release_deployments/release_deployment_ops tables are
new here.

Covers: release repository ops (version sequencing, one-active lock, op CAS),
the release coordinator (plan+stage happy path, unresolvable ref, lock held,
stage failure settles release+lock, switch refusals and handoff, rollback
incl. a pre-release-era target), boot reconciliation of release ops (every
§8 journal-tail row), census counts a running release op. No real-model
quota anywhere.
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

import server.switcher as switcher
from server.config import settings
from server.database import Database
from server.deploy import DeployLayout
from server.deploy_admission import DeployAdmissionGate
from server.task_board.delivery import DeliveryCoordinator
from server.task_board.deploy_quiesce import BusySource, DeployQuiesce
from server.task_board.manager import DeployProbation, TaskBoardManager
from server.task_board.models import (
    DeliveryConfirmationRequired,
    DeployLockedError,
    TaskConflictError,
)
from server.task_board.repository import TaskRepository


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


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


def _init_repo(path: Path, *, branch: str = "main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "a@b.c")
    _git(path, "config", "user.name", "T")
    (path / "f.txt").write_text("base\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    _git(path, "branch", "-M", branch)


def _init_layout(root: Path, monkeypatch, *, seed_idle: bool = True) -> DeployLayout:
    layout = DeployLayout.at(root)
    root.mkdir(parents=True, exist_ok=True)
    for slot in ("a", "b") if seed_idle else ("a",):
        s = layout.slot_path(slot)
        (s / ".git").mkdir(parents=True)
        (s / "web").mkdir()
        bindir = s / ".venv" / "bin"
        bindir.mkdir(parents=True)
        for exe in ("python", "pip", "owlery"):
            (bindir / exe).touch()
    layout.snapshots_path.mkdir(exist_ok=True)
    layout.journal_path.touch()
    layout.switch_current("a")
    monkeypatch.setattr(settings, "deploy_root", str(root))
    monkeypatch.setattr(settings, "debug", False)
    return layout


class FakeStageRunner:
    def __call__(self, argv, cwd, timeout):
        if argv[:2] == ["git", "clone"]:
            dst = Path(argv[3])
            (dst / ".git").mkdir(parents=True, exist_ok=True)
            (dst / "web").mkdir(exist_ok=True)
        elif argv[1:3] == ["-m", "venv"]:
            bindir = cwd / ".venv" / "bin"
            bindir.mkdir(parents=True, exist_ok=True)
            for exe in ("python", "pip", "owlery"):
                (bindir / exe).touch()
        return 0, "ok"


class FailingStageRunner(FakeStageRunner):
    def __init__(self, *, fail_step: str, fail_output: str = "boom"):
        self.fail_step = fail_step
        self.fail_output = fail_output

    def __call__(self, argv, cwd, timeout):
        if argv[:2] == ["bun", "run"] and self.fail_step == "build":
            return 1, self.fail_output
        if argv[1:2] == ["-c"] and self.fail_step == "import_probe":
            return 1, self.fail_output
        return super().__call__(argv, cwd, timeout)


def _coord(db, repo) -> DeliveryCoordinator:
    c = DeliveryCoordinator()
    c.bind(db=db, connectors=None, notify_terminal=None, repo=repo)
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


class FakeQuiesce:
    def __init__(self, busy_sequence):
        self._seq = [list(x) for x in busy_sequence]
        self.paused = 0
        self.resumed = 0
        self.admission_closes = 0
        self.admission_opens = 0

    async def census(self, *, exclude_op_id=None):
        if len(self._seq) > 1:
            return self._seq.pop(0)
        return list(self._seq[0]) if self._seq else []

    async def pause_for_drain(self):
        self.paused += 1

    async def resume_from_drain(self):
        self.resumed += 1

    async def close_admission(self):
        self.admission_closes += 1

    async def open_admission(self):
        self.admission_opens += 1


def _wire_switch(coord, quiesce):
    coord.deploy_quiesce = quiesce
    coord.broadcasts = []
    coord.shutdowns = []
    coord.spawns = []

    async def _broadcast(msg):
        coord.broadcasts.append(msg)

    def _spawn(**kw):
        coord.spawns.append(kw)
        return 4321

    coord.broadcast_restarting = _broadcast
    coord.request_shutdown = lambda: coord.shutdowns.append(True)
    coord.spawn_switcher = _spawn
    coord._drain_poll_interval = 0.01
    return coord


def _manager(db, repo) -> TaskBoardManager:
    m = TaskBoardManager()
    m.repo = repo
    m.db = db
    m.session_mgr = None
    m._probation_poll_interval = 0.01
    return m


async def _board_with_remote(repo, tmp, *, branch: str = "main") -> tuple[str, str, str]:
    """A board whose `working_dir` has a bare `origin` advertising `branch`'s
    tip — the input a release stage needs. Returns (board_id, src, sha)."""
    src = tmp / "src"
    _init_repo(src, branch=branch)
    bare = tmp / "origin.git"
    _git(tmp, "init", "-q", "--bare", str(bare))
    _git(src, "remote", "add", "origin", str(bare))
    _git(src, "push", "origin", branch)
    sha = subprocess.run(
        ["git", "rev-parse", branch], cwd=str(src), check=True, capture_output=True, text=True
    ).stdout.strip()
    board = await repo.create_board(
        name=f"R-{uuid.uuid4().hex[:8]}", working_dir=str(src),
        default_workspace_mode="git_worktree", allow_local_deploy=True,
        deploy_release_ref=branch,
    )
    return board.id, str(src), sha


def _append(layout, op_id, step, detail):
    switcher.Journal(str(layout.journal_path)).append(op_id, step, detail)


# ------------------------------------------------------- repository: plan


@pytest.mark.asyncio
async def test_plan_release_deployment_sequences_versions(store):
    _db, repo, _tmp, _agent = store
    board = await repo.create_board(
        name="B1", working_dir=str(Path.cwd()), default_workspace_mode="shared",
    )
    r1 = await repo.plan_release_deployment(
        board_id=board.id, source_ref="main", sha="a" * 40,
        source_repo="/repo", actor_kind="user", actor_agent_id=None,
    )
    r2 = await repo.plan_release_deployment(
        board_id=board.id, source_ref="main", sha="b" * 40,
        source_repo="/repo", actor_kind="user", actor_agent_id=None,
    )
    assert r1.version.endswith(".01") and r2.version.endswith(".02")
    # Re-planning supersedes the prior non-live (never-switched) candidate.
    assert (await repo.get_release_deployment(r1.id)).state == "superseded"
    assert (await repo.get_release_deployment(r2.id)).state == "planned"


@pytest.mark.asyncio
async def test_plan_release_deployment_never_supersedes_staging(store):
    """A concurrent re-plan for the same board must not clobber a release row
    whose stage pipeline is actively holding the release-level lock — doing
    so would orphan that lock forever."""
    _db, repo, _tmp, _agent = store
    board = await repo.create_board(
        name="B1", working_dir=str(Path.cwd()), default_workspace_mode="shared",
    )
    r1 = await repo.plan_release_deployment(
        board_id=board.id, source_ref="main", sha="a" * 40,
        source_repo="/repo", actor_kind="user", actor_agent_id=None,
    )
    await repo.begin_release_staging(r1.id, slot="b", sha="a" * 40, source_repo="/repo")
    assert (await repo.get_release_deployment(r1.id)).state == "staging"

    r2 = await repo.plan_release_deployment(
        board_id=board.id, source_ref="main", sha="b" * 40,
        source_repo="/repo", actor_kind="user", actor_agent_id=None,
    )
    # r1 is untouched — still staging, its lock intact.
    assert (await repo.get_release_deployment(r1.id)).state == "staging"
    assert r2.state == "planned"


@pytest.mark.asyncio
async def test_plan_release_deployment_never_supersedes_planned_with_running_op(store):
    """A release can be `planned` with a `running` stage op — the window
    between `release_stage`'s plan+start and its `begin_release_staging`
    call. A concurrent re-plan superseding it there would leave that running
    op permanently stuck (Snape review, blocker 1)."""
    _db, repo, _tmp, _agent = store
    board = await repo.create_board(
        name="B1", working_dir=str(Path.cwd()), default_workspace_mode="shared",
    )
    r1 = await repo.plan_release_deployment(
        board_id=board.id, source_ref="main", sha="a" * 40,
        source_repo="/repo", actor_kind="user", actor_agent_id=None,
    )
    op = await repo.plan_release_op(
        r1.id, kind="stage", request={}, actor_kind="user", actor_agent_id=None,
    )
    await repo.start_release_op(r1.id, op.id)
    assert (await repo.get_release_deployment(r1.id)).state == "planned"

    r2 = await repo.plan_release_deployment(
        board_id=board.id, source_ref="main", sha="b" * 40,
        source_repo="/repo", actor_kind="user", actor_agent_id=None,
    )
    # r1 must be untouched — its running op still has somewhere to land.
    assert (await repo.get_release_deployment(r1.id)).state == "planned"
    assert r2.state == "planned"
    # The in-flight stage can still complete normally.
    r1, dep = await repo.begin_release_staging(r1.id, slot="b", sha="a" * 40, source_repo="/repo")
    assert r1.state == "staging" and dep.state == "staging"


@pytest.mark.asyncio
async def test_release_stage_settles_op_when_release_lost_the_plan_cas(store, tmp_path, monkeypatch):
    """If `begin_release_staging` loses its CAS for any reason other than the
    global lock (e.g. the release row was superseded out from under it), the
    running stage op must still settle `failed` — never left `running`
    forever (Snape review, blocker 1's op-settlement half)."""
    db, repo, tmp, _agent = store
    board_id, _src, _sha = await _board_with_remote(repo, tmp)
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    coord = _coord(db, repo)

    real_begin = repo.begin_release_staging

    async def _begin_then_supersede(release_id, **kw):
        # Simulate a race: something else superseded the release row between
        # `release_stage`'s plan+start and its `begin_release_staging` call.
        async with repo._transaction() as conn:
            await conn.execute(
                "UPDATE release_deployments SET state='superseded' WHERE id=?",
                (release_id,),
            )
        return await real_begin(release_id, **kw)

    monkeypatch.setattr(repo, "begin_release_staging", _begin_then_supersede)

    release, op = await coord.release_stage(
        board_id, server_root=layout.slot_path("a"), stage_runner=FakeStageRunner()
    )
    assert op.state == "failed"
    assert release.state == "superseded"  # untouched by the best-effort fail_planned_release


@pytest.mark.asyncio
async def test_release_rollback_refuses_cross_board_live_release(store, tmp_path, monkeypatch):
    """The live deployment/release are instance-wide singletons, but
    `release_rollback(board_id)` is a board-scoped endpoint: board B must not
    be able to roll back board A's live release (Snape review, blocker 2)."""
    db, repo, tmp, _agent = store
    board_a_id, _src, _sha, layout, coord, release = await _staged_release(
        db, repo, tmp, tmp_path, monkeypatch
    )
    async with repo._transaction() as conn:
        await conn.execute(
            "INSERT INTO deployments (id, delivery_id, task_id, op_id, slot, sha, "
            "source_repo, release_id, state, journal, created_at, updated_at) VALUES "
            "('priorsup', NULL, NULL, NULL, 'a', 'oldsha', 'x', NULL, 'superseded', NULL, "
            "'2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')"
        )
    _wire_switch(coord, FakeQuiesce([[]]))
    await coord.release_switch(board_a_id, server_root=layout.slot_path("a"))
    _append(layout, (await _running_switch_op(repo, release.id)), switcher.STEP_SWITCHED_OK,
            {"slot": "b", "sha": release.sha})
    m = _manager(db, repo)
    await m.reconcile_release_switch_ops()
    layout.switch_current("b")
    assert (await repo.get_release_deployment(release.id)).board_id == board_a_id

    # A second, unrelated board with its own local-deploy opt-in.
    board_b = await repo.create_board(
        name="B-other", working_dir=str(tmp / "src"),
        default_workspace_mode="git_worktree", allow_local_deploy=True,
    )
    coord2 = _coord(db, repo)
    _wire_switch(coord2, FakeQuiesce([[]]))

    with pytest.raises(TaskConflictError, match="different board"):
        await coord2.release_rollback(
            board_b.id, confirm_rollback=True, server_root=layout.slot_path("b")
        )


@pytest.mark.asyncio
async def test_release_rollback_refuses_cross_board_target(store, tmp_path, monkeypatch):
    """`get_rollback_target` is slot-level and board-blind (only two slots
    exist total), so "newest superseded on the other slot" can belong to a
    DIFFERENT board's release line. Exact repro from Snape's review:
    board A live on slot a -> board B switches live to slot b (supersedes
    A's slot-a row) -> board A switches live back to slot a (supersedes B's
    slot-b row) -> board A's rollback target is now board B's release, which
    must be refused rather than silently promoted to live."""
    db, repo, tmp, _agent = store
    board_a_id, _src_a, _sha_a, layout, coord_a, release_a = await _staged_release(
        db, repo, tmp, tmp_path, monkeypatch
    )
    m = _manager(db, repo)

    # A: plan+stage+switch live onto slot b (idle_slot() picks b first).
    _wire_switch(coord_a, FakeQuiesce([[]]))
    await coord_a.release_switch(board_a_id, server_root=layout.slot_path("a"))
    _append(layout, (await _running_switch_op(repo, release_a.id)), switcher.STEP_SWITCHED_OK,
            {"slot": "b", "sha": release_a.sha})
    await m.reconcile_release_switch_ops()
    layout.switch_current("b")
    assert (await repo.get_release_deployment(release_a.id)).state == "live"

    # B: a second, independent board+repo, staged onto the now-idle slot a,
    # then switched live — supersedes A's release on slot b... no, A is on
    # slot b so B stages onto slot a and switching to it supersedes A.
    src_b = tmp / "src_b"
    _init_repo(src_b, branch="main")
    bare_b = tmp / "origin_b.git"
    _git(tmp, "init", "-q", "--bare", str(bare_b))
    _git(src_b, "remote", "add", "origin", str(bare_b))
    _git(src_b, "push", "origin", "main")
    board_b = await repo.create_board(
        name="B-other", working_dir=str(src_b), default_workspace_mode="git_worktree",
        allow_local_deploy=True, deploy_release_ref="main",
    )
    coord_b = _coord(db, repo)
    _wire_switch(coord_b, FakeQuiesce([[]]))
    release_b, _op_b = await coord_b.release_stage(
        board_b.id, server_root=layout.slot_path("b"), stage_runner=FakeStageRunner()
    )
    assert release_b.state == "staged"
    await coord_b.release_switch(board_b.id, server_root=layout.slot_path("b"))
    _append(layout, (await _running_switch_op(repo, release_b.id)), switcher.STEP_SWITCHED_OK,
            {"slot": "a", "sha": release_b.sha})
    await m.reconcile_release_switch_ops()
    layout.switch_current("a")
    assert (await repo.get_release_deployment(release_b.id)).state == "live"
    assert (await repo.get_release_deployment(release_a.id)).state == "superseded"

    # A: switch live back to slot b — this supersedes B's slot-a release.
    src_a = Path((await repo.get_board(board_a_id)).working_dir)
    _git(src_a, "commit", "--allow-empty", "-qm", "release a2")
    _git(src_a, "push", "origin", "main")
    coord_a2 = _coord(db, repo)
    _wire_switch(coord_a2, FakeQuiesce([[]]))
    release_a2, _op_a2 = await coord_a2.release_stage(
        board_a_id, server_root=layout.slot_path("a"), stage_runner=FakeStageRunner()
    )
    assert release_a2.state == "staged"
    await coord_a2.release_switch(board_a_id, server_root=layout.slot_path("a"))
    _append(layout, (await _running_switch_op(repo, release_a2.id)), switcher.STEP_SWITCHED_OK,
            {"slot": "b", "sha": release_a2.sha})
    await m.reconcile_release_switch_ops()
    layout.switch_current("b")
    assert (await repo.get_release_deployment(release_a2.id)).state == "live"
    assert (await repo.get_release_deployment(release_b.id)).state == "superseded"

    # Board A's rollback target is now board B's release (the only superseded
    # slot). This must be refused, not promoted to live.
    coord_a3 = _coord(db, repo)
    _wire_switch(coord_a3, FakeQuiesce([[]]))
    with pytest.raises(TaskConflictError, match="different board"):
        await coord_a3.release_rollback(
            board_a_id, confirm_rollback=True, server_root=layout.slot_path("b")
        )
    # Nothing moved: B's release is still live, no rollback op was planned.
    assert (await repo.get_release_deployment(release_b.id)).state == "superseded"
    assert (await repo.get_release_deployment(release_a2.id)).state == "live"
    assert [
        o for o in await repo.list_running_release_ops()
    ] == []


@pytest.mark.asyncio
async def test_release_op_cas_lifecycle(store):
    _db, repo, _tmp, _agent = store
    board = await repo.create_board(
        name="B1", working_dir=str(Path.cwd()), default_workspace_mode="shared",
    )
    release = await repo.plan_release_deployment(
        board_id=board.id, source_ref="main", sha="a" * 40,
        source_repo="/repo", actor_kind="user", actor_agent_id=None,
    )
    op = await repo.plan_release_op(
        release.id, kind="stage", request={}, actor_kind="user", actor_agent_id=None,
    )
    assert op.state == "planned"
    started = await repo.start_release_op(release.id, op.id)
    assert started.state == "running"
    with pytest.raises(TaskConflictError):
        await repo.start_release_op(release.id, op.id)  # already running

    # One-running-per-release index: a second op cannot start concurrently.
    op2 = await repo.plan_release_op(
        release.id, kind="switch", request={}, actor_kind="user", actor_agent_id=None,
    )
    with pytest.raises(TaskConflictError):
        await repo.start_release_op(release.id, op2.id)

    finished = await repo.finish_release_op(
        release.id, op.id, state="succeeded", result={"ok": True},
    )
    assert finished.state == "succeeded" and finished.result == {"ok": True}
    with pytest.raises(TaskConflictError):
        await repo.finish_release_op(release.id, op.id, state="succeeded")  # not running


@pytest.mark.asyncio
async def test_release_stage_lock_is_atomic_with_deployment_lock(store):
    """`begin_release_staging` takes both the release-level and slot-level
    global deploy locks in ONE transaction — a partial acquisition must never
    be observable, whichever lock a concurrent holder contends."""
    _db, repo, _tmp, _agent = store
    board = await repo.create_board(
        name="B1", working_dir=str(Path.cwd()), default_workspace_mode="shared",
    )
    release = await repo.plan_release_deployment(
        board_id=board.id, source_ref="main", sha="a" * 40,
        source_repo="/repo", actor_kind="user", actor_agent_id=None,
    )
    # Slot-level lock already held by an unrelated (pre-release-era) stage.
    await repo.begin_deployment_staging(
        delivery_id=None, task_id="t-other", op_id=None,
        slot="b", sha="deadbeef" * 5, source_repo="/repo",
    )
    with pytest.raises(DeployLockedError):
        await repo.begin_release_staging(release.id, slot="b", sha="a" * 40, source_repo="/repo")
    # The release row must NOT have moved to `staging` — the failed slot-level
    # half of the transaction rolled the whole thing back.
    assert (await repo.get_release_deployment(release.id)).state == "planned"


# ------------------------------------------------------- coordinator: stage


@pytest.mark.asyncio
async def test_release_stage_happy_path(store, tmp_path, monkeypatch):
    db, repo, tmp, _agent = store
    board_id, src, sha = await _board_with_remote(repo, tmp)
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    coord = _coord(db, repo)
    runner = FakeStageRunner()

    release, op = await coord.release_stage(
        board_id, server_root=layout.slot_path("a"), stage_runner=runner
    )

    assert release.state == "staged" and release.sha == sha
    assert release.source_ref == "main" and release.version.endswith(".01")
    assert op.state == "succeeded" and op.kind == "stage"
    assert op.result["staged_slot"] == "b" and op.result["staged_sha"] == sha

    dep = await repo.get_deployment(release.deployment_id)
    assert dep.state == "staged" and dep.slot == "b" and dep.sha == sha
    assert dep.release_id == release.id
    assert dep.delivery_id is None and dep.task_id is None
    assert await repo.get_active_deployment() is None


@pytest.mark.asyncio
async def test_release_stage_refuses_unresolvable_ref_before_planning(store, tmp_path, monkeypatch):
    db, repo, tmp, _agent = store
    src = tmp / "src"
    _init_repo(src)
    bare = tmp / "origin.git"
    _git(tmp, "init", "-q", "--bare", str(bare))
    _git(src, "remote", "add", "origin", str(bare))
    # No push: `main` never reaches the remote — deploy_release_ref won't resolve.
    board = await repo.create_board(
        name="B1", working_dir=str(src), default_workspace_mode="git_worktree",
        allow_local_deploy=True, deploy_release_ref="main",
    )
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    coord = _coord(db, repo)

    with pytest.raises(TaskConflictError, match="release ref could not be resolved"):
        await coord.release_stage(
            board.id, server_root=layout.slot_path("a"), stage_runner=FakeStageRunner()
        )
    assert await repo.list_release_deployments(board.id) == []


@pytest.mark.asyncio
async def test_release_stage_locked_settles_failed(store, tmp_path, monkeypatch):
    db, repo, tmp, _agent = store
    board_id, _src, _sha = await _board_with_remote(repo, tmp)
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    coord = _coord(db, repo)
    holder = await repo.begin_deployment_staging(
        delivery_id=None, task_id="t-other", op_id=None,
        slot="b", sha="deadbeef" * 5, source_repo="/repo",
    )

    release, op = await coord.release_stage(
        board_id, server_root=layout.slot_path("a"), stage_runner=FakeStageRunner()
    )
    assert release.state == "failed" and op.state == "failed"
    assert holder.sha[:12] in (op.error or "")
    assert (await repo.get_active_deployment()).id == holder.id


@pytest.mark.asyncio
async def test_release_stage_failure_settles_release_and_deployment(store, tmp_path, monkeypatch):
    db, repo, tmp, _agent = store
    board_id, _src, _sha = await _board_with_remote(repo, tmp)
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    coord = _coord(db, repo)
    runner = FailingStageRunner(fail_step="build", fail_output="TS2304")

    release, op = await coord.release_stage(
        board_id, server_root=layout.slot_path("a"), stage_runner=runner
    )
    assert release.state == "failed" and "TS2304" in (release.error or "")
    assert op.state == "failed" and "TS2304" in (op.error or "")
    dep = await repo.get_deployment(release.deployment_id)
    assert dep.state == "failed"
    assert await repo.get_active_deployment() is None  # lock released


@pytest.mark.asyncio
async def test_release_stage_refuses_without_opt_in(store, tmp_path, monkeypatch):
    db, repo, tmp, _agent = store
    board_id, _src, _sha = await _board_with_remote(repo, tmp)
    await repo.update_board(board_id, allow_local_deploy=False)
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    coord = _coord(db, repo)

    with pytest.raises(TaskConflictError, match="allow_local_deploy"):
        await coord.release_stage(
            board_id, server_root=layout.slot_path("a"), stage_runner=FakeStageRunner()
        )
    assert await repo.list_release_deployments(board_id) == []


@pytest.mark.asyncio
async def test_release_stage_closed_admission_refuses_before_planning(store, tmp_path, monkeypatch):
    db, repo, tmp, _agent = store
    board_id, _src, _sha = await _board_with_remote(repo, tmp)
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    coord = _coord(db, repo)
    gate = DeployAdmissionGate()
    _bind_admission(coord, gate)
    await gate.close()

    with pytest.raises(TaskConflictError, match="deploy admission is closed"):
        await coord.release_stage(
            board_id, server_root=layout.slot_path("a"), stage_runner=FakeStageRunner()
        )
    assert await repo.list_release_deployments(board_id) == []


# ------------------------------------------------------ coordinator: switch


async def _staged_release(db, repo, tmp, tmp_path, monkeypatch):
    board_id, src, sha = await _board_with_remote(repo, tmp)
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    coord = _coord(db, repo)
    release, _op = await coord.release_stage(
        board_id, server_root=layout.slot_path("a"), stage_runner=FakeStageRunner()
    )
    assert release.state == "staged"
    return board_id, src, sha, layout, coord, release


@pytest.mark.asyncio
async def test_release_switch_happy_path(store, tmp_path, monkeypatch):
    db, repo, tmp, _agent = store
    board_id, _src, sha, layout, coord, release = await _staged_release(
        db, repo, tmp, tmp_path, monkeypatch
    )
    _wire_switch(coord, FakeQuiesce([[]]))

    result_release, op = await coord.release_switch(board_id, server_root=layout.slot_path("a"))

    assert op.state == "running" and op.kind == "switch"
    assert op.journal_ref == str(layout.journal_path)
    dep = await repo.get_deployment(release.deployment_id)
    assert dep.state == "switching" and dep.op_id == op.id
    assert result_release.id == release.id

    entries = switcher.Journal(str(layout.journal_path)).entries(op.id)
    handoff = next(e for e in entries if e["step"] == switcher.STEP_HANDOFF)
    assert handoff["detail"]["to_slot"] == "b" and handoff["detail"]["new_sha"] == sha
    assert coord.spawns[0]["from_slot"] == "a" and coord.spawns[0]["op_id"] == op.id
    assert coord.shutdowns == [True]


@pytest.mark.asyncio
async def test_release_switch_refuses_not_idle_with_census(store, tmp_path, monkeypatch):
    db, repo, tmp, _agent = store
    board_id, _src, _sha, layout, coord, release = await _staged_release(
        db, repo, tmp, tmp_path, monkeypatch
    )
    _wire_switch(coord, FakeQuiesce([[BusySource("session_turn", "s1")]]))

    result_release, op = await coord.release_switch(board_id, server_root=layout.slot_path("a"))

    assert op.state == "failed"
    assert "session_turn:s1" in (op.error or "")
    assert (await repo.get_deployment(release.deployment_id)).state == "staged"
    assert coord.spawns == [] and coord.shutdowns == []


@pytest.mark.asyncio
async def test_release_switch_nothing_staged_refuses(store, tmp_path, monkeypatch):
    db, repo, tmp, _agent = store
    board_id, _src, _sha = await _board_with_remote(repo, tmp)
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    coord = _coord(db, repo)
    _wire_switch(coord, FakeQuiesce([[]]))

    with pytest.raises(TaskConflictError, match="nothing_staged"):
        await coord.release_switch(board_id, server_root=layout.slot_path("a"))


@pytest.mark.asyncio
async def test_release_switch_requires_bound_quiesce(store, tmp_path, monkeypatch):
    db, repo, tmp, _agent = store
    board_id, _src, _sha, layout, coord, _release = await _staged_release(
        db, repo, tmp, tmp_path, monkeypatch
    )
    with pytest.raises(TaskConflictError, match="unavailable on this instance"):
        await coord.release_switch(board_id, server_root=layout.slot_path("a"))


# --------------------------------------------- coordinator: rollback


@pytest.mark.asyncio
async def test_release_rollback_confirmation_required(store, tmp_path, monkeypatch):
    db, repo, tmp, _agent = store
    board_id, _src, _sha, layout, coord, release = await _staged_release(
        db, repo, tmp, tmp_path, monkeypatch
    )
    # Seed a superseded slot-a deployment directly so a rollback target exists
    # (mirrors test_task_deploy_switch.py's `priorlive` seeding pattern).
    async with repo._transaction() as conn:
        await conn.execute(
            "INSERT INTO deployments (id, delivery_id, task_id, op_id, slot, sha, "
            "source_repo, release_id, state, journal, created_at, updated_at) VALUES "
            "('priorsup', NULL, NULL, NULL, 'a', 'oldsha', 'x', NULL, 'superseded', NULL, "
            "'2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')"
        )
    _wire_switch(coord, FakeQuiesce([[]]))
    await coord.release_switch(board_id, server_root=layout.slot_path("a"))
    _append(layout, (await _running_switch_op(repo, release.id)), switcher.STEP_SWITCHED_OK,
            {"slot": "b", "sha": release.sha})
    m = _manager(db, repo)
    await m.reconcile_release_switch_ops()

    live = await repo.get_live_deployment()
    assert live is not None and live.state == "live"

    with pytest.raises(DeliveryConfirmationRequired) as exc:
        await coord.release_rollback(
            board_id, confirm_rollback=False, server_root=layout.slot_path("b")
        )
    assert exc.value.confirmation == "confirm_rollback"


async def _running_switch_op(repo, release_id: str) -> str:
    ops = await repo.list_running_release_switch_ops()
    return next(o.id for o in ops if o.release_id == release_id)


@pytest.mark.asyncio
async def test_release_rollback_switches_to_prior_slot(store, tmp_path, monkeypatch):
    db, repo, tmp, _agent = store
    board_id, _src, _sha, layout, coord, release = await _staged_release(
        db, repo, tmp, tmp_path, monkeypatch
    )
    _wire_switch(coord, FakeQuiesce([[]]))
    await coord.release_switch(board_id, server_root=layout.slot_path("a"))
    switch_op_id = await _running_switch_op(repo, release.id)
    _append(layout, switch_op_id, switcher.STEP_SWITCHED_OK, {"slot": "b", "sha": release.sha})
    m = _manager(db, repo)
    await m.reconcile_release_switch_ops()
    layout.switch_current("b")
    assert (await repo.get_release_deployment(release.id)).state == "live"

    # Stage + switch a SECOND release so there is a superseded slot to roll
    # back to (slot a still holds the pre-release checkout).
    layout.slot_path("a").mkdir(parents=True, exist_ok=True)  # already exists from init
    coord2 = _coord(db, repo)
    board = await repo.get_board(board_id)
    _git(Path(board.working_dir), "commit", "--allow-empty", "-qm", "release 2")
    _git(Path(board.working_dir), "push", "origin", "main")
    _wire_switch(coord2, FakeQuiesce([[]]))
    release2, _op2 = await coord2.release_stage(
        board_id, server_root=layout.slot_path("b"), stage_runner=FakeStageRunner()
    )
    assert release2.state == "staged" and release2.deployment_id
    await coord2.release_switch(board_id, server_root=layout.slot_path("b"))
    switch_op2_id = await _running_switch_op(repo, release2.id)
    _append(layout, switch_op2_id, switcher.STEP_SWITCHED_OK, {"slot": "a", "sha": release2.sha})
    await m.reconcile_release_switch_ops()
    layout.switch_current("a")

    assert (await repo.get_release_deployment(release2.id)).state == "live"
    assert (await repo.get_release_deployment(release.id)).state == "superseded"

    # Now roll back: target is release 1's now-superseded slot b deployment.
    coord3 = _coord(db, repo)
    _wire_switch(coord3, FakeQuiesce([[]]))
    result_release, rollback_op = await coord3.release_rollback(
        board_id, confirm_rollback=True, server_root=layout.slot_path("a")
    )
    assert rollback_op.kind == "rollback" and rollback_op.state == "running"
    # The rollback op is attached to the currently-live release (being abandoned).
    assert result_release.id == release2.id
    entries = switcher.Journal(str(layout.journal_path)).entries(rollback_op.id)
    tail = entries[-1]
    assert tail["detail"]["to_slot"] == "b" and tail["detail"]["new_sha"] == release.sha


@pytest.mark.asyncio
async def test_release_rollback_pre_release_era_target(store, tmp_path, monkeypatch):
    """A rollback target predating release-line-deploy (a `deployments` row with
    no `release_id`) must still work — provenance does not matter to a slot
    flip (release-line-deploy.md §3.1 point 3)."""
    db, repo, tmp, _agent = store
    board_id, _src, _sha, layout, coord, release = await _staged_release(
        db, repo, tmp, tmp_path, monkeypatch
    )
    # Seed a pre-release-era `live` deployment directly on slot a (no release_id).
    async with repo._transaction() as conn:
        await conn.execute(
            "INSERT INTO deployments (id, delivery_id, task_id, op_id, slot, sha, "
            "source_repo, release_id, state, journal, created_at, updated_at) VALUES "
            "('priorlive', NULL, NULL, NULL, 'a', 'oldsha', 'x', NULL, 'live', NULL, "
            "'2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')"
        )
    _wire_switch(coord, FakeQuiesce([[]]))
    await coord.release_switch(board_id, server_root=layout.slot_path("a"))
    switch_op_id = await _running_switch_op(repo, release.id)
    _append(layout, switch_op_id, switcher.STEP_SWITCHED_OK, {"slot": "b", "sha": release.sha})
    m = _manager(db, repo)
    await m.reconcile_release_switch_ops()
    layout.switch_current("b")
    assert (await repo.get_deployment("priorlive")).state == "superseded"

    coord2 = _coord(db, repo)
    _wire_switch(coord2, FakeQuiesce([[]]))
    result_release, rollback_op = await coord2.release_rollback(
        board_id, confirm_rollback=True, server_root=layout.slot_path("b")
    )
    assert rollback_op.kind == "rollback"
    entries = switcher.Journal(str(layout.journal_path)).entries(rollback_op.id)
    tail = entries[-1]
    assert tail["detail"]["to_slot"] == "a" and tail["detail"]["new_sha"] == "oldsha"

    # Settle the rollback: the target release row (release.id) should go to
    # `rolled_back`, and the pre-release-era deployment row becomes `live`.
    _append(layout, rollback_op.id, switcher.STEP_SWITCHED_OK, {"slot": "a", "sha": "oldsha"})
    await m.reconcile_release_switch_ops()
    assert (await repo.get_deployment("priorlive")).state == "live"
    assert (await repo.get_release_deployment(release.id)).state == "superseded"


# ---------------------------------------------- boot reconciliation (§8)


async def _handed_off_release(db, repo, tmp, tmp_path, monkeypatch):
    board_id, _src, sha, layout, coord, release = await _staged_release(
        db, repo, tmp, tmp_path, monkeypatch
    )
    _wire_switch(coord, FakeQuiesce([[]]))
    await coord.release_switch(board_id, server_root=layout.slot_path("a"))
    op_id = await _running_switch_op(repo, release.id)
    return coord, release, layout, board_id, op_id, sha


@pytest.mark.asyncio
async def test_reconcile_release_switched_ok(store, tmp_path, monkeypatch):
    db, repo, tmp, _agent = store
    _coord_, release, layout, _board_id, op_id, sha = await _handed_off_release(
        db, repo, tmp, tmp_path, monkeypatch
    )
    _append(layout, op_id, switcher.STEP_SWITCHED_OK, {"slot": "b", "sha": sha})
    m = _manager(db, repo)

    assert await m.reconcile_release_switch_ops() is None  # terminal, no probation

    assert (await repo.get_release_deployment(release.id)).state == "live"
    assert (await repo.get_deployment(release.deployment_id)).state == "live"


@pytest.mark.asyncio
async def test_reconcile_release_rolled_back(store, tmp_path, monkeypatch):
    db, repo, tmp, _agent = store
    _coord_, release, layout, _board_id, op_id, _sha = await _handed_off_release(
        db, repo, tmp, tmp_path, monkeypatch
    )
    _append(layout, op_id, switcher.STEP_ROLLED_BACK, {"reason": "health_timeout"})
    m = _manager(db, repo)

    await m.reconcile_release_switch_ops()

    assert (await repo.get_release_deployment(release.id)).state == "rolled_back"
    assert (await repo.get_deployment(release.deployment_id)).state == "rolled_back"


@pytest.mark.asyncio
async def test_reconcile_release_rollback_incomplete(store, tmp_path, monkeypatch):
    db, repo, tmp, _agent = store
    _coord_, release, layout, _board_id, op_id, _sha = await _handed_off_release(
        db, repo, tmp, tmp_path, monkeypatch
    )
    _append(layout, op_id, switcher.STEP_ROLLBACK_INCOMPLETE,
            {"stage": "port_not_freed", "reason": "health_timeout"})
    m = _manager(db, repo)

    await m.reconcile_release_switch_ops()

    assert (await repo.get_release_deployment(release.id)).state == "failed"
    assert (await repo.get_deployment(release.deployment_id)).state == "failed"


@pytest.mark.asyncio
async def test_reconcile_release_old_wont_die(store, tmp_path, monkeypatch):
    db, repo, tmp, _agent = store
    _coord_, release, layout, _board_id, op_id, _sha = await _handed_off_release(
        db, repo, tmp, tmp_path, monkeypatch
    )
    _append(layout, op_id, switcher.STEP_OLD_WONT_DIE, {"reason": "old_wont_die"})
    m = _manager(db, repo)

    await m.reconcile_release_switch_ops()

    # The flip never happened: release/deployment revert to `staged`, still
    # deployable, lock released.
    assert (await repo.get_release_deployment(release.id)).state == "staged"
    assert (await repo.get_deployment(release.deployment_id)).state == "staged"


@pytest.mark.asyncio
async def test_reconcile_release_interrupted_stale_handoff(store, tmp_path, monkeypatch):
    db, repo, tmp, _agent = store
    _coord_, release, layout, _board_id, op_id, _sha = await _handed_off_release(
        db, repo, tmp, tmp_path, monkeypatch
    )
    # No further journal entry beyond `handoff` — the switcher never ran.
    m = _manager(db, repo)

    await m.reconcile_release_switch_ops()

    assert (await repo.get_release_deployment(release.id)).state == "failed"
    assert (await repo.get_deployment(release.deployment_id)).state == "failed"


@pytest.mark.asyncio
async def test_reconcile_release_probation_then_resolves(store, tmp_path, monkeypatch):
    db, repo, tmp, _agent = store
    _coord_, release, layout, _board_id, op_id, sha = await _handed_off_release(
        db, repo, tmp, tmp_path, monkeypatch
    )
    _append(layout, op_id, switcher.STEP_FLIP_DONE, {"to_slot": "b", "new_sha": sha})
    m = _manager(db, repo)

    probation = await m.reconcile_release_switch_ops()
    assert probation is not None and probation.kind == "release" and probation.op_id == op_id

    _append(layout, op_id, switcher.STEP_SWITCHED_OK, {"slot": "b", "sha": sha})
    released = []
    outcome = await m.run_deploy_probation(probation, on_release=lambda: released.append(1) or _noop())
    assert outcome == switcher.STEP_SWITCHED_OK
    assert (await repo.get_release_deployment(release.id)).state == "live"


async def _noop():
    return None


@pytest.mark.asyncio
async def test_reconcile_release_no_deploy_root_interrupts(store, tmp_path, monkeypatch):
    db, repo, tmp, _agent = store
    _coord_, release, _layout, _board_id, _op_id, _sha = await _handed_off_release(
        db, repo, tmp, tmp_path, monkeypatch
    )
    monkeypatch.setattr(settings, "deploy_root", "")
    m = _manager(db, repo)

    assert await m.reconcile_release_switch_ops() is None
    assert (await repo.get_release_deployment(release.id)).state == "failed"


@pytest.mark.asyncio
async def test_orphan_staging_release_recovered_at_boot(store):
    _db, repo, _tmp, _agent = store
    board = await repo.create_board(
        name="B1", working_dir=str(Path.cwd()), default_workspace_mode="shared",
    )
    release = await repo.plan_release_deployment(
        board_id=board.id, source_ref="main", sha="a" * 40,
        source_repo="/repo", actor_kind="user", actor_agent_id=None,
    )
    release, dep = await repo.begin_release_staging(
        release.id, slot="b", sha="a" * 40, source_repo="/repo",
    )
    assert release.state == "staging" and dep.state == "staging"

    failed = await repo.fail_orphan_staging_releases(reason="server restarted")
    assert [r.id for r in failed] == [release.id]
    assert (await repo.get_release_deployment(release.id)).state == "failed"
    assert (await repo.get_deployment(dep.id)).state == "failed"
    assert await repo.get_active_deployment() is None  # slot-level lock released


# --------------------------------------------------------------- census


@pytest.mark.asyncio
async def test_census_counts_running_release_op():
    from server.task_board.deploy_quiesce import DeployQuiesce

    class _SM:
        sessions = {}

    class _DB:
        async def list_parked_turns(self):
            return []

        async def list_running_delegation_runs(self):
            return []

        async def list_pending_session_injections(self):
            return []

    class _Repo:
        async def list_running_runs(self):
            return []

        async def list_running_delivery_ops(self):
            return []

        async def list_running_release_ops(self):
            return [type("Op", (), {"id": "op1", "kind": "switch"})()]

    q = DeployQuiesce(session_manager=_SM(), repo=_Repo(), db=_DB())
    busy = await q.census()
    assert busy == [BusySource("release_op", "switch:op1")]
    # Excludes itself when it is the op running the census.
    assert await q.census(exclude_op_id="op1") == []
