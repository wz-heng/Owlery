"""Octopus → Owlery first-boot migration (docs/plans/rename-owlery.md §3).

The contract under test: an existing install crosses over losslessly on the
first boot of the renamed build, the crossing is idempotent, and every absolute
path the DB stored into the old tree is rewritten to the new one.
"""

import json
import os

import pytest

from server.config import Settings
from server.database import Database
from server.legacy_rename import (
    MARKER_NAME,
    migrate_legacy_state,
    rewrite_legacy_paths,
)


def _settings(tmp_path, **overrides) -> Settings:
    """Settings pinned entirely inside tmp_path — never the developer's ~."""
    return Settings(
        home_dir=str(tmp_path / "new"),
        legacy_home_dir=str(tmp_path / "old"),
        db_path=str(tmp_path / "owlery.db"),
        **overrides,
    )


def _populate_old_home(tmp_path) -> None:
    """A pre-rename home with one file in each real subdirectory."""
    old = tmp_path / "old"
    for leaf in ("agents", "attachments", "codex", "fork", "large-prompts", "research"):
        (old / leaf).mkdir(parents=True)
        (old / leaf / "keep.txt").write_text(leaf, encoding="utf-8")
    (old / "agents" / "a1" / "memory").mkdir(parents=True)
    (old / "agents" / "a1" / "memory" / "MEMORY.md").write_text("# hi", encoding="utf-8")


# ------------------------------------------------------------------- step 1


def test_moves_home_dir_and_marks_it(tmp_path):
    _populate_old_home(tmp_path)
    s = _settings(tmp_path)

    assert migrate_legacy_state(s) is True

    new = tmp_path / "new"
    assert not (tmp_path / "old").exists()
    assert (new / MARKER_NAME).is_file()
    # Lossless: every file crossed over, contents intact.
    assert (new / "codex" / "keep.txt").read_text(encoding="utf-8") == "codex"
    assert (new / "agents" / "a1" / "memory" / "MEMORY.md").read_text(
        encoding="utf-8"
    ) == "# hi"


def test_moves_db_file_with_wal_and_shm(tmp_path):
    _populate_old_home(tmp_path)
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"octopus.db{suffix}").write_text(suffix or "main", encoding="utf-8")
    s = _settings(tmp_path)

    migrate_legacy_state(s)

    for suffix in ("", "-wal", "-shm"):
        assert not (tmp_path / f"octopus.db{suffix}").exists()
        assert (tmp_path / f"owlery.db{suffix}").read_text(encoding="utf-8") == (
            suffix or "main"
        )


def test_is_idempotent(tmp_path):
    _populate_old_home(tmp_path)
    s = _settings(tmp_path)

    assert migrate_legacy_state(s) is True
    before = sorted(p.name for p in (tmp_path / "new").iterdir())

    # Second boot: marker present, old home gone. No-op, still True so the
    # (idempotent) path rewrite stays licensed.
    assert migrate_legacy_state(s) is True
    assert sorted(p.name for p in (tmp_path / "new").iterdir()) == before


def test_idempotent_when_old_home_reappears(tmp_path):
    """A user restores a backup of ~/.octopus after migrating. The marker means
    we've already crossed over: don't clobber the live home."""
    _populate_old_home(tmp_path)
    s = _settings(tmp_path)
    migrate_legacy_state(s)
    (tmp_path / "new" / "codex" / "keep.txt").write_text("live", encoding="utf-8")

    _populate_old_home(tmp_path)  # old home is back
    assert migrate_legacy_state(s) is True

    assert (tmp_path / "new" / "codex" / "keep.txt").read_text(encoding="utf-8") == "live"
    assert (tmp_path / "old").is_dir()  # left alone for the user to delete


def test_refuses_when_both_homes_are_populated(tmp_path):
    """Ambiguous state: never guess which tree is live."""
    _populate_old_home(tmp_path)
    (tmp_path / "new").mkdir()
    (tmp_path / "new" / "mine.txt").write_text("live", encoding="utf-8")
    s = _settings(tmp_path)

    assert migrate_legacy_state(s) is False
    assert (tmp_path / "old").is_dir()
    assert not (tmp_path / "new" / MARKER_NAME).exists()
    assert (tmp_path / "new" / "mine.txt").read_text(encoding="utf-8") == "live"


