"""Focused contract tests for the Task Board REST router."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.routers import task_boards as routes
from server.database import Database
from server.task_board.models import TaskConflictError
from server.task_board.repository import TaskRepository

HEADERS = {"Authorization": "Bearer changeme"}


@dataclass
class _Record:
    id: str
    updated_at: str = "v1"
    status: str = "triage"

    def to_dict(self):
        return vars(self)


class _Manager:
    def __init__(self):
        self.calls = []

    async def wake_dispatcher(self):
        self.calls.append(("wake",))

    async def publish_task_update(self, task_id):
        self.calls.append(("publish", task_id))

    async def publish_board_update(self, board_id):
        self.calls.append(("publish_board", board_id))

    async def publish_board_updates(self, board_id):
        self.calls.append(("publish_board_tasks", board_id))

    async def complete_worker(self, *identity, **payload):
        self.calls.append(("complete", identity, payload))
        return {"state": "completed"}

    async def create_worker_task(self, *identity, **payload):
        self.calls.append(("worker_create", identity, payload))
        return {"id": "child-1", "status": payload["status"]}


@pytest.fixture
async def client(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    manager = _Manager()
    routes.set_manager(manager)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, manager


@pytest.mark.asyncio
async def test_create_task_reports_idempotency_winner(client, monkeypatch):
    c, _manager = client

    class Repo:
        async def create_task_result(self, **kwargs):
            assert kwargs["dependencies"] == ["dep-1"]
            assert kwargs["created_by_kind"] == "user"
            return _Record("task-1"), True

    monkeypatch.setattr(routes, "task_repository", Repo())
    response = await c.post(
        "/api/task-boards/board-1/tasks",
        headers=HEADERS,
        json={
            "title": "Ship it",
            "specified": True,
            "dependencies": ["dep-1"],
            "idempotency_key": "request-1",
        },
    )
    assert response.status_code == 201
    assert response.headers["location"] == "/api/tasks/task-1"


@pytest.mark.asyncio
async def test_create_task_replay_is_200(client, monkeypatch):
    c, _manager = client

    class Repo:
        async def create_task_result(self, **kwargs):
            return _Record("task-existing"), False

    monkeypatch.setattr(routes, "task_repository", Repo())
    response = await c.post(
        "/api/task-boards/board-1/tasks",
        headers=HEADERS,
        json={"title": "Ship it", "idempotency_key": "same"},
    )
    assert response.status_code == 200
    assert response.json()["id"] == "task-existing"


@pytest.mark.asyncio
async def test_patch_task_uses_repository_cas(client, monkeypatch):
    c, _manager = client
    seen = {}

    class Repo:
        async def get_task(self, task_id):
            return _Record(task_id, updated_at="stamp-1")

        async def update_task(self, task_id, **kwargs):
            seen.update(kwargs)
            return _Record(task_id, updated_at="stamp-2")

    monkeypatch.setattr(routes, "task_repository", Repo())
    response = await c.patch(
        "/api/tasks/task-1",
        headers={**HEADERS, "If-Match": '"stamp-1"'},
        json={"title": "New title", "parent_id": "parent-1"},
    )
    assert response.status_code == 200
    assert response.headers["etag"] == '"stamp-2"'
    assert seen == {
        "expected_updated_at": "stamp-1",
        "title": "New title",
        "parent_task_id": "parent-1",
    }


@pytest.mark.asyncio
async def test_patch_task_rejects_stale_drawer(client, monkeypatch):
    c, _manager = client

    class Repo:
        async def get_task(self, task_id):
            return _Record(task_id, updated_at="newer")

    monkeypatch.setattr(routes, "task_repository", Repo())
    response = await c.patch(
        "/api/tasks/task-1",
        headers={**HEADERS, "If-Match": '"old"'},
        json={"title": "Lost edit"},
    )
    assert response.status_code == 412
    assert response.json()["detail"]["current"]["updated_at"] == "newer"


@pytest.mark.asyncio
async def test_repository_conflict_is_structured_409(client, monkeypatch):
    c, _manager = client
    current = _Record("task-1", status="ready")

    class Repo:
        async def add_dependency(self, *args, **kwargs):
            raise TaskConflictError("dependency would create a cycle", current=current)

    monkeypatch.setattr(routes, "task_repository", Repo())
    response = await c.post(
        "/api/tasks/task-1/dependencies",
        headers=HEADERS,
        json={"depends_on_task_id": "task-2"},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "conflict"
    assert detail["current"]["id"] == "task-1"


@pytest.mark.asyncio
async def test_worker_complete_uses_only_trusted_headers(client):
    c, manager = client
    response = await c.post(
        "/api/task-worker/current/complete",
        headers={
            **HEADERS,
            "X-Owlery-Session-ID": "session-1",
            "X-Owlery-Task-ID": "task-1",
            "X-Owlery-Task-Run-ID": "run-1",
        },
        json={"summary": "Done", "metadata": {"tests": "green"}},
    )
    assert response.status_code == 200
    assert response.json() == {"state": "completed"}
    assert manager.calls == [
        (
            "complete",
            ("task-1", "run-1", "session-1"),
            {"summary": "Done", "metadata": {"tests": "green"}, "artifacts": []},
        )
    ]


@pytest.mark.asyncio
async def test_worker_callback_requires_all_identity_headers(client):
    c, manager = client
    response = await c.post(
        "/api/task-worker/current/complete",
        headers={**HEADERS, "X-Owlery-Task-ID": "task-1"},
        json={"summary": "Done"},
    )
    assert response.status_code == 422
    assert manager.calls == []


@pytest.mark.asyncio
async def test_worker_create_preserves_unspecified_triage_state(client):
    c, manager = client
    response = await c.post(
        "/api/task-worker/current/tasks",
        headers={
            **HEADERS,
            "X-Owlery-Session-ID": "session-1",
            "X-Owlery-Task-ID": "task-1",
            "X-Owlery-Task-Run-ID": "run-1",
        },
        json={"title": "Investigate later"},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "triage"
    operation, identity, payload = manager.calls[0]
    assert operation == "worker_create"
    assert identity == ("task-1", "run-1", "session-1")
    assert payload["status"] == "triage"
    assert "workspace_mode" not in payload
    assert "working_dir_override" not in payload


@pytest.mark.asyncio
async def test_real_repository_board_task_roundtrip(client, monkeypatch, tmp_path):
    """Exercise the router against the real second SQLite connection."""
    c, manager = client
    db_path = tmp_path / "task-api.db"
    database = Database(str(db_path))
    await database.initialize()
    repository = TaskRepository(str(db_path))
    await repository.initialize()
    monkeypatch.setattr(routes, "task_repository", repository)
    try:
        board = await c.post(
            "/api/task-boards",
            headers=HEADERS,
            json={"name": "Release", "working_dir": str(tmp_path)},
        )
        assert board.status_code == 201, board.text
        board_id = board.json()["id"]

        created = await c.post(
            f"/api/task-boards/{board_id}/tasks",
            headers=HEADERS,
            json={"title": "Ship", "body": "Do the work"},
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        assert created.json()["status"] == "triage"

        detail = await c.get(f"/api/tasks/{task_id}", headers=HEADERS)
        assert detail.status_code == 200
        assert detail.json()["dependencies"] == []
        assert detail.json()["runs"] == []

        specified = await c.post(
            f"/api/tasks/{task_id}/specify",
            headers=HEADERS,
            json={"body": "Executable spec"},
        )
        assert specified.status_code == 200
        assert specified.json()["status"] == "todo"
        assert ("publish", task_id) in manager.calls
    finally:
        await repository.close()
        await database.close()
