"""Crash-safe system-to-session delivery.

The contract under test is deliberately narrower than a job queue:
`session_injections` only guarantees that one producer event becomes at most
one durable user-message row.  It does not retry the model turn that consumes
that message.
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from server.database import Database
from server.session_manager import QueuedPrompt, SessionManager


@pytest.fixture
async def db():
    value = Database(":memory:")
    await value.initialize()
    yield value
    await value.close()


async def _session(manager: SessionManager):
    assert manager.db is not None
    agent = await manager.db.get_default_agent()
    return await manager.create_session(agent["id"], "target", "/tmp")


@pytest.mark.asyncio
async def test_pending_injection_replays_after_restart(db, monkeypatch):
    first = SessionManager()
    await first.initialize(db)
    target = await _session(first)
    accepted: list[tuple[str, str, str | None]] = []

    async def accept(sid, prompt, attachment_ids=None, injection_id=None):
        accepted.append((sid, prompt, injection_id))

    monkeypatch.setattr(first, "start_message", accept)
    row = await first.enqueue_session_injection(
        source_key="bg:t1", session_id=target.id, prompt="result"
    )
    assert row["status"] == "pending"
    assert accepted == [(target.id, "result", row["id"])]

    # The first process accepted only into memory and then vanished.  A fresh
    # manager has no dispatch guard and must replay the still-pending row.
    second = SessionManager()
    await second.initialize(db)
    replayed: list[str | None] = []

    async def replay(sid, prompt, attachment_ids=None, injection_id=None):
        replayed.append(injection_id)

    monkeypatch.setattr(second, "start_message", replay)
    assert await second.recover_pending_session_injections() == 1
    assert replayed == [row["id"]]


@pytest.mark.asyncio
async def test_paused_dispatch_persists_then_central_resume_drains(
    db, monkeypatch
):
    manager = SessionManager()
    await manager.initialize(db)
    target = await _session(manager)
    started: list[str | None] = []

    async def capture(sid, prompt, attachment_ids=None, injection_id=None):
        started.append(injection_id)

    monkeypatch.setattr(manager, "start_message", capture)
    manager.pause_session_injection_dispatch()
    row = await manager.enqueue_session_injection(
        source_key="bg:boot",
        session_id=target.id,
        prompt="recovered result",
    )
    assert row["status"] == "pending"
    assert started == []

    assert await manager.resume_session_injection_dispatch() == 1
    assert started == [row["id"]]


@pytest.mark.asyncio
async def test_message_insert_and_delivery_ack_commit_together(db):
    manager = SessionManager()
    await manager.initialize(db)
    target = await _session(manager)
    row = await db.create_session_injection(
        injection_id="inj1",
        source_key="delegation:r1:terminal",
        session_id=target.id,
        prompt="reply",
        created_at="2026-07-24T00:00:00Z",
    )
    await db.append_message(
        target.id,
        seq=0,
        role="user",
        type="text",
        content="reply",
        injection_id=row["id"],
    )
    final = await db.get_session_injection(row["id"])
    assert final and final["status"] == "delivered"
    assert final["delivered_at"]
    assert await db.count_messages(target.id) == 1


@pytest.mark.asyncio
async def test_message_insert_statement_itself_acknowledges_outbox(db):
    """No second Python execute() may be required for the delivery receipt.

    Database methods share one connection, so an unrelated coroutine can
    commit after any statement. The SQLite trigger must already have changed
    the outbox row when the raw INSERT statement returns.
    """
    manager = SessionManager()
    await manager.initialize(db)
    target = await _session(manager)
    await db.create_session_injection(
        injection_id="inj-trigger",
        source_key="bg:trigger",
        session_id=target.id,
        prompt="result",
        created_at="2026-07-24T00:00:00Z",
    )
    await db._conn.execute(
        "INSERT INTO messages "
        "(session_id, seq, role, type, content, injection_id) "
        "VALUES (?, 0, 'user', 'text', ?, ?)",
        (target.id, '"result"', "inj-trigger"),
    )
    row = await db.get_session_injection("inj-trigger")
    assert row and row["status"] == "delivered"


@pytest.mark.asyncio
async def test_boot_reconciles_legacy_pending_row_with_existing_message(
    db, monkeypatch
):
    manager = SessionManager()
    await manager.initialize(db)
    target = await _session(manager)
    await db.create_session_injection(
        injection_id="inj-legacy",
        source_key="bg:legacy",
        session_id=target.id,
        prompt="old result",
        created_at="2026-07-24T00:00:00Z",
    )

    # Reproduce the pre-trigger crash window: the transcript row committed,
    # but the outbox acknowledgement did not.
    await db._conn.execute("DROP TRIGGER messages_ack_injection")
    await db._conn.execute(
        "INSERT INTO messages "
        "(session_id, seq, role, type, content, injection_id) "
        "VALUES (?, 0, 'user', 'text', ?, ?)",
        (target.id, '"old result"', "inj-legacy"),
    )
    await db._conn.commit()
    before = await db.get_session_injection("inj-legacy")
    assert before and before["status"] == "pending"

    async def forbidden(*args, **kwargs):
        raise AssertionError("an existing transcript row must not be replayed")

    monkeypatch.setattr(manager, "start_message", forbidden)
    assert await manager.recover_pending_session_injections() == 1
    after = await db.get_session_injection("inj-legacy")
    assert after and after["status"] == "delivered"


@pytest.mark.asyncio
async def test_wrong_target_rolls_back_message_and_ack(db):
    manager = SessionManager()
    await manager.initialize(db)
    first = await _session(manager)
    second = await _session(manager)
    await db.create_session_injection(
        injection_id="inj1",
        source_key="bg:t1",
        session_id=first.id,
        prompt="reply",
        created_at="2026-07-24T00:00:00Z",
    )
    with pytest.raises(ValueError, match="another session"):
        await db.append_message(
            second.id,
            seq=0,
            role="user",
            type="text",
            content="reply",
            injection_id="inj1",
        )
    assert await db.count_messages(second.id) == 0
    row = await db.get_session_injection("inj1")
    assert row and row["status"] == "pending"


@pytest.mark.asyncio
async def test_wrong_payload_cannot_ack_injection(db):
    manager = SessionManager()
    await manager.initialize(db)
    target = await _session(manager)
    await db.create_session_injection(
        injection_id="inj1",
        source_key="bg:t1",
        session_id=target.id,
        prompt="expected payload",
        created_at="2026-07-24T00:00:00Z",
    )
    with pytest.raises(ValueError, match="different payload"):
        await db.append_message(
            target.id,
            seq=0,
            role="user",
            type="text",
            content="different payload",
            injection_id="inj1",
        )
    assert await db.count_messages(target.id) == 0
    row = await db.get_session_injection("inj1")
    assert row and row["status"] == "pending"


@pytest.mark.asyncio
async def test_unique_injection_id_blocks_duplicate_transcript_row(db):
    manager = SessionManager()
    await manager.initialize(db)
    target = await _session(manager)
    await db.create_session_injection(
        injection_id="inj1",
        source_key="research:j1",
        session_id=target.id,
        prompt="report",
        created_at="2026-07-24T00:00:00Z",
    )
    await db.append_message(
        target.id, 0, "user", "text", "report", injection_id="inj1"
    )
    with pytest.raises(ValueError, match="no longer pending"):
        await db.append_message(
            target.id, 1, "user", "text", "report", injection_id="inj1"
        )
    # The database layer independently enforces the invariant below Python.
    with pytest.raises(aiosqlite.IntegrityError):
        await db._conn.execute(
            "INSERT INTO messages "
            "(session_id, seq, role, type, content, injection_id) "
            "VALUES (?, 1, 'user', 'text', ?, ?)",
            (target.id, '"report"', "inj1"),
        )
    assert await db.count_messages(target.id) == 1


@pytest.mark.asyncio
async def test_source_key_reuse_with_changed_payload_is_rejected(db, monkeypatch):
    manager = SessionManager()
    await manager.initialize(db)
    target = await _session(manager)

    async def accept(*args, **kwargs):
        return None

    monkeypatch.setattr(manager, "start_message", accept)
    await manager.enqueue_session_injection(
        source_key="bg:t1", session_id=target.id, prompt="first"
    )
    with pytest.raises(ValueError, match="different target or payload"):
        await manager.enqueue_session_injection(
            source_key="bg:t1", session_id=target.id, prompt="changed"
        )


@pytest.mark.asyncio
async def test_delivered_injection_is_not_replayed(db, monkeypatch):
    manager = SessionManager()
    await manager.initialize(db)
    target = await _session(manager)
    await db.create_session_injection(
        injection_id="inj1",
        source_key="bg:t1",
        session_id=target.id,
        prompt="result",
        created_at="2026-07-24T00:00:00Z",
    )
    await db.append_message(
        target.id, 0, "user", "text", "result", injection_id="inj1"
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("delivered injection must not be scheduled again")

    monkeypatch.setattr(manager, "start_message", forbidden)
    assert await manager.recover_pending_session_injections() == 0


@pytest.mark.asyncio
async def test_reset_retries_injection_dropped_from_memory_queue(db, monkeypatch):
    manager = SessionManager()
    await manager.initialize(db)
    target = await _session(manager)
    row = await db.create_session_injection(
        injection_id="inj-reset",
        source_key="bg:reset",
        session_id=target.id,
        prompt="result",
        created_at="2026-07-24T00:00:00Z",
    )
    target._pending_queue.append(
        QueuedPrompt(prompt="result", attachment_ids=[], injection_id=row["id"])
    )
    manager._dispatched_injection_ids.add(row["id"])
    replayed: list[str | None] = []

    async def capture(sid, prompt, attachment_ids=None, injection_id=None):
        replayed.append(injection_id)

    monkeypatch.setattr(manager, "start_message", capture)
    await manager.reset_session(target.id)
    await asyncio.sleep(0.2)
    assert replayed == [row["id"]]
    await manager.shutdown_session_injections()
