"""`deploy_rollback` op, quiesce gate, boot reconciliation, and probation
(docs/plans/local-deploy.md §7/§8, step 4 of §17).

`deploy_rollback` is the one per-run coordinator entry point that survived the
release-line-deploy cutover (docs/plans/release-line-deploy.md §3.4) — the
board-level `release_stage`/`release_switch`/`release_rollback` (tested in
`test_task_release_deploy.py`) replaced `deploy_stage`/`deploy_switch`, but a
per-run deployment can still be rolled back to its prior slot. `deploy_rollback`
drives the exact same handoff machinery (`_SwitchCtx`, `_perform_switch_handoff`,
`_snapshot_db`, quiesce census/drain/admission) as the removed `deploy_switch`
did, so this file exercises that shared machinery through `deploy_rollback`.

Everything runs against temp dirs and a fake instance (a dual-slot layout whose
`.venv/bin/owlery` binaries are stubs), never a real production path (§14). The
switcher itself is a seam here — its real-process behaviour is covered by
`test_task_deploy_switcher.py`; these tests drive the SERVER side: the census,
the drain gate, the handoff (snapshot + journal + detached spawn + broadcast +
shutdown), the §8 boot-reconciliation table, and probation.

The §14 switch/boot bullets covered:
- quiesce census, each busy source individually;
- drain pause/resume (and the honest `not_idle` refusal on timeout);
- snapshot checkpoint + copy (a self-contained, queryable snapshot);
- handoff journal contract;
- detachment: the spawned switcher owns its process group + session;
- every §8 journal-tail row;
- probation holds until the journal goes terminal (or the window elapses);
- stale journal → interrupted.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import server.switcher as switcher
from server.config import settings
from server.database import Database
from server.deploy import DeployLayout
from server.task_board.delivery import DeliveryCoordinator
from server.task_board.deploy_quiesce import BusySource, DeployQuiesce
from server.task_board.manager import DeployProbation, TaskBoardManager
from server.task_board.models import DeliveryConfirmationRequired, TaskConflictError
from server.task_board.repository import TaskRepository
from server.task_board.workspaces import prepare_workspace


# --------------------------------------------------------------- fixtures


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


def _coord(db, repo) -> DeliveryCoordinator:
    c = DeliveryCoordinator()
    c.bind(db=db, connectors=None, notify_terminal=None, repo=repo)
    return c


def _init_layout(root: Path, monkeypatch, *, seed_idle: bool = True) -> DeployLayout:
    """Dual-slot layout with `current -> a` live and `b` the idle target."""
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


async def _staged(db, repo, tmp, agent, monkeypatch, *, sha="targetsha01"):
    """A delivery with a `live` deployment on the current slot `a` and a
    `superseded` deployment on slot `b` — the rollback target `deploy_rollback`
    needs (the surviving per-run coordinator entry point after `deploy_stage`/
    `deploy_switch` were removed for the board-level release-line deploy,
    release-line-deploy.md §3.4). `staged` here names the rollback TARGET row
    (kept for minimal diff against the pre-cutover fixture name), reachable via
    `coord.deploy_rollback(live.id, confirm_rollback=True, ...)`.
    Returns (board, task, run, delivery, layout, coord, staged_target, live)."""
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
    _git(wt, "add", ".")
    _git(wt, "commit", "-qm", "worker change")
    await repo.set_run_metadata(task.id, run.id, {"prepared": prepared.metadata})
    await repo.complete_run(task.id, run.id, summary="did the work")
    coord = _coord(db, repo)
    delivery = await coord.accept(task.id, run.id)
    layout = _init_layout(tmp / "deploy", monkeypatch)

    async with repo._transaction() as conn:
        await conn.execute(
            "INSERT INTO deployments (id, delivery_id, task_id, op_id, slot, sha, "
            "source_repo, release_id, state, journal, created_at, updated_at) VALUES "
            "(?, ?, ?, NULL, 'a', 'oldsha0000', 'x', NULL, 'live', NULL, "
            "'2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')",
            (f"live-{run.id}", delivery.id, task.id),
        )
        await conn.execute(
            "INSERT INTO deployments (id, delivery_id, task_id, op_id, slot, sha, "
            "source_repo, release_id, state, journal, created_at, updated_at) VALUES "
            "(?, ?, ?, NULL, 'b', ?, 'x', NULL, 'superseded', NULL, "
            "'2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')",
            (f"sup-{run.id}", delivery.id, task.id, sha),
        )
    live = await repo.get_deployment(f"live-{run.id}")
    staged = await repo.get_deployment(f"sup-{run.id}")
    assert staged is not None and staged.slot == "b" and staged.state == "superseded"
    return board, task, await repo.get_run(run.id), delivery, layout, coord, staged, live


class FakeQuiesce:
    """Coordinator-side quiesce seam: a scripted census plus pause/resume spies."""

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
    """Inject the deploy-switch effects with spies; no real spawn/broadcast/kill."""
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


# ---------------------------------------------------- A. quiesce census


class _Task:
    def __init__(self, done):
        self._done = done

    def done(self):
        return self._done


class _Sess:
    def __init__(self, done):
        self._active_task = _Task(done)


class _SM:
    def __init__(self):
        self.sessions = {}


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Repo:
    def __init__(self):
        self.runs = []
        self.ops = []
        self.release_ops = []

    async def list_running_runs(self):
        return self.runs

    async def list_running_delivery_ops(self):
        return self.ops

    async def list_running_release_ops(self):
        return self.release_ops


class _DB:
    def __init__(self):
        self.parked = []
        self.deleg = []
        self.inj = []

    async def list_parked_turns(self):
        return self.parked

    async def list_running_delegation_runs(self):
        return self.deleg

    async def list_pending_session_injections(self):
        return self.inj


def _quiesce(sm=None, repo=None, db=None, bg=None, research=None):
    return DeployQuiesce(
        session_manager=sm or _SM(), repo=repo or _Repo(), db=db or _DB(),
        bg_task_manager=bg, research_manager=research,
    )


@pytest.mark.asyncio
async def test_census_idle_is_empty():
    assert await _quiesce().census() == []


@pytest.mark.asyncio
async def test_census_active_session_turn():
    sm = _SM()
    sm.sessions = {"live": _Sess(done=False), "finished": _Sess(done=True)}
    busy = await _quiesce(sm=sm).census()
    assert [b.kind for b in busy] == ["session_turn"]
    assert busy[0].detail == "live"


@pytest.mark.asyncio
async def test_census_parked_turn():
    db = _DB()
    db.parked = [{"session_id": "s7"}]
    busy = await _quiesce(db=db).census()
    assert busy == [BusySource("parked_turn", "s7")]


@pytest.mark.asyncio
async def test_census_task_run():
    repo = _Repo()
    repo.runs = [_Obj(id="run1")]
    busy = await _quiesce(repo=repo).census()
    assert busy == [BusySource("task_run", "run1")]


@pytest.mark.asyncio
async def test_census_other_delivery_op_excludes_self():
    repo = _Repo()
    repo.ops = [_Obj(id="me", kind="deploy_switch"), _Obj(id="other", kind="push")]
    busy = await _quiesce(repo=repo).census(exclude_op_id="me")
    assert busy == [BusySource("delivery_op", "push:other")]


@pytest.mark.asyncio
async def test_census_bg_research_delegation_injection():
    db = _DB()
    db.deleg = [{"id": "d1"}]
    db.inj = [{"id": "inj1"}]
    bg = _Obj(_running={"t1": object()})
    research = _Obj(_tasks={"r1": object(), "r2": object()})
    busy = await _quiesce(db=db, bg=bg, research=research).census()
    kinds = {b.kind for b in busy}
    assert kinds == {"bg_task", "research", "delegation", "session_injection"}
    research_src = next(b for b in busy if b.kind == "research")
    assert research_src.detail == "2 running"


# ------------------------------------------ B. DeployQuiesce drain primitives


@pytest.mark.asyncio
async def test_pause_resume_for_drain_hits_every_primitive():
    events = []

    class _SMspy:
        sessions = {}

        def pause_session_injection_dispatch(self):
            events.append("inj_pause")

        async def resume_session_injection_dispatch(self):
            events.append("inj_resume")

    class _Bridge:
        async def stop_all(self):
            events.append("bridge_stop")

        async def start_all(self):
            events.append("bridge_start")

    class _Sched:
        def pause(self):
            events.append("sched_pause")

        def resume(self):
            events.append("sched_resume")

    sched = _Sched()
    q = DeployQuiesce(
        session_manager=_SMspy(), repo=_Repo(), db=_DB(),
        pause_dispatch=lambda: events.append("disp_pause"),
        resume_dispatch=lambda: events.append("disp_resume"),
        bridge_manager_getter=lambda: _Bridge(),
        scheduler_getter=lambda: sched,
        parked_scheduler_getter=lambda: sched,
    )
    await q.pause_for_drain()
    await q.resume_from_drain()
    assert "inj_pause" in events and "disp_pause" in events
    assert "bridge_stop" in events and "sched_pause" in events
    assert "bridge_start" in events and "disp_resume" in events
    assert "inj_resume" in events and "sched_resume" in events


# --------------------------------------------------- C. coordinator gate


@pytest.mark.asyncio
async def test_switch_refuses_not_idle_with_census(store, monkeypatch):
    db, repo, tmp, agent = store
    _, task, run, delivery, layout, coord, staged, live = await _staged(db, repo, tmp, agent, monkeypatch)
    _wire_switch(coord, FakeQuiesce([[BusySource("session_turn", "s1")]]))

    d = await coord.deploy_rollback(
        live.id, confirm_rollback=True, server_root=layout.slot_path("a")
    )

    assert d.status == "blocked" and d.reason_kind == "not_idle"
    assert "session_turn:s1" in (d.reason_detail or "")
    op = next(o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_switch")
    assert op.state == "failed" and op.external is True
    # The rollback target is untouched — a busy refusal never flips anything.
    assert (await repo.get_deployment(staged.id)).state == "superseded"
    assert coord.spawns == [] and coord.shutdowns == []


@pytest.mark.asyncio
async def test_final_census_closes_admission_and_abort_reopens_it(store, monkeypatch):
    db, repo, tmp, agent = store
    _, task, run, delivery, layout, coord, staged, live = await _staged(db, repo, tmp, agent, monkeypatch)
    # The first census is idle, then work arrives before the handoff's final
    # census. Admission must be closed before that second census and reopened
    # once this still-live server refuses the switch.
    q = FakeQuiesce([[], [BusySource("session_turn", "raced")]])
    _wire_switch(coord, q)

    d = await coord.deploy_rollback(
        live.id, confirm_rollback=True, server_root=layout.slot_path("a")
    )

    assert d.status == "blocked" and d.reason_kind == "not_idle"
    assert q.admission_closes == 1 and q.admission_opens == 1
    assert (await repo.get_deployment(staged.id)).state == "superseded"


@pytest.mark.asyncio
async def test_pre_spawn_cancellation_reopens_admission(store, monkeypatch):
    db, repo, tmp, agent = store
    _, task, run, delivery, layout, coord, staged, live = await _staged(db, repo, tmp, agent, monkeypatch)
    q = FakeQuiesce([[]])
    _wire_switch(coord, q)

    async def _cancel_snapshot(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(coord, "_snapshot_db", _cancel_snapshot)
    with pytest.raises(asyncio.CancelledError):
        await coord.deploy_rollback(
            live.id, confirm_rollback=True, server_root=layout.slot_path("a")
        )

    assert q.admission_closes == 1 and q.admission_opens == 1
    assert coord.spawns == []


@pytest.mark.asyncio
async def test_post_spawn_exception_keeps_admission_closed(store, monkeypatch):
    db, repo, tmp, agent = store
    _, task, run, delivery, layout, coord, staged, live = await _staged(db, repo, tmp, agent, monkeypatch)
    q = FakeQuiesce([[]])
    _wire_switch(coord, q)

    async def _broadcast_failure(message):
        raise RuntimeError("broadcast unavailable after spawn")

    coord.broadcast_restarting = _broadcast_failure
    with pytest.raises(RuntimeError, match="broadcast unavailable"):
        await coord.deploy_rollback(
            live.id, confirm_rollback=True, server_root=layout.slot_path("a")
        )

    assert len(coord.spawns) == 1
    assert q.admission_closes == 1 and q.admission_opens == 0


# ---------------------------------------------- E. handoff + snapshot


@pytest.mark.asyncio
async def test_handoff_snapshot_journal_spawn_broadcast_shutdown(store, monkeypatch):
    db, repo, tmp, agent = store
    _, task, run, delivery, layout, coord, staged, live = await _staged(db, repo, tmp, agent, monkeypatch)
    q = FakeQuiesce([[]])
    _wire_switch(coord, q)

    await coord.deploy_rollback(
        live.id, confirm_rollback=True, server_root=layout.slot_path("a")
    )

    op = next(o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_switch")
    # The op stays RUNNING through handoff — the switcher/probation finalizes it.
    assert op.state == "running"
    assert op.result and op.result.get("journal_ref") == str(layout.journal_path)
    # Deployment took the global lock as `switching`, bound to this op.
    dep = await repo.get_deployment(staged.id)
    assert dep.state == "switching" and dep.op_id == op.id

    # The handoff journal line carries the full switch contract (§7.2 step 2).
    entries = switcher.Journal(str(layout.journal_path)).entries(op.id)
    handoff = next(e for e in entries if e["step"] == switcher.STEP_HANDOFF)
    d = handoff["detail"]
    assert d["from_slot"] == "a" and d["to_slot"] == "b"
    assert d["new_sha"] == staged.sha and d["old_pid"] == os.getpid()
    assert d["snapshot_path"].endswith(f"{op.id}.db")

    # The snapshot is a self-contained, queryable copy of the live DB (§7.4).
    snap = Path(d["snapshot_path"])
    assert snap.is_file()
    conn = sqlite3.connect(str(snap))
    got = conn.execute("SELECT id FROM tasks WHERE id=?", (task.id,)).fetchone()
    conn.close()
    assert got is not None  # committed rows survived checkpoint+copy

    # The detached switcher was spawned from the OLD slot; restart is under way.
    assert coord.spawns[0]["from_slot"] == "a" and coord.spawns[0]["op_id"] == op.id
    assert coord.broadcasts[0]["type"] == "server_restarting"
    assert q.admission_closes == 1 and q.admission_opens == 0
    assert coord.shutdowns == [True]


@pytest.mark.asyncio
async def test_snapshot_prunes_to_keep(store, monkeypatch):
    db, repo, tmp, agent = store
    _, task, run, delivery, layout, coord, staged, live = await _staged(db, repo, tmp, agent, monkeypatch)
    monkeypatch.setattr(settings, "deploy_keep_snapshots", 2)
    # Pre-seed 3 old snapshots; pruning keeps 2 total incl. the fresh one.
    for i in range(3):
        (layout.snapshots_path / f"old{i}.db").write_text("x")
    _wire_switch(coord, FakeQuiesce([[]]))

    await coord.deploy_rollback(
        live.id, confirm_rollback=True, server_root=layout.slot_path("a")
    )

    op = next(o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_switch")
    snaps = {p.name for p in layout.snapshots_path.glob("*.db")}
    assert f"{op.id}.db" in snaps and len(snaps) == 2  # own snapshot never pruned


# ------------------------------------------------- F. detachment (pgid/sid)


_DETACH_STUB = """\
#!{python}
import os
out = os.environ["DETACH_OUT"]
with open(out, "w") as f:
    f.write("{},{},{}".format(os.getpid(), os.getpgid(0), os.getsid(0)))