def test_migrates_into_an_empty_new_home(tmp_path):
    """An empty ~/.owlery (e.g. `mkdir`'d by a tool) isn't live state."""
    _populate_old_home(tmp_path)
    (tmp_path / "new").mkdir()
    s = _settings(tmp_path)

    assert migrate_legacy_state(s) is True
    assert (tmp_path / "new" / MARKER_NAME).is_file()
    assert (tmp_path / "new" / "codex" / "keep.txt").is_file()


def test_no_op_on_a_fresh_install(tmp_path):
    """No old home, no marker — a fresh checkout must Just Work."""
    s = _settings(tmp_path)
    assert migrate_legacy_state(s) is False
    assert not (tmp_path / "new").exists()


def test_disabled_by_empty_legacy_home_dir(tmp_path):
    _populate_old_home(tmp_path)
    s = _settings(tmp_path)
    s.legacy_home_dir = ""

    assert migrate_legacy_state(s) is False
    assert (tmp_path / "old").is_dir()


def test_never_clobbers_an_existing_db(tmp_path):
    """A live owlery.db wins over a stale octopus.db."""
    _populate_old_home(tmp_path)
    (tmp_path / "octopus.db").write_text("stale", encoding="utf-8")
    (tmp_path / "owlery.db").write_text("live", encoding="utf-8")
    s = _settings(tmp_path)

    migrate_legacy_state(s)

    assert (tmp_path / "owlery.db").read_text(encoding="utf-8") == "live"
    assert (tmp_path / "octopus.db").read_text(encoding="utf-8") == "stale"


def test_in_memory_db_has_no_legacy_file(tmp_path):
    _populate_old_home(tmp_path)
    s = _settings(tmp_path)
    s.db_path = ":memory:"

    migrate_legacy_state(s)  # must not raise

    assert (tmp_path / "new" / MARKER_NAME).is_file()


# ------------------------------------------------------------------- step 2


@pytest.fixture
async def migrated(tmp_path):
    """A migrated home + an initialized DB, ready for the path rewrite."""
    _populate_old_home(tmp_path)
    s = _settings(tmp_path)
    migrate_legacy_state(s)
    db = Database(s.db_path)
    await db.initialize()
    yield s, db
    await db.close()


async def _agent_id(db) -> str:
    cursor = await db._conn.execute("SELECT id FROM agents LIMIT 1")
    return (await cursor.fetchone())[0]


async def _add_session(db, sid: str, working_dir: str, **cols) -> None:
    columns = {"id": sid, "name": sid, "working_dir": working_dir,
               "created_at": "2026-01-01T00:00:00+00:00",
               "agent_id": await _agent_id(db), **cols}
    names = ", ".join(columns)
    holes = ", ".join("?" * len(columns))
    await db._conn.execute(
        f"INSERT INTO sessions ({names}) VALUES ({holes})", tuple(columns.values())
    )
    await db._conn.commit()


async def _get(db, table, column, row_id):
    cursor = await db._conn.execute(
        f"SELECT {column} FROM {table} WHERE id = ?", (row_id,)
    )
    return (await cursor.fetchone())[0]


async def test_rewrites_fork_working_dir(migrated):
    s, db = migrated
    old, new = s.legacy_home_dir, s.home_dir
    await _add_session(db, "s1", f"{old}/fork/proj-abc")

    assert await rewrite_legacy_paths(db, s) == 1

    assert await _get(db, "sessions", "working_dir", "s1") == f"{new}/fork/proj-abc"


async def test_leaves_unrelated_working_dirs_alone(migrated):
    """A normal session points at the user's project, not the app home."""
    s, db = migrated
    await _add_session(db, "s1", "/Users/me/code/myproj")

    assert await rewrite_legacy_paths(db, s) == 0

    assert await _get(db, "sessions", "working_dir", "s1") == "/Users/me/code/myproj"


