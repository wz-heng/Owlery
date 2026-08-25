"""Migration test for the Task Board gaps rectification: ``tasks.verdict``
and ``cancelled`` becoming a first-class terminal status
(docs/plans/task-board-gaps.md §3.1, §3.4).

Boots a DB with the *new* shape, then downgrades only the ``tasks`` table
back to its pre-gaps shape (no ``verdict`` column, no ``'cancelled'`` in the
status CHECK) to simulate a pre-existing install, seeds the legacy
``status='blocked', blocked_kind='cancelled'`` shape alongside an ordinary
``done`` row and a genuinely-blocked row, runs ``_apply_migrations()``
(twice), and asserts the cancelled fold — idempotently, and without
disturbing anything else.
"""

import sqlite3

import pytest

from server.database import Database


def _downgrade_tasks_table(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        DROP TABLE tasks;
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            board_id TEXT NOT NULL,
            parent_task_id TEXT,
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL
                CHECK (status IN ('triage', 'todo', 'ready', 'running', 'blocked', 'done')),
            assignee_agent_id TEXT,
            priority INTEGER NOT NULL DEFAULT 0,
            origin_session_id TEXT,
            idempotency_key TEXT,
            scheduled_at TEXT,
            workspace_mode TEXT,
            working_dir_override TEXT,
            model TEXT,
            current_run_id TEXT,
            blocked_kind TEXT,
            blocked_reason TEXT,
            result_summary TEXT,
            archived INTEGER NOT NULL DEFAULT 0,
            created_by_kind TEXT NOT NULL,
            created_by_agent_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            archived_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO tasks (id, board_id, title, status, blocked_kind, "
        "blocked_reason, archived, created_by_kind, created_at, updated_at) "
        "VALUES ('legacy-cancel', 'b1', 'old cancelled task', 'blocked', "
        "'cancelled', 'no longer needed', 0, 'user', "
        "'2025-01-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO tasks (id, board_id, title, status, archived, "
        "created_by_kind, created_at, updated_at, completed_at) VALUES "
        "('legacy-done', 'b1', 'ordinary done task', 'done', 0, 'user', "
        "'2025-01-01T00:00:00+00:00', '2025-01-02T00:00:00+00:00', "
        "'2025-01-02T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO tasks (id, board_id, title, status, blocked_kind, "
        "blocked_reason, archived, created_by_kind, created_at, updated_at) "
        "VALUES ('legacy-blocked', 'b1', 'genuinely blocked task', "
        "'blocked', 'input', 'need a decision', 0, 'user', "
        "'2025-01-01T00:00:00+00:00', '2025-01-02T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_legacy_cancelled_blocked_rows_fold_into_cancelled_status(tmp_path):
    db_path = str(tmp_path / "gaps.db")
    db = Database(db_path)
    await db.initialize()
    await db.conn.execute(
        "INSERT INTO task_boards "
        "(id, name, working_dir, default_workspace_mode, created_at, updated_at) "
        "VALUES ('b1', 'B', '/tmp', 'shared', "
        "'2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00')"
    )
    await db.conn.commit()
    await db.close()

    _downgrade_tasks_table(db_path)

    db2 = Database(db_path)
    await db2.initialize()  # runs _apply_migrations, including the fold
    try:
        cur = await db2.conn.execute(
            "SELECT status, blocked_kind, blocked_reason, verdict, "
            "completed_at, updated_at FROM tasks WHERE id = 'legacy-cancel'"
        )
        status, blocked_kind, blocked_reason, verdict, completed_at, updated_at = (
            await cur.fetchone()
        )
        assert status == "cancelled"
        assert blocked_kind is None
        assert blocked_reason == "no longer needed"  # reason text preserved
        assert verdict is None  # nothing to migrate; stays NULL
        assert completed_at == updated_at  # backfilled from updated_at

        # An ordinary done task is untouched.
        cur = await db2.conn.execute(
            "SELECT status, blocked_kind, completed_at FROM tasks "
            "WHERE id = 'legacy-done'"
        )
        assert await cur.fetchone() == (
            "done", None, "2025-01-02T00:00:00+00:00",
        )

        # A genuinely-blocked (non-cancelled) task is untouched.
        cur = await db2.conn.execute(
            "SELECT status, blocked_kind FROM tasks WHERE id = 'legacy-blocked'"
        )
        assert await cur.fetchone() == ("blocked", "input")

        # The widened CHECK actually admits 'cancelled' now (a raw INSERT
        # that predates this migration would have rejected it).
        await db2.conn.execute(
            "INSERT INTO tasks (id, board_id, title, status, archived, "
            "created_by_kind, created_at, updated_at) VALUES "
            "('fresh-cancel', 'b1', 'new cancel', 'cancelled', 0, 'user', "
            "'2025-06-01T00:00:00+00:00', '2025-06-01T00:00:00+00:00')"
        )
        await db2.conn.commit()

        # Idempotency: guarded on the verdict column's presence, a second
        # pass changes nothing.
        await db2._apply_migrations()
        cur = await db2.conn.execute(
            "SELECT status, blocked_kind FROM tasks WHERE id = 'legacy-cancel'"
        )
        assert await cur.fetchone() == ("cancelled", None)
    finally:
        await db2.close()