""".replace("{python}", sys.executable)


@pytest.mark.asyncio
async def test_spawn_switcher_is_fully_detached(tmp_path, monkeypatch):
    import time

    root = tmp_path / "deploy"
    bindir = root / "a" / ".venv" / "bin"
    bindir.mkdir(parents=True)
    owlery = bindir / "owlery"
    owlery.write_text(_DETACH_STUB)
    owlery.chmod(0o755)
    (root / switcher.JOURNAL_NAME).touch()
    out = tmp_path / "detach.txt"

    pid = switcher.spawn_switcher(
        root=str(root), from_slot="a", op_id="op1",
        switch_timeout=1, health_timeout=1,
        env={**os.environ, "DETACH_OUT": str(out)},
    )
    for _ in range(200):
        if out.exists():
            break
        time.sleep(0.02)
    child_pid, pgid, sid = (int(x) for x in out.read_text().split(","))
    # The grandchild lives in the NEW session `setsid` created in the intermediate
    # child (so pgid == sid), entirely separate from this (server) process's
    # session and group — it can never be reaped by the dying server's
    # process-group cleanup (§7.2).
    assert pgid == sid
    assert child_pid != os.getpid()
    assert sid != os.getsid(0) and pgid != os.getpgid(0)


# ------------------------------------------- G. §8 boot reconciliation


async def _handed_off(db, repo, tmp, agent, monkeypatch):
    """Drive a full handoff via `deploy_rollback`, then return
    (manager, coord, op, staged, layout, ids, live) with the op `running`, the
    deployment `switching`, and a `handoff` journal tail — the state every §8
    boot row starts from. `live` is the fixture's own live deployment (slot
    `a`) being rolled back away from — still `live` at this point, since the
    supersede only happens at finalize time (§8)."""
    _, task, run, delivery, layout, coord, staged, live = await _staged(db, repo, tmp, agent, monkeypatch)
    _wire_switch(coord, FakeQuiesce([[]]))
    await coord.deploy_rollback(
        live.id, confirm_rollback=True, server_root=layout.slot_path("a")
    )
    op = next(o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_switch")
    return _manager(db, repo), coord, op, staged, layout, (task, run, delivery), live


def _append(layout, op_id, step, detail):
    switcher.Journal(str(layout.journal_path)).append(op_id, step, detail)


@pytest.mark.asyncio
async def test_reconcile_switched_ok(store, monkeypatch):
    """Also covers the former `..._supersedes_prior_live` case: the fixture's
    own `live` deployment (slot `a`, the rollback's FROM side) is exactly the
    prior-live row a successful switch must supersede — no separately seeded
    row is needed once `deploy_rollback` (rather than a bare `deploy_switch`
    onto an idle slot with no live deployment yet) is the entry point under
    test."""
    db, repo, tmp, agent = store
    m, coord, op, staged, layout, (task, run, delivery), live = await _handed_off(db, repo, tmp, agent, monkeypatch)
    _append(layout, op.id, switcher.STEP_SWITCHED_OK, {"slot": "b", "sha": staged.sha, "pid": 9})

    assert await m.reconcile_deploy_switch_ops() is None  # terminal, no probation

    op = next(o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_switch")
    assert op.state == "succeeded"
    assert (await repo.get_deployment(staged.id)).state == "live"
    assert (await repo.get_deployment(live.id)).state == "superseded"
    d = await repo.get_delivery(delivery.id)
    assert d.deployed_sha == staged.sha and d.deployed_slot == "b"


@pytest.mark.asyncio
async def test_confirmed_rollback_reuses_the_switch_handoff(store, monkeypatch):
    """A later rollback is a fresh deploy_switch targeting the superseded slot,
    with the same snapshot/journal/quiesce handoff — never a direct symlink
    flip. `_handed_off` already drives the FIRST rollback (fixture's `live`,
    slot a -> fixture's `staged` target, slot b); once that is reconciled, the
    now-superseded former-`live` row (slot a) becomes the target for a SECOND,
    genuine rollback attempt — no manually seeded row needed."""
    db, repo, tmp, agent = store
    m, coord, op, staged, layout, (task, run, delivery), live = await _handed_off(
        db, repo, tmp, agent, monkeypatch
    )
    _append(layout, op.id, switcher.STEP_SWITCHED_OK, {"slot": "b", "sha": staged.sha})
    await m.reconcile_deploy_switch_ops()
    layout.switch_current("b")
    assert (await repo.get_deployment(staged.id)).state == "live"
    assert (await repo.get_deployment(live.id)).state == "superseded"

    with pytest.raises(DeliveryConfirmationRequired) as refused:
        await coord.deploy_rollback(
            staged.id, confirm_rollback=False, server_root=layout.slot_path("b")
        )
    assert refused.value.confirmation == "confirm_rollback"

    await coord.deploy_rollback(
        staged.id, confirm_rollback=True, server_root=layout.slot_path("b")
    )
    target = await repo.get_deployment(live.id)
    assert target.state == "switching"
    rollback_op = [
        item for item in await repo.list_delivery_ops(delivery.id)
        if item.kind == "deploy_switch" and item.id != op.id
    ]
    assert len(rollback_op) == 1 and rollback_op[0].state == "running"
    entries = switcher.Journal(str(layout.journal_path)).entries(rollback_op[0].id)
    tail = entries[-1]
    assert tail["detail"]["to_slot"] == "a" and tail["detail"]["new_sha"] == live.sha

    # A failed health check restores the pre-handoff DB snapshot. That snapshot
    # predates `superseded -> switching`, so terminal reconciliation must locate
    # the exact rollback target without its op_id binding.
    async with repo._transaction() as conn:
        await conn.execute(
            "UPDATE deployments SET state='superseded', op_id=NULL WHERE id=?",
            (live.id,),
        )
    await repo.finalize_deploy_rolled_back(
        rollback_op[0].id,
        reason="health_timeout",
        target_slot="a",
        target_sha=live.sha,
    )
    assert (await repo.get_deployment(live.id)).state == "rolled_back"


@pytest.mark.asyncio
async def test_reconcile_rolled_back(store, monkeypatch):
    db, repo, tmp, agent = store
    m, coord, op, staged, layout, (task, run, delivery), live = await _handed_off(db, repo, tmp, agent, monkeypatch)
    _append(layout, op.id, switcher.STEP_ROLLED_BACK, {"reason": "health_timeout"})

    await m.reconcile_deploy_switch_ops()

    op = next(o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_switch")
    assert op.state == "failed"
    assert (await repo.get_deployment(staged.id)).state == "rolled_back"
    d = await repo.get_delivery(delivery.id)
    assert d.status == "blocked" and d.reason_kind == "health_failed"


@pytest.mark.asyncio
async def test_reconcile_rollback_incomplete(store, monkeypatch):
    db, repo, tmp, agent = store
    m, coord, op, staged, layout, (task, run, delivery), live = await _handed_off(db, repo, tmp, agent, monkeypatch)
    _append(layout, op.id, switcher.STEP_ROLLBACK_INCOMPLETE,
            {"stage": "port_not_freed", "reason": "health_timeout"})

    await m.reconcile_deploy_switch_ops()

    op = next(o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_switch")
    assert op.state == "failed"
    assert (await repo.get_deployment(staged.id)).state == "failed"
    d = await repo.get_delivery(delivery.id)
    assert d.status == "blocked" and d.reason_kind == "health_failed"


@pytest.mark.asyncio
async def test_reconcile_old_wont_die_reverts_to_staged(store, monkeypatch):
    db, repo, tmp, agent = store
    m, coord, op, staged, layout, (task, run, delivery), live = await _handed_off(db, repo, tmp, agent, monkeypatch)
    _append(layout, op.id, switcher.STEP_OLD_WONT_DIE, {"old_pid": 1, "port": 8000})

    await m.reconcile_deploy_switch_ops()

    op = next(o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_switch")
    assert op.state == "failed"
    # The flip never happened → the deployment is deployable again (§8).
    assert (await repo.get_deployment(staged.id)).state == "staged"
    assert await repo.get_active_deployment() is None  # lock released
    d = await repo.get_delivery(delivery.id)
    assert d.status == "blocked" and d.reason_kind == "old_wont_die"


@pytest.mark.asyncio
async def test_reconcile_handoff_only_is_interrupted(store, monkeypatch):
    db, repo, tmp, agent = store
    m, coord, op, staged, layout, (task, run, delivery), live = await _handed_off(db, repo, tmp, agent, monkeypatch)
    # No switcher line beyond `handoff`.
    assert await m.reconcile_deploy_switch_ops() is None

    op = next(o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_switch")
    assert op.state == "interrupted"
    assert (await repo.get_deployment(staged.id)).state == "failed"
    d = await repo.get_delivery(delivery.id)
    assert d.status == "blocked" and d.reason_kind == "interrupted"


@pytest.mark.asyncio
async def test_reconcile_stale_flip_done_is_interrupted(store, monkeypatch):
    db, repo, tmp, agent = store
    m, coord, op, staged, layout, (task, run, delivery), live = await _handed_off(db, repo, tmp, agent, monkeypatch)
    monkeypatch.setattr(settings, "deploy_health_timeout_seconds", 60)
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=9999)).isoformat()
    with open(layout.journal_path, "a") as f:
        f.write(json.dumps({"ts": old_ts, "op_id": op.id, "step": "flip_done", "detail": {}}) + "\n")

    assert await m.reconcile_deploy_switch_ops() is None  # too stale for probation

    op = next(o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_switch")
    assert op.state == "interrupted"


@pytest.mark.asyncio
async def test_reconcile_after_real_snapshot_restore(store, monkeypatch):
    """A rollback (or a boot reading the restored DB) must still locate and
    finalize the deployment even though the pre-switch snapshot it restored
    (§7.4) predates the CAS that bound the deployment row to this op (§7.2:
    the snapshot is step 1, `begin_deployment_switching` is step 3) — so
    restoring it reverts that row to its pre-handoff `superseded` state, still
    unbound (`op_id` NULL, as the fixture seeded it) rather than bound to this
    `deploy_switch` op — exactly the way `server.switcher._restore_snapshot`
    does on a real rollback. This drives the REAL snapshot file (checkpoint +
    copy, taken by the coordinator during handoff) back over a fresh DB
    file/connection — not a scripted journal fixture — so it exercises the
    actual on-disk sequence, not just the reconciler's journal-reading half."""
    db, repo, tmp, agent = store
    m, coord, op, staged, layout, (task, run, delivery), live = await _handed_off(
        db, repo, tmp, agent, monkeypatch
    )

    # Sanity: the deployment row IS bound to this op right after handoff —
    # the CAS (§7.2 step 3) ran after the snapshot (§7.2 step 1).
    dep_before = await repo.get_deployment(staged.id)
    assert dep_before.state == "switching" and dep_before.op_id == op.id

    snapshot_path = layout.snapshots_path / f"{op.id}.db"
    assert snapshot_path.is_file()
    db_path = tmp / "owlery.db"

    # Close both connections (a real rollback/boot never restores a snapshot
    # over a live connection), then restore exactly as the switcher does
    # (server/switcher.py `_restore_snapshot`): copy the snapshot over the
    # live DB and drop any stale -wal/-shm sidecar of the live file.
    await repo.close()
    await db.close()
    shutil.copyfile(str(snapshot_path), str(db_path))
    for sidecar in (f"{db_path}-wal", f"{db_path}-shm"):
        try:
            os.unlink(sidecar)
        except FileNotFoundError:
            pass

    db2 = Database(str(db_path))
    await db2.initialize()
    repo2 = TaskRepository(str(db_path))
    await repo2.initialize()
    try:
        # The restore really did revert the binding — this is the trap: a
        # naive `WHERE op_id=?` lookup for the deploy_switch op now finds
        # nothing (the row's op_id reverted to NULL, as seeded pre-handoff).
        dep_after_restore = await repo2.get_deployment(staged.id)
        assert dep_after_restore.state == "superseded"
        assert dep_after_restore.op_id is None
        assert dep_after_restore.op_id != op.id

        # The switcher's rollback then journals its verdict for this op (§8).
        _append(layout, op.id, switcher.STEP_ROLLED_BACK, {"reason": "health_timeout"})

        m2 = _manager(db2, repo2)
        assert await m2.reconcile_deploy_switch_ops() is None  # terminal, no probation

        op_after = next(
            o for o in await repo2.list_delivery_ops(delivery.id) if o.kind == "deploy_switch"
        )
        assert op_after.state == "failed"
        dep_final = await repo2.get_deployment(staged.id)
        # Must land on the §8 terminal state, never remain `staged`/`switching`.
        assert dep_final.state == "rolled_back"
        assert dep_final.op_id == op.id  # re-bound by the fallback locator
        d = await repo2.get_delivery(delivery.id)
        assert d.status == "blocked" and d.reason_kind == "health_failed"
    finally:
        await repo2.close()
        await db2.close()


# --------------------------------------------------- H. probation


@pytest.mark.asyncio
async def test_fresh_flip_done_enters_probation(store, monkeypatch):
    db, repo, tmp, agent = store
    m, coord, op, staged, layout, (task, run, delivery), live = await _handed_off(db, repo, tmp, agent, monkeypatch)
    _append(layout, op.id, switcher.STEP_FLIP_DONE, {"from_slot": "a", "to_slot": "b"})

    probation = await m.reconcile_deploy_switch_ops()

    assert isinstance(probation, DeployProbation) and probation.op_id == op.id
    # Left RUNNING for the probation monitor (§7.5).
    op = next(o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_switch")
    assert op.state == "running"


@pytest.mark.asyncio
async def test_probation_uses_remaining_window_from_flip_done(store, monkeypatch):
    db, repo, tmp, agent = store
    m, coord, op, staged, layout, (task, run, delivery), live = await _handed_off(db, repo, tmp, agent, monkeypatch)
    monkeypatch.setattr(settings, "deploy_health_timeout_seconds", 0.06)
    flipped_at = datetime.now(timezone.utc) - timedelta(seconds=0.045)
    with open(layout.journal_path, "a") as f:
        f.write(json.dumps({
            "ts": flipped_at.isoformat(), "op_id": op.id,
            "step": switcher.STEP_FLIP_DONE, "detail": {},
        }) + "\n")

    probation = await m.reconcile_deploy_switch_ops()
    assert probation is not None
    assert probation.health_deadline == flipped_at + timedelta(seconds=0.06)
    released = []
    started = time.monotonic()
    outcome = await m.run_deploy_probation(probation, on_release=lambda: _release(released))

    assert outcome == "timeout" and released == [True]
    # A restarted server gets only the roughly 15ms remainder, not another 60ms.
    assert time.monotonic() - started < 0.045


@pytest.mark.asyncio
async def test_probation_applies_terminal_tail_even_after_window_elapsed(store, monkeypatch):
    db, repo, tmp, agent = store
    m, coord, op, staged, layout, (task, run, delivery), live = await _handed_off(db, repo, tmp, agent, monkeypatch)
    _append(layout, op.id, switcher.STEP_FLIP_DONE, {"from_slot": "a", "to_slot": "b"})
    probation = await m.reconcile_deploy_switch_ops()
    assert probation is not None
    _append(layout, op.id, switcher.STEP_SWITCHED_OK, {"slot": "b", "sha": staged.sha})
    expired = DeployProbation(
        op_id=probation.op_id,
        journal_path=probation.journal_path,
        health_deadline=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    released = []

    outcome = await m.run_deploy_probation(expired, on_release=lambda: _release(released))

    assert outcome == switcher.STEP_SWITCHED_OK and released == [True]
    assert (await repo.get_deployment(staged.id)).state == "live"


async def _release(released):
    released.append(True)


@pytest.mark.asyncio
async def test_probation_releases_on_switched_ok(store, monkeypatch):
    db, repo, tmp, agent = store
    m, coord, op, staged, layout, (task, run, delivery), live = await _handed_off(db, repo, tmp, agent, monkeypatch)
    _append(layout, op.id, switcher.STEP_FLIP_DONE, {"from_slot": "a", "to_slot": "b"})
    probation = await m.reconcile_deploy_switch_ops()
    # The switcher confirms health.
    _append(layout, op.id, switcher.STEP_SWITCHED_OK, {"slot": "b", "sha": staged.sha})

    released = []

    async def _release():
        released.append(True)

    outcome = await m.run_deploy_probation(probation, on_release=_release)

    assert outcome == switcher.STEP_SWITCHED_OK and released == [True]
    op = next(o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_switch")
    assert op.state == "succeeded"
    assert (await repo.get_deployment(staged.id)).state == "live"


@pytest.mark.asyncio
async def test_probation_times_out_to_interrupted(store, monkeypatch):
    db, repo, tmp, agent = store
    m, coord, op, staged, layout, (task, run, delivery), live = await _handed_off(db, repo, tmp, agent, monkeypatch)
    monkeypatch.setattr(settings, "deploy_health_timeout_seconds", 0.05)
    _append(layout, op.id, switcher.STEP_FLIP_DONE, {"from_slot": "a", "to_slot": "b"})
    probation = await m.reconcile_deploy_switch_ops()

    released = []

    async def _release():
        released.append(True)

    outcome = await m.run_deploy_probation(probation, on_release=_release)

    assert outcome == "timeout" and released == [True]  # producers always released
    op = next(o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_switch")
    assert op.state == "interrupted"
    assert (await repo.get_deployment(staged.id)).state == "failed"


# --------------------------------------------------- I. preconditions


@pytest.mark.asyncio
async def test_switch_requires_board_opt_in(store, monkeypatch):
    db, repo, tmp, agent = store
    board, task, run, delivery, layout, coord, staged, live = await _staged(db, repo, tmp, agent, monkeypatch)
    await repo.update_board(board.id, allow_local_deploy=False)
    _wire_switch(coord, FakeQuiesce([[]]))

    with pytest.raises(TaskConflictError):
        await coord.deploy_rollback(
            live.id, confirm_rollback=True, server_root=layout.slot_path("a")
        )
    # No op planned; nothing flipped.
    assert not [o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_switch"]
    assert (await repo.get_deployment(staged.id)).state == "superseded"


@pytest.mark.asyncio
async def test_switch_requires_a_staged_slot(store, monkeypatch):
    """`deploy_rollback` refuses when no superseded slot is available to roll
    back to — the rollback mirror of the removed `deploy_switch`'s
    `nothing_staged` refusal."""
    db, repo, tmp, agent = store
    _, task, run, delivery, layout, coord, staged, live = await _staged(db, repo, tmp, agent, monkeypatch)
    # Promote the rollback target out of `superseded` so nothing is left to
    # roll back to.
    async with repo._transaction() as conn:
        await conn.execute("UPDATE deployments SET state='failed' WHERE id=?", (staged.id,))
    _wire_switch(coord, FakeQuiesce([[]]))

    with pytest.raises(TaskConflictError, match="no superseded deployment is available"):
        await coord.deploy_rollback(
            live.id, confirm_rollback=True, server_root=layout.slot_path("a")
        )
    assert not [o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_switch"]


# ------------------------------------- J. successful retry clears a switch-
#                                          owned blocker (§9, micro-fix C2)


async def _staged_with_push(db, repo, tmp, agent, monkeypatch):
    """A rollback-ready delivery (live slot a / superseded target slot b, per
    `_staged`) that was ALSO successfully pushed — the "was this delivery ever
    delivered" branch of the switch-success fold must land `delivered`, not
    `ready`."""
    src = tmp / "src"
    _init_repo(src)
    bare = tmp / "remote.git"
    _git(tmp, "init", "-q", "--bare", str(bare))
    _git(src, "remote", "add", "origin", str(bare))
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
    _git(wt, "add", ".")
    _git(wt, "commit", "-qm", "worker change")
    await repo.set_run_metadata(task.id, run.id, {"prepared": prepared.metadata})
    await repo.complete_run(task.id, run.id, summary="did the work")
    coord = _coord(db, repo)
    delivery = await coord.accept(task.id, run.id)
    delivery = await coord.deliver_op(task.id, run.id, kind="push")
    assert delivery.status == "delivered" and delivery.pushed_ref
    layout = _init_layout(tmp / "deploy", monkeypatch)

    async with repo._transaction() as conn:
        await conn.execute(
            "INSERT INTO deployments (id, delivery_id, task_id, op_id, slot, sha, "
            "source_repo, release_id, state, journal, created_at, updated_at) VALUES "
            "(?, ?, ?, NULL, 'a', 'oldsha0000', 'x', NULL, 'live', NULL, "
            "'2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')",
            (f"live-{run.id}", delivery.id, task.id),
        )
        await conn.execute(
            "INSERT INTO deployments (id, delivery_id, task_id, op_id, slot, sha, "
            "source_repo, release_id, state, journal, created_at, updated_at) VALUES "
            "(?, ?, ?, NULL, 'b', 'targetsha01', 'x', NULL, 'superseded', NULL, "
            "'2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')",
            (f"sup-{run.id}", delivery.id, task.id),
        )
    live = await repo.get_deployment(f"live-{run.id}")
    staged = await repo.get_deployment(f"sup-{run.id}")
    assert staged is not None and staged.slot == "b" and staged.state == "superseded"
    return task, await repo.get_run(run.id), delivery, layout, coord, staged, live


async def _staged_then_conflicted(db, repo, tmp, agent, monkeypatch):
    """A rollback-ready delivery whose status was independently pushed to
    `conflicted` by a merge attempt — a REAL git conflict, never something the
    switch machinery itself produced."""
    _, task, run, delivery, layout, coord, staged, live = await _staged(db, repo, tmp, agent, monkeypatch)
    src = Path(delivery.repository)
    # Diverge the base branch so a fast-forward merge cannot proceed.
    (src / "f.txt").write_text("diverged base\n")
    _git(src, "commit", "-aqm", "diverge")
    delivery = await coord.deliver_op(
        task.id, run.id, kind="merge", merge_strategy="fast_forward_only"
    )
    assert delivery.status == "conflicted" and delivery.reason_kind == "conflict"
    return task, run, delivery, layout, coord, staged, live


@pytest.mark.asyncio
async def test_successful_retry_clears_not_idle_blocker(store, monkeypatch):
    db, repo, tmp, agent = store
    _, task, run, delivery, layout, coord, staged, live = await _staged(db, repo, tmp, agent, monkeypatch)
    _wire_switch(coord, FakeQuiesce([[BusySource("session_turn", "s1")]]))

    d = await coord.deploy_rollback(
        live.id, confirm_rollback=True, server_root=layout.slot_path("a")
    )
    assert d.status == "blocked" and d.reason_kind == "not_idle"
    assert (await repo.get_deployment(staged.id)).state == "superseded"

    # An explicit new attempt — a fresh `:retry:1` op, never a tick (§4).
    _wire_switch(coord, FakeQuiesce([[]]))
    await coord.deploy_rollback(
        live.id, confirm_rollback=True, server_root=layout.slot_path("a")
    )
    ops = [o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_switch"]
    assert len(ops) == 2
    retry_op = ops[1]
    assert retry_op.source_key.endswith(":retry:1") and retry_op.state == "running"

    _append(layout, retry_op.id, switcher.STEP_SWITCHED_OK, {"slot": "b", "sha": staged.sha})
    m = _manager(db, repo)
    assert await m.reconcile_deploy_switch_ops() is None  # terminal, no probation

    final = await repo.get_delivery(delivery.id)
    # A finally-successful, explicit switch retry clears the SWITCH-OWNED
    # blocker its own earlier attempt produced — landing `ready` (no
    # push/PR/merge ever succeeded on this delivery).
    assert final.status == "ready"
    assert final.reason_kind is None and final.reason_detail is None
    assert final.deployed_sha == staged.sha and final.deployed_slot == "b"
    assert (await repo.get_deployment(staged.id)).state == "live"


@pytest.mark.asyncio
async def test_successful_retry_after_push_lands_delivered(store, monkeypatch):
    db, repo, tmp, agent = store
    task, run, delivery, layout, coord, staged, live = await _staged_with_push(
        db, repo, tmp, agent, monkeypatch
    )
    _wire_switch(coord, FakeQuiesce([[BusySource("session_turn", "s1")]]))

    d = await coord.deploy_rollback(
        live.id, confirm_rollback=True, server_root=layout.slot_path("a")
    )
    assert d.status == "blocked" and d.reason_kind == "not_idle"

    _wire_switch(coord, FakeQuiesce([[]]))
    await coord.deploy_rollback(
        live.id, confirm_rollback=True, server_root=layout.slot_path("a")
    )
    retry_op = next(
        o for o in await repo.list_delivery_ops(delivery.id)
        if o.kind == "deploy_switch" and o.source_key.endswith(":retry:1")
    )
    _append(layout, retry_op.id, switcher.STEP_SWITCHED_OK, {"slot": "b", "sha": staged.sha})
    m = _manager(db, repo)
    assert await m.reconcile_deploy_switch_ops() is None

    final = await repo.get_delivery(delivery.id)
    # `was_delivered` is authoritative (the succeeded push op), not inferred
    # from `pushed_ref` alone — the clear lands `delivered`, not `ready`.
    assert final.status == "delivered"
    assert final.reason_kind is None and final.reason_detail is None
    assert final.pushed_ref  # the earlier delivery fact survives untouched


@pytest.mark.asyncio
async def test_switch_success_never_clears_real_conflict(store, monkeypatch):
    db, repo, tmp, agent = store
    task, run, delivery, layout, coord, staged, live = await _staged_then_conflicted(
        db, repo, tmp, agent, monkeypatch
    )
    _wire_switch(coord, FakeQuiesce([[]]))

    await coord.deploy_rollback(
        live.id, confirm_rollback=True, server_root=layout.slot_path("a")
    )
    op = next(o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_switch")
    _append(layout, op.id, switcher.STEP_SWITCHED_OK, {"slot": "b", "sha": staged.sha})
    m = _manager(db, repo)
    assert await m.reconcile_deploy_switch_ops() is None

    final = await repo.get_delivery(delivery.id)
    # The switch itself succeeded (deployment live, sha/slot folded in) but a
    # REAL git conflict — neither caused nor resolvable by the switch — must
    # never be cleared by it, unlike a switch-owned `not_idle`/`health_failed`/
    # `old_wont_die` blocker.
    assert final.status == "conflicted" and final.reason_kind == "conflict"
    assert final.deployed_sha == staged.sha and final.deployed_slot == "b"
    assert (await repo.get_deployment(staged.id)).state == "live"


@pytest.mark.asyncio
async def test_successful_retry_after_deploy_locked_leaves_delivery_blocked(store, monkeypatch):
    """A switch attempt that aborts `deploy_locked` (another deploy holds the
    global lock at the `begin_deployment_switching` CAS, §7.2 step 3) blocks
    the delivery. Releasing the holder and retrying explicitly then succeeds —
    the deployment flips live and the delivery's sha/slot fold in — but
    `deploy_locked` reflects contention with a DIFFERENT deploy, never this
    switch's own outcome, so it is deliberately excluded from
    `SWITCH_OWNED_REASON_KINDS` (§9): the retry's success must leave the
    delivery blocked(deploy_locked), exactly like a real git conflict in
    `test_switch_success_never_clears_real_conflict`."""
    db, repo, tmp, agent = store
    _, task, run, delivery, layout, coord, staged, live = await _staged(db, repo, tmp, agent, monkeypatch)
    _wire_switch(coord, FakeQuiesce([[]]))

    # Another deploy holds the global lock (a `staging` row) when this switch
    # attempts its `begin_deployment_switching` CAS.
    holder = await repo.begin_deployment_staging(
        delivery_id=None, task_id="t-other", op_id=None,
        slot="a", sha="deadbeef" * 5, source_repo=str(layout.root),
    )

    d = await coord.deploy_rollback(
        live.id, confirm_rollback=True, server_root=layout.slot_path("a")
    )
    assert d.status == "blocked" and d.reason_kind == "deploy_locked"
    assert (await repo.get_deployment(staged.id)).state == "superseded"  # never flipped

    # Release the holder — the global lock is free again.
    await repo.mark_deployment_failed(holder.id)

    # An explicit new attempt — a fresh `:retry:1` op, never a tick (§4).
    await coord.deploy_rollback(
        live.id, confirm_rollback=True, server_root=layout.slot_path("a")
    )
    ops = [o for o in await repo.list_delivery_ops(delivery.id) if o.kind == "deploy_switch"]
    assert len(ops) == 2
    retry_op = ops[1]
    assert retry_op.source_key.endswith(":retry:1") and retry_op.state == "running"

    _append(layout, retry_op.id, switcher.STEP_SWITCHED_OK, {"slot": "b", "sha": staged.sha})
    m = _manager(db, repo)
    assert await m.reconcile_deploy_switch_ops() is None  # terminal, no probation

    final = await repo.get_delivery(delivery.id)
    # The retry succeeded — deployment live, sha/slot folded — but
    # `deploy_locked` is not switch-owned, so it must survive untouched.
    assert final.status == "blocked" and final.reason_kind == "deploy_locked"
    assert final.deployed_sha == staged.sha and final.deployed_slot == "b"
    assert (await repo.get_deployment(staged.id)).state == "live"