async def test_does_not_rewrite_a_sibling_prefix(migrated):
    """`/old-backup` merely starts with `/old` — it isn't under it."""
    s, db = migrated
    sibling = f"{s.legacy_home_dir}-backup/fork/x"
    await _add_session(db, "s1", sibling)

    assert await rewrite_legacy_paths(db, s) == 0

    assert await _get(db, "sessions", "working_dir", "s1") == sibling


async def test_rewrites_nested_json_paths(migrated):
    s, db = migrated
    old, new = s.legacy_home_dir, s.home_dir
    metadata = {
        "prefilled_prompt": "go",
        "side_effect_summary": {
            "agent_touched_paths": [f"{old}/fork/p-1/a.py", "/elsewhere/b.py"],
            "counts": {"total": 2},
        },
    }
    record = {"ran": True, "files": [f"{old}/fork/p-1/a.py"], "stash_ref": "stash@{0}"}
    await _add_session(
        db, "s1", f"{old}/fork/p-1",
        fork_metadata=json.dumps(metadata), fork_revert_record=json.dumps(record),
    )

    await rewrite_legacy_paths(db, s)

    meta = json.loads(await _get(db, "sessions", "fork_metadata", "s1"))
    assert meta["side_effect_summary"]["agent_touched_paths"] == [
        f"{new}/fork/p-1/a.py",
        "/elsewhere/b.py",
    ]
    assert meta["prefilled_prompt"] == "go"          # non-path strings untouched
    assert meta["side_effect_summary"]["counts"] == {"total": 2}  # non-strings survive

    rec = json.loads(await _get(db, "sessions", "fork_revert_record", "s1"))
    assert rec["files"] == [f"{new}/fork/p-1/a.py"]
    assert rec["stash_ref"] == "stash@{0}"


async def test_rewrites_bg_task_and_research_paths(migrated):
    s, db = migrated
    old, new = s.legacy_home_dir, s.home_dir
    await _add_session(db, "s1", f"{old}/fork/p")
    await db._conn.execute(
        "INSERT INTO bg_tasks (id, session_id, command, working_dir, status, "
        "started_at) VALUES ('b1', 's1', 'ls', ?, 'completed', 'now')",
        (f"{old}/fork/p",),
    )
    await db.create_research_job("r1", "s1", "q?", "now")
    await db.update_research_job("r1", report_path=f"{old}/research/r1.md")

    await rewrite_legacy_paths(db, s)

    assert await _get(db, "bg_tasks", "working_dir", "b1") == f"{new}/fork/p"
    assert await _get(db, "research_jobs", "report_path", "r1") == f"{new}/research/r1.md"


async def test_rewrite_is_idempotent(migrated):
    s, db = migrated
    await _add_session(db, "s1", f"{s.legacy_home_dir}/fork/p")

    assert await rewrite_legacy_paths(db, s) == 1
    assert await rewrite_legacy_paths(db, s) == 0  # nothing left to rewrite

    assert await _get(db, "sessions", "working_dir", "s1") == f"{s.home_dir}/fork/p"


async def test_rewrite_requires_the_marker(tmp_path):
    """Without a migration having happened, stored paths are left alone — the
    old home may still be the live one for another (unmigrated) install."""
    s = _settings(tmp_path)
    (tmp_path / "new").mkdir()
    db = Database(s.db_path)
    await db.initialize()
    try:
        await _add_session(db, "s1", f"{s.legacy_home_dir}/fork/p")
        assert await rewrite_legacy_paths(db, s) == 0
    finally:
        await db.close()


async def test_rewrite_tolerates_non_json_blob(migrated):
    """A malformed fork_metadata must not abort the whole rewrite."""
    s, db = migrated
    await _add_session(db, "s1", f"{s.legacy_home_dir}/fork/p",
                       fork_metadata="not json{")

    assert await rewrite_legacy_paths(db, s) == 1  # working_dir still rewritten

    assert await _get(db, "sessions", "fork_metadata", "s1") == "not json{"


