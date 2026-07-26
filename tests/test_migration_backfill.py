"""Migration/backfill tests for the first-class Agents refactor.

Boots a DB created with the *old* (pre-agents) schema — sessions without
agent_id, schedules with a NOT NULL session_id + FK, bridge_mappings with a
NOT NULL session_id — runs `_apply_migrations()` (twice), and asserts the
ownership graph is rebuilt onto a Default Agent, idempotently. See
docs/plans/agent-refactor.md §4.5.
"""

import json
import sqlite3

import pytest

from server.database import Database

# The schema as it existed before the Agents refactor, for the three tables
# the migration transforms. Everything else is created fresh by Database.
_OLD_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    working_dir TEXT NOT NULL,
    created_at TEXT NOT NULL,
    claude_session_id TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    credential_id TEXT
);
CREATE TABLE schedules (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE TABLE bridge_mappings (
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    PRIMARY KEY (platform, chat_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
"""


def _seed_old_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO sessions (id, name, working_dir, created_at) "
        "VALUES ('s1', 'Old Session', '/tmp', '2025-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO schedules (id, session_id, name, prompt, interval_seconds, created_at) "
        "VALUES ('sch1', 's1', 'daily', 'do it', 3600, '2025-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO bridge_mappings (platform, chat_id, session_id) "
        "VALUES ('telegram', 'c1', 's1')"
    )
    conn.commit()
    conn.close()


async def _column_names(db: Database, table: str) -> set[str]:
    cursor = await db.conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


@pytest.mark.asyncio
async def test_backfill_from_old_schema(tmp_path):
    db_path = str(tmp_path / "old.db")
    _seed_old_db(db_path)

    db = Database(db_path)
    await db.initialize()  # runs _apply_migrations once
    try:
        # A brand-new agents table (the old schema had none) seeds exactly
        # one ordinary starter agent named 'Owl' (agent-identity.md).
        agents = await db.load_agents()
        owls = [a for a in agents if a["name"] == "Owl"]
        assert len(owls) == 1
        default = owls[0]
        # The built-in backfill (agent-collaboration.md §5.1 ask_agent;
        # native-deep-research.md §7 research) runs alongside the other
        # migrations and appends to every existing agent's mcp_servers list.
        assert default["mcp_servers"] == ["ask", "bg", "ask_agent", "research", "tasks"]

        # Session backfilled onto it, origin defaults to 'user', backend to
        # claude-code (codex-backend.md §4.1 migration).
        sessions = await db.load_sessions()
        assert len(sessions) == 1
        assert sessions[0]["agent_id"] == default["id"]
        assert sessions[0]["origin"] == "user"
        assert sessions[0]["backend"] == "claude-code"

        # Schedule re-owned by the agent; session_id column gone.
        schedules = await db.load_schedules()
        assert len(schedules) == 1
        assert schedules[0]["agent_id"] == default["id"]
        assert "session_id" not in await _column_names(db, "schedules")

        # Bridge mapping bound to the agent; session_id preserved as the
        # sticky pointer and is now nullable.
        mappings = await db.load_bridge_mappings()
        assert len(mappings) == 1
        assert mappings[0]["agent_id"] == default["id"]
        assert mappings[0]["session_id"] == "s1"
        assert not await db._column_is_not_null("bridge_mappings", "session_id")

        # Idempotency: a second migration run changes nothing.
        await db._apply_migrations()
        agents2 = await db.load_agents()
        assert len([a for a in agents2 if a["name"] == "Owl"]) == 1
        assert (await db.get_agent(default["id"]))["id"] == default["id"]
        assert len(await db.load_schedules()) == 1
        assert len(await db.load_bridge_mappings()) == 1
        sessions2 = await db.load_sessions()
        assert all(s["agent_id"] == default["id"] for s in sessions2)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_builtin_mcp_backfill_runs_once_and_respects_removals(tmp_path):
    """The built-in MCP backfill enrols a pre-existing agent in newly-shipped
    servers exactly once. After that a user's deliberate removal of a built-in
    (now possible from the settings UI) survives a restart: the per-boot re-add
    that would overturn it is gated by PRAGMA user_version."""
    db = Database(str(tmp_path / "once.db"))
    await db.initialize()
    try:
        aid = (await db.get_default_agent())["id"]
        # Rewind to a pre-backfill world: the agent stored the narrow legacy
        # set and the version marker is unset, exactly as on an instance that
        # predates ask_agent/research.
        await db.conn.execute(
            "UPDATE agents SET mcp_servers = ? WHERE id = ?",
            (json.dumps(["ask", "bg"]), aid),
        )
        await db.conn.execute("PRAGMA user_version = 0")
        await db.conn.commit()

        # The first upgrade run backfills the shipped built-ins.
        await db._apply_migrations()
        assert set((await db.get_agent(aid))["mcp_servers"]) == {
            "ask",
            "bg",
            "ask_agent",
            "research",
            "tasks",
        }

        # The user deselects delegation in the settings UI and saves.
        await db.conn.execute(
            "UPDATE agents SET mcp_servers = ? WHERE id = ?",
            (json.dumps(["ask", "bg", "research"]), aid),
        )
        await db.conn.commit()

        # A later restart must NOT resurrect the removed server.
        await db._apply_migrations()
        assert "ask_agent" not in (await db.get_agent(aid))["mcp_servers"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_fresh_db_gets_default_agent(tmp_path):
    """A brand-new DB is born with the new shape and one seeded 'Owl'."""
    db = Database(str(tmp_path / "fresh.db"))
    await db.initialize()
    try:
        system = await db.get_default_agent()
        assert system is not None
        assert system["name"] == "Owl"
        # The dropped columns are gone from the fresh table.
        assert "is_system" not in await _column_names(db, "agents")
        assert "avatar" not in await _column_names(db, "agents")
        # No session_id leftover on the freshly-created tables.
        assert "session_id" not in await _column_names(db, "schedules")
        assert not await db._column_is_not_null("bridge_mappings", "session_id")

        # Second run no-ops (still exactly one seeded agent).
        await db._apply_migrations()
        agents = await db.load_agents(include_archived=True)
        assert len([a for a in agents if a["name"] == "Owl"]) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_orphan_schedule_falls_back_to_default(tmp_path):
    """A schedule whose session was deleted still lands on the Default Agent."""
    db_path = str(tmp_path / "orphan.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(_OLD_SCHEMA)
    # Schedule references a session id that doesn't exist (FK enforcement is
    # off by default in this raw connection, so the orphan persists).
    conn.execute(
        "INSERT INTO schedules (id, session_id, name, prompt, interval_seconds, created_at) "
        "VALUES ('sch1', 'gone', 'daily', 'do it', 3600, '2025-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    db = Database(db_path)
    await db.initialize()
    try:
        default = await db.get_default_agent()
        schedules = await db.load_schedules()
        assert len(schedules) == 1
        assert schedules[0]["agent_id"] == default["id"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_legacy_db_drops_columns_and_does_not_reseed(tmp_path):
    """An existing instance (legacy is_system/avatar shape, its own 'Octo')
    keeps its agents untouched, has both retired columns dropped, is NOT
    reseeded with 'Owl', and its 'Octo' is now an ordinary archivable agent
    (agent-identity.md §6)."""
    from server.agent_manager import AgentManager

    db = Database(str(tmp_path / "legacy.db"))
    await db.initialize()
    try:
        # Reconstruct a legacy population: re-add the retired columns and a
        # user's own 'Octo' (the protected default of the old world), and
        # remove the just-seeded Owl so the population is exactly [Octo].
        await db.conn.execute(
            "ALTER TABLE agents ADD COLUMN is_system INTEGER NOT NULL DEFAULT 0"
        )
        await db.conn.execute("ALTER TABLE agents ADD COLUMN avatar TEXT")
        await db.conn.execute("DELETE FROM agents")
        await db.conn.execute(
            "INSERT INTO agents (id, name, created_at, updated_at, is_system, avatar) "
            "VALUES ('octo01', 'Octo', '2025-01-01T00:00:00+00:00', "
            "'2025-01-01T00:00:00+00:00', 1, '🐙')"
        )
        await db.conn.commit()

        # Re-run migrations: columns dropped, no reseed.
        await db._apply_migrations()
        cols = await _column_names(db, "agents")
        assert "is_system" not in cols
        assert "avatar" not in cols
        agents = await db.load_agents(include_archived=True)
        assert [a["name"] for a in agents] == ["Octo"]  # no 'Owl' seeded

        # The legacy 'Octo' is archivable now — no protection guard.
        mgr = AgentManager(db)
        await mgr.archive_agent("octo01")  # must not raise
        assert (await db.get_agent("octo01"))["archived"] is True

        # Re-running migrations after archiving the only agent must NOT
        # reseed 'Owl': the table is non-empty (an archived row still counts),
        # so a restart never resurrects a default (agent-identity.md §6).
        await db._apply_migrations()
        names = [a["name"] for a in await db.load_agents(include_archived=True)]
        assert names == ["Octo"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_backfill_targets_oldest_live_not_archived(tmp_path):
    """A populated table with a stray NULL-owner row backfills onto the oldest
    LIVE agent — matching runtime get_default_agent — never an older archived
    one, and it does not reseed 'Owl' (agent-identity.md)."""
    db = Database(str(tmp_path / "mixed.db"))
    await db.initialize()
    try:
        # An OLDER archived agent and a NEWER live agent (the seed is removed
        # so the population is exactly these two).
        await db.conn.execute("DELETE FROM agents")
        await db.conn.execute(
            "INSERT INTO agents (id, name, archived, created_at, updated_at) "
            "VALUES ('old1', 'Archived', 1, '2025-01-01T00:00:00+00:00', "
            "'2025-01-01T00:00:00+00:00')"
        )
        await db.conn.execute(
            "INSERT INTO agents (id, name, archived, created_at, updated_at) "
            "VALUES ('new1', 'Live', 0, '2025-06-01T00:00:00+00:00', "
            "'2025-06-01T00:00:00+00:00')"
        )
        # A stray session with no owner.
        await db.conn.execute(
            "INSERT INTO sessions (id, name, working_dir, created_at) "
            "VALUES ('s1', 'orphan', '/tmp', '2025-01-01T00:00:00+00:00')"
        )
        await db.conn.commit()

        await db._apply_migrations()

        # No reseed — still exactly the two agents.
        names = sorted(
            a["name"] for a in await db.load_agents(include_archived=True)
        )
        assert names == ["Archived", "Live"]
        # The orphan lands on the oldest LIVE agent, not the older archived one.
        sessions = await db.load_sessions()
        assert next(s for s in sessions if s["id"] == "s1")["agent_id"] == "new1"
        # And that is exactly what the runtime default resolves to.
        assert (await db.get_default_agent())["id"] == "new1"
    finally:
        await db.close()
