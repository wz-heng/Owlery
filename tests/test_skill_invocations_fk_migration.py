"""Migration test for skill_invocations.session_id gaining a real FK to
sessions (T-B review round 2 §should, experience-consolidation-v2.md §3⑤).

Boots a DB with the *new* shape, then downgrades only the
``skill_invocations`` table back to its pre-migration shape (bare TEXT
``session_id``, no ``REFERENCES sessions``) to simulate a pre-existing
install, seeds a row pointing at a real session alongside one pointing at a
session that no longer exists, runs ``_apply_migrations()`` via a fresh
``Database.initialize()``, and asserts the rebuild both nulls the dangling
reference and leaves the table genuinely FK-enforced going forward.
"""

import sqlite3

import pytest

from server.database import Database


def _downgrade_skill_invocations_table(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        DROP TABLE skill_invocations;
        CREATE TABLE skill_invocations (
            id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL REFERENCES skill_candidates(id) ON DELETE CASCADE,
            agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
            repository TEXT,
            session_id TEXT,
            task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
            run_id TEXT REFERENCES task_runs(id) ON DELETE SET NULL,
            backend TEXT,
            used_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_skill_invocations_session_fk_migration_nulls_dangling_session(tmp_path):
    db_path = str(tmp_path / "skills.db")
    db = Database(db_path)
    await db.initialize()

    agent = await db.get_default_agent()
    candidate_id = "cand-1"
    now = "2025-01-01T00:00:00+00:00"
    await db.conn.execute(
        "INSERT INTO skill_candidates "
        "(id, slug, title, description, body_markdown, repository, rationale, "
        "status, proposed_by_agent_id, created_at, updated_at) "
        "VALUES (?, 'hermes-pr-flow', 'T', 'D', 'body', '/repo', 'r', "
        "'approved', ?, ?, ?)",
        (candidate_id, agent["id"], now, now),
    )
    session_id = "session-real"
    await db.conn.execute(
        "INSERT INTO sessions (id, name, working_dir, created_at) "
        "VALUES (?, 'S', '/repo', ?)",
        (session_id, now),
    )
    await db.conn.commit()
    await db.close()

    _downgrade_skill_invocations_table(db_path)

    # Seed pre-migration rows directly, exploiting the downgraded table's
    # missing FK: one pointing at a real session, one at a session that no
    # longer exists — the only shape a bare TEXT column ever let happen.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO skill_invocations (id, candidate_id, session_id, used_at) "
        "VALUES ('inv-live', ?, ?, ?)",
        (candidate_id, session_id, now),
    )
    conn.execute(
        "INSERT INTO skill_invocations (id, candidate_id, session_id, used_at) "
        "VALUES ('inv-orphan', ?, 'session-deleted-long-ago', ?)",
        (candidate_id, now),
    )
    conn.commit()
    conn.close()

    db2 = Database(db_path)
    await db2.initialize()  # runs _migrate_skill_invocations_session_fk
    try:
        cur = await db2.conn.execute(
            "SELECT session_id FROM skill_invocations WHERE id = 'inv-live'"
        )
        assert (await cur.fetchone())[0] == session_id

        cur = await db2.conn.execute(
            "SELECT session_id FROM skill_invocations WHERE id = 'inv-orphan'"
        )
        assert (await cur.fetchone())[0] is None  # dangling ref nulled by the rebuild

        # The rebuilt table really carries the FK now: deleting the still-
        # referenced session fires ON DELETE SET NULL going forward, which a
        # bare TEXT column could never do.
        await db2.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await db2.conn.commit()
        cur = await db2.conn.execute(
            "SELECT session_id FROM skill_invocations WHERE id = 'inv-live'"
        )
        assert (await cur.fetchone())[0] is None

        # Idempotent: guarded on the live table's DDL already naming the FK,
        # a second pass changes nothing and doesn't error.
        await db2._apply_migrations()
        cur = await db2.conn.execute("SELECT COUNT(*) FROM skill_invocations")
        assert (await cur.fetchone())[0] == 2
    finally:
        await db2.close()


@pytest.mark.asyncio
async def test_fresh_db_skill_invocations_already_has_the_session_fk(tmp_path):
    """A brand-new DB gets the FK straight from ``_SCHEMA`` — no rebuild
    needed, and ON DELETE SET NULL works immediately."""
    db = Database(str(tmp_path / "fresh.db"))
    await db.initialize()
    try:
        agent = await db.get_default_agent()
        now = "2025-01-01T00:00:00+00:00"
        await db.conn.execute(
            "INSERT INTO skill_candidates "
            "(id, slug, title, description, body_markdown, repository, rationale, "
            "status, proposed_by_agent_id, created_at, updated_at) "
            "VALUES ('cand-1', 'hermes-pr-flow', 'T', 'D', 'body', '/repo', 'r', "
            "'approved', ?, ?, ?)",
            (agent["id"], now, now),
        )
        await db.conn.execute(
            "INSERT INTO sessions (id, name, working_dir, created_at) "
            "VALUES ('session-fresh', 'S', '/repo', ?)",
            (now,),
        )
        await db.conn.execute(
            "INSERT INTO skill_invocations (id, candidate_id, session_id, used_at) "
            "VALUES ('inv-1', 'cand-1', 'session-fresh', ?)",
            (now,),
        )
        await db.conn.commit()

        await db.conn.execute("DELETE FROM sessions WHERE id = 'session-fresh'")
        await db.conn.commit()
        cur = await db.conn.execute(
            "SELECT session_id FROM skill_invocations WHERE id = 'inv-1'"
        )
        assert (await cur.fetchone())[0] is None
    finally:
        await db.close()