async def test_rewrite_rekeys_claude_transcripts(migrated, tmp_path, monkeypatch):
    """Claude keys transcripts by a slug of the working dir. A fork whose dir
    moved must have its project directory re-keyed and each line's `cwd`
    rewritten, or it resumes into an empty session and loses its history."""
    s, db = migrated
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    from server.harness.claude_code import _claude_project_dir

    old_wd = f"{s.legacy_home_dir}/fork/p-1"
    new_wd = f"{s.home_dir}/fork/p-1"
    src = _claude_project_dir(old_wd)
    src.mkdir(parents=True)
    (src / "sess.jsonl").write_text(
        json.dumps({"type": "user", "cwd": old_wd, "sessionId": "sess"}) + "\n",
        encoding="utf-8",
    )
    await _add_session(db, "s1", old_wd)

    await rewrite_legacy_paths(db, s)

    assert not src.exists()
    dest = _claude_project_dir(new_wd) / "sess.jsonl"
    record = json.loads(dest.read_text(encoding="utf-8").strip())
    assert record["cwd"] == new_wd
    assert record["sessionId"] == "sess"  # everything else preserved


async def test_rewrite_survives_a_transcript_failure(migrated, tmp_path, monkeypatch):
    """A transcript re-key that blows up degrades that fork's resume — it must
    never block startup or lose the DB rewrite."""
    s, db = migrated
    monkeypatch.setattr(
        "server.harness.claude_code._claude_project_dir",
        lambda wd: (_ for _ in ()).throw(OSError("boom")),
    )
    await _add_session(db, "s1", f"{s.legacy_home_dir}/fork/p")

    assert await rewrite_legacy_paths(db, s) == 1

    assert await _get(db, "sessions", "working_dir", "s1") == f"{s.home_dir}/fork/p"


# ------------------------------------------------------------------- settings


def test_state_dirs_derive_from_home_dir(monkeypatch):
    # conftest exports these to isolate test state; an explicit env var beats
    # the derived default (see the next test), so drop them here.
    monkeypatch.delenv("OWLERY_AGENTS_DIR", raising=False)
    monkeypatch.delenv("OWLERY_RESEARCH_DIR", raising=False)
    s = Settings(home_dir="/srv/state", _env_file=None)
    assert s.attachments_dir == "/srv/state/attachments"
    assert s.large_prompts_dir == "/srv/state/large-prompts"
    assert s.codex_home_dir == "/srv/state/codex"
    assert s.agents_dir == "/srv/state/agents"
    assert s.fork_dir == "/srv/state/fork"
    assert s.research_dir == "/srv/state/research"


def test_home_relative_dirs_expand_at_use_time(monkeypatch, tmp_path):
    """`~` must resolve when the path is USED, not when config is loaded —
    that's what lets a test relocate the tree by monkeypatching $HOME, and it's
    why the fork/research dirs are properties rather than eager literals."""
    monkeypatch.delenv("OWLERY_RESEARCH_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    s = Settings(_env_file=None)

    assert s.fork_dir == "~/.owlery/fork"  # stored unexpanded
    assert s.resolved_fork_dir == str(tmp_path / "home" / ".owlery" / "fork")
    assert s.resolved_research_dir == str(tmp_path / "home" / ".owlery" / "research")


def test_explicit_state_dir_overrides_home_dir(monkeypatch):
    monkeypatch.delenv("OWLERY_AGENTS_DIR", raising=False)
    s = Settings(home_dir="/srv/state", agents_dir="/elsewhere/agents",
                 _env_file=None)
    assert s.agents_dir == "/elsewhere/agents"
    assert s.attachments_dir == "/srv/state/attachments"  # the rest still derive


def test_legacy_env_prefix_is_honored(monkeypatch):
    """`OCTOPUS_AUTH_TOKEN` must keep working: server.crypto derives the
    credential encryption key from its VALUE, so silently falling back to the
    default would make every stored secret undecryptable."""
    monkeypatch.delenv("OWLERY_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "legacy-secret")
    monkeypatch.setenv("OCTOPUS_PORT", "9100")

    s = Settings(_env_file=None)

    assert s.auth_token == "legacy-secret"
    assert s.port == 9100


def test_new_env_prefix_wins_over_legacy(monkeypatch):
    monkeypatch.setenv("OWLERY_AUTH_TOKEN", "new-secret")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "legacy-secret")

    assert Settings(_env_file=None).auth_token == "new-secret"
