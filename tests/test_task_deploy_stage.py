"""`deploy_stage` op tests (docs/plans/local-deploy.md §4/§5, step 2 of §17).

The §5 subprocess pipeline is driven by a fake stage runner that materializes
each step's on-disk effect (no real clone/venv/bun build), and the instance
guard is satisfied by a temp dual-slot layout with an explicit ``server_root``
seam — so every test is hermetic against temp dirs with a fake instance and
never touches a real production path (§14).

Covers the §14 stage bullets: local-path fetch of the exact sha, detached
checkout, venv-at-final-path, import-probe catching a broken slot,
``stage_failed`` capture, supersede-on-restage, the global deploy lock, the
fail-closed guards, the git prerequisites, source-key/retry naming, and event
emission.
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import aiosqlite
import pytest

from server.config import settings
from server.database import Database
from server.deploy_admission import DeployAdmissionGate
from server.deploy import DeployLayout
from server.task_board.delivery import DeliveryCoordinator
from server.task_board.models import DeployLockedError, TaskConflictError
from server.task_board.repository import TaskRepository
from server.task_board.workspaces import prepare_workspace


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


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "a@b.c")
    _git(path, "config", "user.name", "T")
    (path / "f.txt").write_text("base\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    _git(path, "branch", "-M", "main")


async def _ready_delivery(db, repo, tmp, agent, *, worker_commits=True):
    """A completed git_worktree run with an accepted, baseline-captured delivery
    (its ``attempt_head``/``commits_ahead`` populated) — the input a stage needs."""
    src = tmp / "src"
    _init_repo(src)
    board = await repo.create_board(
        name=f"D-{uuid.uuid4().hex[:8]}",
        working_dir=str(src),
        default_workspace_mode="git_worktree",
        allow_local_deploy=True,
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
    if worker_commits:
        _git(wt, "add", ".")
        _git(wt, "commit", "-qm", "worker change")
    await repo.set_run_metadata(task.id, run.id, {"prepared": prepared.metadata})
    await repo.complete_run(task.id, run.id, summary="did the work")
    coord = _coord(db, repo)
    delivery = await coord.accept(task.id, run.id)
    return board, task, await repo.get_run(run.id), src, delivery


def _coord(db, repo):
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


def _init_layout(root: Path, monkeypatch, *, seed_idle: bool = False) -> DeployLayout:
    """A minimal dual-slot layout with ``current -> a`` live; ``b`` is the idle
    slot the stage targets. ``seed_idle`` pre-clones ``b`` (with .git + .venv) to
    exercise the established-slot fast path (skips clone/venv)."""
    layout = DeployLayout.at(root)
    root.mkdir(parents=True, exist_ok=True)
    a = layout.slot_path("a")
    (a / ".git").mkdir(parents=True)
    (a / "web").mkdir()
    bindir = a / ".venv" / "bin"
    bindir.mkdir(parents=True)
    for exe in ("python", "pip", "owlery"):
        (bindir / exe).touch()
    layout.snapshots_path.mkdir(exist_ok=True)
    layout.journal_path.touch()
    layout.switch_current("a")
    if seed_idle:
        b = layout.slot_path("b")
        (b / ".git").mkdir(parents=True)
        (b / "web").mkdir()
        bbin = b / ".venv" / "bin"
        bbin.mkdir(parents=True)
        for exe in ("python", "pip", "owlery"):
            (bbin / exe).touch()
    monkeypatch.setattr(settings, "deploy_root", str(root))
    monkeypatch.setattr(settings, "debug", False)
    return layout


class FakeStageRunner:
    """Simulates the §5 pipeline subprocesses by materializing each step's
    on-disk artifact. ``fail_step`` makes one step exit non-zero with
    ``fail_output`` so a broken build / import crash can be exercised."""

    def __init__(self, *, fail_step: str | None = None, fail_output: str = "boom"):
        self.calls: list[tuple[list[str], Path, int]] = []
        self.fail_step = fail_step
        self.fail_output = fail_output

    @staticmethod
    def classify(argv: list[str]) -> str:
        if argv[:2] == ["git", "clone"]:
            return "clone"
        if argv[:2] == ["git", "fetch"]:
            return "fetch"
        if argv[:2] == ["git", "checkout"]:
            return "checkout"
        if argv[1:3] == ["-m", "venv"]:
            return "venv"
        if Path(argv[0]).name == "pip":
            return "pip"
        if argv[:2] == ["bun", "install"]:
            return "bun_install"
        if argv[:2] == ["bun", "run"]:
            return "build"
        if argv[1:2] == ["-c"]:
            return "import_probe"
        return "?"

    def __call__(self, argv: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
        self.calls.append((argv, cwd, timeout))
        step = self.classify(argv)
        if step == self.fail_step:
            return 1, self.fail_output
        if step == "clone":
            dst = Path(argv[3])
            (dst / ".git").mkdir(parents=True, exist_ok=True)
            (dst / "web").mkdir(exist_ok=True)
        elif step == "venv":
            bindir = cwd / ".venv" / "bin"
            bindir.mkdir(parents=True, exist_ok=True)
            for exe in ("python", "pip", "owlery"):
                (bindir / exe).touch()
        return 0, f"ok:{step}"

    def steps(self) -> list[str]:
        return [self.classify(argv) for argv, _, _ in self.calls]


# --------------------------------------------------------------- happy path


@pytest.mark.asyncio
async def test_stage_prepares_idle_slot_and_records_staged(store, tmp_path, monkeypatch):
    db, repo, tmp, agent = store
    board, task, run, src, delivery = await _ready_delivery(db, repo, tmp, agent)
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    coord = _coord(db, repo)
    runner = FakeStageRunner()

    d = await coord.deploy_stage(
        task.id, run.id, server_root=layout.slot_path("a"), stage_runner=runner
    )

    # Staging is transparent to the git-delivery status (restored to ready).
    assert d.status == "ready"

    # A fresh idle slot runs the whole §5 pipeline in order.
    assert runner.steps() == [
        "clone", "fetch", "checkout", "venv", "pip", "bun_install", "build",
        "import_probe",
    ]
    # The exact sha, by local path, and a DETACHED checkout — never a branch.
    fetch = next(a for a, _, _ in runner.calls if a[:2] == ["git", "fetch"])
    assert fetch == ["git", "fetch", delivery.repository, delivery.attempt_head]
    checkout = next(a for a, _, _ in runner.calls if a[:2] == ["git", "checkout"])
    assert checkout == ["git", "checkout", "--detach", delivery.attempt_head]
    # The venv is created at the slot's FINAL path (venvs are not relocatable).
    b = layout.slot_path("b")
    venv = next(a for a, c, _ in runner.calls if a[1:3] == ["-m", "venv"])
    venv_cwd = next(c for a, c, _ in runner.calls if a[1:3] == ["-m", "venv"])
    assert venv[1:] == ["-m", "venv", ".venv"] and venv_cwd == b
    pip = next(a for a, _, _ in runner.calls if Path(a[0]).name == "pip")
    assert pip == [str(b / ".venv" / "bin" / "pip"), "install", "-e", "."]
    # Every subprocess is bounded by the stage timeout.
    assert all(t == settings.deploy_stage_timeout_seconds for _, _, t in runner.calls)

    # The op: local (external=0), the §4 source key, staged_* folded into result.
    ops = await repo.list_delivery_ops(delivery.id)
    stage_ops = [o for o in ops if o.kind == "deploy_stage"]
    assert len(stage_ops) == 1
    op = stage_ops[0]
    assert op.state == "succeeded" and op.external is False
    assert op.source_key == f"task:{task.id}:run:{run.id}:delivery:deploy_stage"
    assert op.result["staged_slot"] == "b"
    assert op.result["staged_sha"] == delivery.attempt_head

    # The deployments row settled to `staged` on slot b, lock released.
    deployments = await repo.list_deployments()
    assert len(deployments) == 1
    dep = deployments[0]
    assert dep.state == "staged" and dep.slot == "b"
    assert dep.sha == delivery.attempt_head
    assert dep.source_repo == delivery.repository
    assert dep.delivery_id == delivery.id and dep.task_id == task.id
    assert await repo.get_active_deployment() is None
    assert op.result["deployment_id"] == dep.id

    # Op lifecycle emitted the shared audit events.
    kinds = [e.kind for e in await repo.list_task_events(task.id)]
    assert "delivery_op_started" in kinds and "delivery_op_finished" in kinds


@pytest.mark.asyncio
async def test_closed_admission_rejects_stage_before_it_is_planned(store, tmp_path, monkeypatch):
    db, repo, tmp, agent = store
    _board, task, run, _src, delivery = await _ready_delivery(db, repo, tmp, agent)
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    coord = _coord(db, repo)
    gate = DeployAdmissionGate()
    _bind_admission(coord, gate)
    await gate.close()

    with pytest.raises(TaskConflictError, match="deploy admission is closed"):
        await coord.deploy_stage(
            task.id, run.id, server_root=layout.slot_path("a"), stage_runner=FakeStageRunner()
        )

    assert [
        op for op in await repo.list_delivery_ops(delivery.id) if op.kind == "deploy_stage"
    ] == []


@pytest.mark.asyncio
async def test_established_slot_skips_clone_and_venv(store, tmp_path, monkeypatch):
    db, repo, tmp, agent = store
    board, task, run, src, delivery = await _ready_delivery(db, repo, tmp, agent)
    layout = _init_layout(tmp_path / "deploy", monkeypatch, seed_idle=True)
    coord = _coord(db, repo)
    runner = FakeStageRunner()

    await coord.deploy_stage(
        task.id, run.id, server_root=layout.slot_path("a"), stage_runner=runner
    )
    assert runner.steps() == [
        "fetch", "checkout", "pip", "bun_install", "build", "import_probe",
    ]


@pytest.mark.asyncio
async def test_stage_from_delivered_preserves_status(store, tmp_path, monkeypatch):
    db, repo, tmp, agent = store
    board, task, run, src, delivery = await _ready_delivery(db, repo, tmp, agent)
    bare = tmp / "remote.git"
    _git(tmp, "init", "-q", "--bare", str(bare))
    _git(src, "remote", "add", "origin", str(bare))
    coord = _coord(db, repo)
    d = await coord.deliver_op(task.id, run.id, kind="push")
    assert d.status == "delivered"

    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    d = await coord.deploy_stage(
        task.id, run.id, server_root=layout.slot_path("a"), stage_runner=FakeStageRunner()
    )
    # A staged deploy must not downgrade a delivered delivery.
    assert d.status == "delivered"


# ------------------------------------------------------------- stage failure


@pytest.mark.asyncio
async def test_import_probe_catches_broken_slot(store, tmp_path, monkeypatch):
    db, repo, tmp, agent = store
    board, task, run, src, delivery = await _ready_delivery(db, repo, tmp, agent)
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    coord = _coord(db, repo)
    runner = FakeStageRunner(fail_step="import_probe", fail_output="ImportError: boom")

    d = await coord.deploy_stage(
        task.id, run.id, server_root=layout.slot_path("a"), stage_runner=runner
    )
    assert d.status == "blocked" and d.reason_kind == "stage_failed"
    assert "import_probe" in d.reason_detail and "ImportError: boom" in d.reason_detail

    op = [o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_stage"][0]
    assert op.state == "failed"
    # The deployment row failed; the global lock is released.
    dep = (await repo.list_deployments())[0]
    assert dep.state == "failed"
    assert await repo.get_active_deployment() is None


@pytest.mark.asyncio
async def test_stage_failed_captures_output_and_leaves_instance(store, tmp_path, monkeypatch):
    db, repo, tmp, agent = store
    board, task, run, src, delivery = await _ready_delivery(db, repo, tmp, agent)
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    before = layout.current_link.resolve()
    coord = _coord(db, repo)
    runner = FakeStageRunner(fail_step="build", fail_output="bun build error: TS2304")

    d = await coord.deploy_stage(
        task.id, run.id, server_root=layout.slot_path("a"), stage_runner=runner
    )
    assert d.status == "blocked" and d.reason_kind == "stage_failed"
    assert "bun build error: TS2304" in d.reason_detail
    # import_probe never ran — the pipeline short-circuits on the first failure.
    assert "import_probe" not in runner.steps()
    # The running instance is untouched: `current` still points at the live slot.
    assert layout.current_link.resolve() == before
    assert layout.current_slot() == "a"


# ----------------------------------------------------- idle-slot path guard


@pytest.mark.asyncio
async def test_symlinked_idle_slot_refused(store, tmp_path, monkeypatch):
    db, repo, tmp, agent = store
    board, task, run, src, delivery = await _ready_delivery(db, repo, tmp, agent)
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    # Replace the idle slot `b` with a symlink pointing outside the deploy tree
    # (with a .git inside) — the guard must refuse rather than build there (§13.9).
    outside = tmp_path / "outside"
    (outside / ".git").mkdir(parents=True)
    (outside / "web").mkdir()
    layout.slot_path("b").symlink_to(outside)
    coord = _coord(db, repo)
    runner = FakeStageRunner()

    d = await coord.deploy_stage(
        task.id, run.id, server_root=layout.slot_path("a"), stage_runner=runner
    )
    assert d.status == "blocked" and d.reason_kind == "stage_failed"
    assert "slot_guard" in d.reason_detail
    # No subprocess ran against the foreign directory.
    assert runner.calls == []
    assert not (outside / ".venv").exists()


# -------------------------------------------------------------- supersede


@pytest.mark.asyncio
async def test_supersede_on_restage(store, tmp_path, monkeypatch):
    db, repo, tmp, agent = store
    board, task, run, src, delivery = await _ready_delivery(db, repo, tmp, agent)
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    coord = _coord(db, repo)

    await coord.deploy_stage(
        task.id, run.id, server_root=layout.slot_path("a"), stage_runner=FakeStageRunner()
    )
    first = (await repo.list_deployments())[0]
    assert first.state == "staged"

    # Re-stage: same idle slot, so the prior staged row is overwritten.
    await coord.deploy_stage(
        task.id, run.id, server_root=layout.slot_path("a"), stage_runner=FakeStageRunner()
    )
    deployments = {d.id: d for d in await repo.list_deployments()}
    assert deployments[first.id].state == "superseded"
    staged = [d for d in deployments.values() if d.state == "staged"]
    assert len(staged) == 1 and staged[0].id != first.id

    # A re-stage is an explicit new op with a :retry suffix (§3).
    keys = sorted(
        o.source_key for o in await repo.list_delivery_ops(delivery.id)
        if o.kind == "deploy_stage"
    )
    assert keys == [
        f"task:{task.id}:run:{run.id}:delivery:deploy_stage",
        f"task:{task.id}:run:{run.id}:delivery:deploy_stage:retry:1",
    ]


# ------------------------------------------------------------- global lock


@pytest.mark.asyncio
async def test_global_lock_blocks_second_deploy(store, tmp_path, monkeypatch):
    db, repo, tmp, agent = store
    board, task, run, src, delivery = await _ready_delivery(db, repo, tmp, agent)
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    coord = _coord(db, repo)

    # Another deploy holds the lock (a `staging` row exists). delivery_id is
    # NULL here — the point under test is the global lock, not the FK.
    holder = await repo.begin_deployment_staging(
        delivery_id=None, task_id="t-other", op_id=None,
        slot="a", sha="deadbeef" * 5, source_repo=str(src),
    )
    d = await coord.deploy_stage(
        task.id, run.id, server_root=layout.slot_path("a"),
        stage_runner=FakeStageRunner(),
    )
    # deploy_locked is a durable outcome (§4/§12): the op fails and the delivery
    # is blocked, naming the holder — an explicit new op is required to retry.
    assert d.status == "blocked" and d.reason_kind == "deploy_locked"
    assert holder.sha[:12] in d.reason_detail and "t-other" in d.reason_detail
    op = [o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_stage"][0]
    assert op.state == "failed"
    # The holder still holds the lock; the loser never inserted a deployments row.
    assert (await repo.get_active_deployment()).id == holder.id
    assert [dep.id for dep in await repo.list_deployments()] == [holder.id]


@pytest.mark.asyncio
async def test_board_without_opt_in_refuses(store, tmp_path, monkeypatch):
    db, repo, tmp, agent = store
    board, task, run, src, delivery = await _ready_delivery(db, repo, tmp, agent)
    # Flip the board's opt-in back off: deploy must refuse (§9).
    await repo.update_board(board.id, allow_local_deploy=False)
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    coord = _coord(db, repo)
    with pytest.raises(TaskConflictError) as exc:
        await coord.deploy_stage(
            task.id, run.id, server_root=layout.slot_path("a"),
            stage_runner=FakeStageRunner(),
        )
    assert "allow_local_deploy" in str(exc.value)
    # Op-free refusal: no op, delivery untouched.
    assert not [o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_stage"]
    assert (await repo.get_delivery(delivery.id)).status == "ready"


@pytest.mark.asyncio
async def test_boot_recovery_releases_staging_lock(store):
    db, repo, tmp, agent = store
    # A stage that died mid-pipeline leaves an orphan `staging` row holding the
    # global lock. Boot recovery must fail it so deploys are not wedged.
    orphan = await repo.begin_deployment_staging(
        delivery_id=None, task_id="t1", op_id=None,
        slot="a", sha="a" * 40, source_repo="/repo",
    )
    assert (await repo.get_active_deployment()).id == orphan.id
    failed = await repo.fail_orphan_staging_deployments(reason="server restarted")
    assert [d.id for d in failed] == [orphan.id]
    assert (await repo.get_deployment(orphan.id)).state == "failed"
    assert await repo.get_active_deployment() is None  # lock released
    # A second run is idempotent (nothing left staging).
    assert await repo.fail_orphan_staging_deployments(reason="again") == []


@pytest.mark.asyncio
async def test_restage_after_failure_clears_block(store, tmp_path, monkeypatch):
    db, repo, tmp, agent = store
    board, task, run, src, delivery = await _ready_delivery(db, repo, tmp, agent)
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    coord = _coord(db, repo)

    # First stage fails → delivery blocked(stage_failed).
    d = await coord.deploy_stage(
        task.id, run.id, server_root=layout.slot_path("a"),
        stage_runner=FakeStageRunner(fail_step="build"),
    )
    assert d.status == "blocked" and d.reason_kind == "stage_failed"
    # A green restage from that blocked state must clear the deploy-caused block
    # and succeed back to a ready delivery (§4), not perpetuate stage_failed.
    d = await coord.deploy_stage(
        task.id, run.id, server_root=layout.slot_path("a"),
        stage_runner=FakeStageRunner(),
    )
    assert d.status == "ready" and d.reason_kind is None and d.reason_detail is None


@pytest.mark.asyncio
async def test_restage_after_failure_keeps_merge_delivered(store, tmp_path, monkeypatch):
    db, repo, tmp, agent = store
    board, task, run, src, delivery = await _ready_delivery(db, repo, tmp, agent)
    coord = _coord(db, repo)
    # Deliver via merge (no push/PR) → delivered, merge_strategy set, no pushed_ref.
    d = await coord.deliver_op(task.id, run.id, kind="merge")
    assert d.status == "delivered" and d.pushed_ref is None and d.merge_strategy

    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    # Stage fails → blocked(stage_failed), overwriting the delivered status.
    d = await coord.deploy_stage(
        task.id, run.id, server_root=layout.slot_path("a"),
        stage_runner=FakeStageRunner(fail_step="build"),
    )
    assert d.status == "blocked" and d.reason_kind == "stage_failed"
    # A green restage must recognise the succeeded merge op and restore delivered
    # (not downgrade to ready just because there is no pushed_ref/pr_number).
    d = await coord.deploy_stage(
        task.id, run.id, server_root=layout.slot_path("a"),
        stage_runner=FakeStageRunner(),
    )
    assert d.status == "delivered" and d.reason_kind is None


@pytest.mark.asyncio
async def test_begin_staging_is_globally_unique(store):
    db, repo, tmp, agent = store
    await repo.begin_deployment_staging(
        delivery_id=None, task_id="t1", op_id=None,
        slot="a", sha="a" * 40, source_repo="/repo",
    )
    # A second in-flight deploy (any slot) is rejected by deployments_one_active.
    with pytest.raises(DeployLockedError) as exc:
        await repo.begin_deployment_staging(
            delivery_id=None, task_id="t2", op_id=None,
            slot="b", sha="b" * 40, source_repo="/repo",
        )
    assert "t1" in str(exc.value)


# --------------------------------------------------------- fail-closed guard


@pytest.mark.asyncio
async def test_precheck_fail_closed_paths(store, tmp_path, monkeypatch):
    db, repo, tmp, agent = store
    board, task, run, src, delivery = await _ready_delivery(db, repo, tmp, agent)
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    coord = _coord(db, repo)

    async def _stage(**kw):
        return await coord.deploy_stage(
            task.id, run.id, stage_runner=FakeStageRunner(), **kw
        )

    # 1. no deploy_root → deploy_not_initialized.
    monkeypatch.setattr(settings, "deploy_root", "")
    with pytest.raises(TaskConflictError) as e1:
        await _stage(server_root=layout.slot_path("a"))
    assert "deploy init" in str(e1.value)
    monkeypatch.setattr(settings, "deploy_root", str(layout.root))

    # 2. debug/reload dev server → refuse.
    monkeypatch.setattr(settings, "debug", True)
    with pytest.raises(TaskConflictError):
        await _stage(server_root=layout.slot_path("a"))
    monkeypatch.setattr(settings, "debug", False)

    # 3. code not running through `current` → refuse.
    with pytest.raises(TaskConflictError):
        await _stage(server_root=tmp_path / "elsewhere")

    # None of the refusals created an op or moved the delivery off ready.
    assert not [o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_stage"]
    assert (await repo.get_delivery(delivery.id)).status == "ready"
    assert await repo.list_deployments() == []


# ------------------------------------------------------------- prerequisites


@pytest.mark.asyncio
async def test_prerequisite_dirty_refuses(store, tmp_path, monkeypatch):
    db, repo, tmp, agent = store
    board, task, run, src, delivery = await _ready_delivery(
        db, repo, tmp, agent, worker_commits=False
    )
    assert delivery.dirty is True
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    coord = _coord(db, repo)
    with pytest.raises(TaskConflictError) as exc:
        await coord.deploy_stage(
            task.id, run.id, server_root=layout.slot_path("a"),
            stage_runner=FakeStageRunner(),
        )
    assert "dirty" in str(exc.value) or "commit" in str(exc.value)
    assert not [o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_stage"]


async def _zero_ahead_delivery(db, repo, tmp, agent):
    """A completed run whose worktree equals base — clean and zero commits
    ahead, so it reaches the nothing_to_deliver guard past the dirty check."""
    src = tmp / "src"
    _init_repo(src)
    board = await repo.create_board(
        name=f"Z-{uuid.uuid4().hex[:8]}", working_dir=str(src),
        default_workspace_mode="git_worktree", allow_local_deploy=True,
    )
    task = await repo.create_task(
        board_id=board.id, title="No-op", status="todo", assignee_agent_id=agent
    )
    run_id = uuid.uuid4().hex[:12]
    planned = str(Path(settings.resolved_task_workspaces_dir) / task.id / run_id)
    run = await repo.claim_ready(
        task.id, workspace_mode="git_worktree", workspace_path=planned, run_id=run_id
    )
    prepared = await prepare_workspace(
        mode="git_worktree", source_dir=str(src), task_id=task.id,
        run_id=run.id, attempt_no=run.attempt_no,
    )  # no worker change at all → worktree == base
    await repo.set_run_metadata(task.id, run.id, {"prepared": prepared.metadata})
    await repo.complete_run(task.id, run.id, summary="did nothing")
    coord = _coord(db, repo)
    delivery = await coord.accept(task.id, run.id)
    return task, await repo.get_run(run.id), delivery


@pytest.mark.asyncio
async def test_prerequisite_nothing_to_deliver_refuses(store, tmp_path, monkeypatch):
    db, repo, tmp, agent = store
    task, run, delivery = await _zero_ahead_delivery(db, repo, tmp, agent)
    assert delivery.dirty is False and (delivery.commits_ahead or 0) == 0
    layout = _init_layout(tmp_path / "deploy", monkeypatch)
    coord = _coord(db, repo)
    with pytest.raises(TaskConflictError) as exc:
        await coord.deploy_stage(
            task.id, run.id, server_root=layout.slot_path("a"),
            stage_runner=FakeStageRunner(),
        )
    assert "nothing_to_deliver" in str(exc.value)
    assert not [o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_stage"]


# ---------------------------------------------------- schema CHECK migration


@pytest.mark.asyncio
async def test_kind_check_migration_admits_deploy_stage(tmp_path):
    """A DB created before local deploy has a 6-kind CHECK on
    ``task_delivery_ops.kind``; opening it must rebuild the table to admit
    ``deploy_stage`` without losing existing op rows (§4). The pre-existing op
    references a real delivery row (delivery_id is NOT NULL with a CASCADE FK),
    so the rebuild's FK-checked copy resolves — exactly as on a production DB."""
    p = str(tmp_path / "old.db")
    conn = await aiosqlite.connect(p)
    # Seed with FKs off so the parent's own dangling task/run FKs don't matter —
    # only the op → delivery FK is exercised by the rebuild copy.
    await conn.execute("PRAGMA foreign_keys=OFF")
    await conn.executescript(
        """
        CREATE TABLE task_deliveries (
            id TEXT PRIMARY KEY, task_id TEXT, run_id TEXT, status TEXT,
            repository TEXT, attempt_branch TEXT, dirty INTEGER,
            created_at TEXT, updated_at TEXT);
        CREATE TABLE task_delivery_ops (
            id TEXT PRIMARY KEY,
            delivery_id TEXT NOT NULL REFERENCES task_deliveries(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN
              ('commit','push','pull_request','merge','branch_delete','worktree_remove')),
            source_key TEXT NOT NULL, external INTEGER NOT NULL, state TEXT NOT NULL,
            request TEXT NOT NULL DEFAULT '{}', result TEXT, error TEXT,
            actor_kind TEXT NOT NULL, actor_agent_id TEXT, started_at TEXT,
            finished_at TEXT, created_at TEXT NOT NULL, UNIQUE(source_key));
        INSERT INTO task_deliveries (id, task_id, run_id, status, repository,
            attempt_branch, dirty, created_at, updated_at)
            VALUES ('d1', 't1', 'r1', 'ready', '/repo', 'br', 0, 'now', 'now');
        INSERT INTO task_delivery_ops
            (id, delivery_id, kind, source_key, external, state, actor_kind, created_at)
            VALUES ('op1', 'd1', 'commit', 'k1', 0, 'succeeded', 'user', 'now');
        """
    )
    await conn.commit()
    await conn.close()

    # Reopening through Database runs initialize() → the guarded rebuild.
    db = Database(p)
    await db.initialize()
    c = db._conn
    row = await (await c.execute(
        "SELECT kind FROM task_delivery_ops WHERE id='op1'"
    )).fetchone()
    assert row[0] == "commit"  # the pre-existing op row survived the rebuild
    # deploy_stage is now a legal kind at the DB layer.
    await c.execute(
        "INSERT INTO task_delivery_ops (id, delivery_id, kind, source_key, external, "
        "state, actor_kind, created_at) "
        "VALUES ('op2','d1','deploy_stage','k2',0,'planned','user','now')"
    )
    await c.commit()
    sql = (await (await c.execute(
        "SELECT sql FROM sqlite_master WHERE name='task_delivery_ops'"
    )).fetchone())[0]
    assert "deploy_stage" in sql
    # The one-running / by-delivery indexes were recreated after the rename.
    idx = await (await c.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='index' AND name IN "
        "('task_delivery_ops_one_running','task_delivery_ops_delivery')"
    )).fetchone()
    assert idx[0] == 2
    await db.close()
