"""Attempt-replay assembly (docs/plans/attempt-replay.md): the write-side
DB additions (messages.created_at, turn_usage.message_seq, harness_exits),
heartbeat downsampling, and the read-side replay assembly + REST endpoints.

The turn-termination invariant itself (harness_exits always written, on
every exit path including a killed subprocess) is exercised end-to-end in
tests/test_session_manager.py, since it needs the real turn loop. This file
covers the DB layer and the read-side assembly/API in isolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server.database import Database
from server.main import app
from server.replay import assemble_session_replay
from server.session_manager import session_manager
from server.task_board.repository import TaskRepository

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
    try:
        yield d
    finally:
        await d.close()


async def _session(db: Database, session_id: str = "s1", **overrides):
    row = {
        "name": "S",
        "working_dir": "/tmp",
        "created_at": "2026-08-01T00:00:00+00:00",
    }
    row.update(overrides)
    await db.save_session(session_id, **row)


# --------------------------------------------------------------------------- #
# DB layer
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_append_message_stamps_created_at(db):
    await _session(db)
    await db.append_message(session_id="s1", seq=0, role="user", type="text", content="hi")
    rows = await db.load_messages("s1")
    assert rows[0]["created_at"]  # stamped, not NULL


@pytest.mark.asyncio
async def test_add_harness_exit_roundtrip(db):
    await _session(db)
    await db.add_harness_exit(
        session_id="s1",
        reason="process_error",
        created_at="2026-08-01T00:05:00+00:00",
        message_seq=3,
        exit_code=None,
        signal=9,
        escalation=None,
        reason_detail={},
        stderr_tail="boom",
    )
    rows = await db.list_harness_exits_for_session("s1")
    assert len(rows) == 1
    assert rows[0] == {
        "message_seq": 3,
        "reason": "process_error",
        "exit_code": None,
        "signal": 9,
        "escalation": None,
        "reason_detail": {},
        "stderr_tail": "boom",
        "created_at": "2026-08-01T00:05:00+00:00",
    }


@pytest.mark.asyncio
async def test_turn_usage_message_seq_roundtrip(db):
    await _session(db)
    await db.add_turn_usage(
        created_at="2026-08-01T00:05:00+00:00",
        session_id="s1",
        backend="claude-code",
        message_seq=7,
    )
    rows = await db.list_turn_usage_for_session("s1")
    assert len(rows) == 1
    assert rows[0]["message_seq"] == 7


# --------------------------------------------------------------------------- #
# Heartbeat downsampling (attempt-replay.md §3.1 point 3)
# --------------------------------------------------------------------------- #


def test_should_emit_heartbeat_event_throttles(monkeypatch):
    from server.task_board.manager import TaskBoardManager

    clock = {"t": 0.0}
    monkeypatch.setattr("server.task_board.manager.time.monotonic", lambda: clock["t"])

    mgr = TaskBoardManager()
    assert mgr._should_emit_heartbeat_event("run-1") is True  # first call always emits
    clock["t"] += 1.0
    assert mgr._should_emit_heartbeat_event("run-1") is False  # inside the interval
    clock["t"] += TaskBoardManager._HEARTBEAT_EVENT_INTERVAL_SECONDS
    assert mgr._should_emit_heartbeat_event("run-1") is True  # interval elapsed
    # A different run_id has its own independent clock.
    assert mgr._should_emit_heartbeat_event("run-2") is True


# --------------------------------------------------------------------------- #
# Replay assembly
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_assemble_session_replay_missing_session_returns_none(db):
    assert await assemble_session_replay(db, "nope") is None


@pytest.mark.asyncio
async def test_assemble_session_replay_orders_events_and_detects_gaps(db):
    await _session(db)
    await db.append_message(session_id="s1", seq=0, role="user", type="text", content="go")
    # append_message stamps "now" — overwrite with controlled timestamps so
    # gap detection is deterministic.
    await db.conn.execute(
        "UPDATE messages SET created_at = ? WHERE session_id = 's1' AND seq = 0",
        ("2026-08-01T00:00:00+00:00",),
    )
    await db.append_message(session_id="s1", seq=1, role="assistant", type="text", content="ok")
    await db.conn.execute(
        "UPDATE messages SET created_at = ? WHERE session_id = 's1' AND seq = 1",
        ("2026-08-01T00:00:05+00:00",),
    )
    await db.conn.commit()
    # The terminal record lands 20 minutes later — a clear gap at the
    # default-ish threshold used below.
    await db.add_harness_exit(
        session_id="s1",
        reason="completed",
        created_at="2026-08-01T00:20:00+00:00",
        message_seq=1,
    )

    replay = await assemble_session_replay(db, "s1", gap_threshold_seconds=60)
    assert replay is not None
    kinds = [e["kind"] for e in replay["timeline"]]
    assert kinds == ["message", "message", "gap", "turn_terminal"]
    gap = replay["timeline"][2]
    assert gap["detail"]["duration_seconds"] == pytest.approx(20 * 60 - 5, abs=1)
    assert gap["detail"]["before"]["kind"] == "message"
    assert gap["detail"]["after"]["kind"] == "turn_terminal"
    assert replay["unobserved_prefix"] is None


@pytest.mark.asyncio
async def test_assemble_session_replay_flags_unobserved_prefix(db):
    await _session(db)
    # Simulate a legacy row predating the created_at migration.
    await db.conn.execute(
        "INSERT INTO messages (session_id, seq, role, type, content, created_at) "
        "VALUES ('s1', 0, 'user', 'text', '\"legacy\"', NULL)"
    )
    await db.conn.commit()
    await db.append_message(session_id="s1", seq=1, role="assistant", type="text", content="new")

    replay = await assemble_session_replay(db, "s1")
    assert replay["unobserved_prefix"] is not None
    assert len(replay["unobserved_prefix"]["events"]) == 1
    assert replay["unobserved_prefix"]["events"][0]["seq"] == 0
    # The timestamped row still appears in the main timeline.
    assert [e["seq"] for e in replay["timeline"]] == [1]


@pytest.mark.asyncio
async def test_assemble_session_replay_includes_task_run_and_delegation(tmp_path: Path, monkeypatch):
    import server.task_board.repository as repo_mod

    db_path = tmp_path / "owlery.db"
    db = Database(str(db_path))
    await db.initialize()
    repo = TaskRepository(str(db_path))
    await repo.initialize()
    # `server/replay.py` looks up the process-global `task_repository`
    # singleton by name inside `assemble_session_replay` — point that name at
    # this test's own instance (bound to the same file as `db`) for the
    # duration of the test, rather than touching the real singleton.
    monkeypatch.setattr(repo_mod, "task_repository", repo)
    try:
        agent_id = (await db.load_agents())[0]["id"]
        board = await repo.create_board(name="B", working_dir=str(tmp_path))
        task = await repo.create_task(
            board_id=board.id, title="T", status="todo", assignee_agent_id=agent_id
        )
        run = await repo.claim_ready(
            task.id, workspace_mode="copy", workspace_path=str(tmp_path / "run")
        )
        await _session(db, session_id="worker", agent_id=agent_id, origin="task")
        await repo.attach_run_session(task.id, run.id, "worker")
        await repo.heartbeat_run(
            task.id, run.id, lease_expires_at="2026-08-01T01:00:00+00:00", emit_event=True
        )

        # A child delegation spawned from this worker session.
        await _session(
            db, session_id="child", agent_id=agent_id, origin="delegation",
            parent_session_id="worker",
        )
        await db.create_delegation_run(
            run_id="drun1",
            delegation_id="child",
            round_no=1,
            request="do the subtask",
            start_seq=0,
            created_at="2026-08-01T00:01:00+00:00",
            state="completed",
            finished_at="2026-08-01T00:02:00+00:00",
        )

        replay = await assemble_session_replay(db, "worker")
        assert replay["task_run"] == {"task_id": task.id, "run_id": run.id}
        kinds = {e["kind"] for e in replay["timeline"]}
        assert "task_event" in kinds
        assert "delegation" in kinds
        delegation_event = next(e for e in replay["timeline"] if e["kind"] == "delegation")
        assert delegation_event["detail"]["delegation_id"] == "child"
        assert delegation_event["detail"]["request"] == "do the subtask"
    finally:
        await repo.close()
        await db.close()


@pytest.mark.asyncio
async def test_assemble_session_replay_no_task_board_binding_is_not_an_error(db):
    """Most sessions (interactive chat) have no Task Board involvement, and
    the global `task_repository` singleton may never be initialized in a
    plain-chat deployment — that must not break replay for ordinary
    sessions."""
    from server.task_board.repository import task_repository

    assert task_repository.is_initialized is False
    await _session(db)
    await db.append_message(session_id="s1", seq=0, role="user", type="text", content="hi")
    replay = await assemble_session_replay(db, "s1")
    assert replay is not None
    assert replay["task_run"] is None


# --------------------------------------------------------------------------- #
# REST endpoints
# --------------------------------------------------------------------------- #


@pytest.fixture
async def client(db):
    session_manager.sessions.clear()
    await session_manager.initialize(db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_get_session_replay_route(client, db):
    await _session(db)
    await db.append_message(session_id="s1", seq=0, role="user", type="text", content="hi")

    resp = await client.get("/api/sessions/s1/replay", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "s1"
    assert len(body["timeline"]) == 1


@pytest.mark.asyncio
async def test_get_session_replay_route_404_for_missing_session(client):
    resp = await client.get("/api/sessions/nope/replay", headers=HEADERS)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_run_replay_route_404_without_worker_session(client, tmp_path: Path):
    db_path = tmp_path / "owlery.db"
    # A separate, fully-schema'd DB file (Database owns schema creation; a
    # bare TaskRepository doesn't) bound to its own TaskRepository — this
    # test only needs the route's own not-yet-started-worker 404 path, not
    # the httpx client's in-memory app db.
    from server.routers import task_boards as task_boards_mod

    side_db = Database(str(db_path))
    await side_db.initialize()
    repo = TaskRepository(str(db_path))
    await repo.initialize()
    monkeypatch_target = task_boards_mod.task_repository
    try:
        agent_id = (await side_db.load_agents())[0]["id"]
        board = await repo.create_board(name="B", working_dir=str(tmp_path))
        task = await repo.create_task(
            board_id=board.id, title="T", status="todo", assignee_agent_id=agent_id
        )
        run = await repo.claim_ready(
            task.id, workspace_mode="copy", workspace_path=str(tmp_path / "run")
        )
        task_boards_mod.task_repository = repo
        resp = await client.get(f"/api/tasks/{task.id}/runs/{run.id}/replay", headers=HEADERS)
        assert resp.status_code == 404
    finally:
        task_boards_mod.task_repository = monkeypatch_target
        await repo.close()
        await side_db.close()
