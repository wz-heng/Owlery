"""Deploy configuration, the `deployments` table, and the board opt-in flag
(docs/plans/local-deploy.md §6/§9, step 1 of §17).
"""
from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from server.config import Settings
from server.database import Database
from server.task_board.repository import TaskRepository


# ------------------------------------------------------------------- config §9


def test_deploy_config_defaults():
    s = Settings(deploy_root="")
    assert s.deploy_root == ""
    assert s.resolved_deploy_root == ""  # feature disabled
    assert s.deploy_stage_timeout_seconds == 600
    assert s.deploy_switch_timeout_seconds == 30
    assert s.deploy_health_timeout_seconds == 60
    assert s.deploy_quiesce_timeout_seconds == 120
    assert s.deploy_keep_snapshots == 5


def test_deploy_root_expands_user(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    s = Settings(deploy_root="~/deploy")
    assert s.resolved_deploy_root == str(tmp_path / "deploy")


def test_deploy_config_from_env(monkeypatch):
    monkeypatch.setenv("OWLERY_DEPLOY_ROOT", "/srv/owlery-deploy")
    monkeypatch.setenv("OWLERY_DEPLOY_KEEP_SNAPSHOTS", "9")
    s = Settings()
    assert s.deploy_root == "/srv/owlery-deploy"
    assert s.resolved_deploy_root == "/srv/owlery-deploy"
    assert s.deploy_keep_snapshots == 9


# ---------------------------------------------------------- deployments table §6


@pytest.fixture
async def conn(tmp_path: Path):
    db_path = tmp_path / "owlery.db"
    db = Database(str(db_path))
    await db.initialize()
    await db.close()
    connection = await aiosqlite.connect(str(db_path))
    connection.row_factory = aiosqlite.Row
    await connection.execute("PRAGMA foreign_keys=ON")
    yield connection
    await connection.close()


async def _objects(connection, kind: str) -> set[str]:
    cur = await connection.execute(
        "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
    )
    return {r["name"] for r in await cur.fetchall()}


async def test_deployments_table_and_indexes_exist(conn):
    assert "deployments" in await _objects(conn, "table")
    indexes = await _objects(conn, "index")
    assert "deployments_one_live" in indexes
    assert "deployments_one_active" in indexes


async def _insert_deployment(connection, dep_id: str, state: str) -> None:
    await connection.execute(
        "INSERT INTO deployments "
        "(id, slot, sha, source_repo, state, created_at, updated_at) "
        "VALUES (?, 'a', 'deadbeef', '/repo', ?, '2026-07-30', '2026-07-30')",
        (dep_id, state),
    )


async def test_one_live_deployment_enforced(conn):
    await _insert_deployment(conn, "d1", "live")
    # A second live row is rejected by the partial unique index.
    with pytest.raises(aiosqlite.IntegrityError):
        await _insert_deployment(conn, "d2", "live")
    # Non-live rows coexist freely.
    await _insert_deployment(conn, "d3", "superseded")
    await _insert_deployment(conn, "d4", "rolled_back")


async def test_one_active_deployment_enforced(conn):
    await _insert_deployment(conn, "d1", "staging")
    with pytest.raises(aiosqlite.IntegrityError):
        await _insert_deployment(conn, "d2", "switching")
    # But a live deploy alongside no active one is fine.
    await conn.execute("DELETE FROM deployments WHERE id = 'd1'")
    await _insert_deployment(conn, "d3", "live")
    await _insert_deployment(conn, "d4", "switching")


# ----------------------------------------------------- board opt-in flag §9


@pytest.fixture
async def repo(tmp_path: Path):
    db_path = tmp_path / "owlery.db"
    db = Database(str(db_path))
    await db.initialize()
    repository = TaskRepository(str(db_path))
    await repository.initialize()
    yield repository, tmp_path
    await repository.close()
    await db.close()


async def test_board_allow_local_deploy_defaults_false(repo):
    repository, root = repo
    board = await repository.create_board(name="B1", working_dir=str(root))
    assert board.allow_local_deploy is False
    # Survives a reload from disk.
    assert (await repository.get_board(board.id)).allow_local_deploy is False


async def test_board_allow_local_deploy_opt_in_and_update(repo):
    repository, root = repo
    board = await repository.create_board(
        name="B2", working_dir=str(root), allow_local_deploy=True
    )
    assert board.allow_local_deploy is True

    toggled = await repository.update_board(board.id, allow_local_deploy=False)
    assert toggled.allow_local_deploy is False
    assert (await repository.get_board(board.id)).allow_local_deploy is False
