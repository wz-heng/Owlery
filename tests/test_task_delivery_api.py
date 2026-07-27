"""Contract tests for the Git-delivery REST endpoints (thin router layer)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.routers import task_boards as routes
from server.task_board.models import DeliveryConfirmationRequired

HEADERS = {"Authorization": "Bearer changeme"}


@dataclass
class _Delivery:
    id: str = "d1"
    task_id: str = "t1"
    run_id: str = "r1"
    status: str = "ready"

    def to_dict(self):
        return vars(self)


class _Manager:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.raise_confirm = False

    async def publish_task_update(self, task_id):
        pass

    async def deliver_accept(self, task_id, run_id, **kw):
        self.calls.append(("accept", {"task_id": task_id, "run_id": run_id, **kw}))
        return _Delivery(status="ready")

    async def deliver_run_op(self, task_id, run_id, **kw):
        self.calls.append(("op", {"task_id": task_id, "run_id": run_id, **kw}))
        if self.raise_confirm:
            raise DeliveryConfirmationRequired(
                "force push needs confirmation",
                confirmation="allow_force_push", action="push",
                current=_Delivery(status="ready"),
            )
        return _Delivery(status="delivered")

    async def deliver_teardown(self, task_id, run_id, **kw):
        self.calls.append(("teardown", {"task_id": task_id, "run_id": run_id, **kw}))
        return _Delivery(status="delivered")

    async def request_delivery(self, task_id, run_id, session_id, **kw):
        self.calls.append(("worker_request", {"task_id": task_id, "run_id": run_id,
                                              "session_id": session_id, **kw}))
        return {"requested": True}


@dataclass
class _Repo:
    delivery: _Delivery | None = field(default_factory=_Delivery)

    async def get_delivery_by_run(self, run_id):
        return self.delivery

    async def list_delivery_ops(self, delivery_id):
        return [type("Op", (), {"to_dict": lambda self: {"id": "op1", "kind": "push"}})()]


@pytest.fixture
async def client(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    manager = _Manager()
    routes.set_manager(manager)
    monkeypatch.setattr(routes, "task_repository", _Repo())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, manager


@pytest.mark.asyncio
async def test_get_delivery_and_404(client, monkeypatch):
    c, _ = client
    r = await c.get("/api/tasks/t1/runs/r1/delivery", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["delivery"]["id"] == "d1" and body["ops"][0]["kind"] == "push"

    monkeypatch.setattr(routes, "task_repository", _Repo(delivery=None))
    r = await c.get("/api/tasks/t1/runs/r1/delivery", headers=HEADERS)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_accept_and_op_routing(client):
    c, manager = client
    r = await c.post("/api/tasks/t1/runs/r1/delivery/accept",
                     json={"base_ref": "main"}, headers=HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "ready"
    assert manager.calls[0][1]["base_ref"] == "main"
    assert manager.calls[0][1]["actor_kind"] == "user"

    await c.post("/api/tasks/t1/runs/r1/delivery/commit", headers=HEADERS)
    assert manager.calls[-1][1]["kind"] == "commit"

    await c.post("/api/tasks/t1/runs/r1/delivery/push",
                 json={"confirmations": {"allow_force_push": True}}, headers=HEADERS)
    assert manager.calls[-1][1]["kind"] == "push"
    assert manager.calls[-1][1]["confirmations"] == {"allow_force_push": True}

    await c.post("/api/tasks/t1/runs/r1/delivery/pull-request",
                 json={"connector_installation_id": "gh1", "draft": True}, headers=HEADERS)
    assert manager.calls[-1][1]["kind"] == "pull_request"
    assert manager.calls[-1][1]["connector_installation_id"] == "gh1"

    await c.post("/api/tasks/t1/runs/r1/delivery/merge",
                 json={"merge_strategy": "fast_forward_only"}, headers=HEADERS)
    assert manager.calls[-1][1]["kind"] == "merge"

    await c.post("/api/tasks/t1/runs/r1/delivery/teardown",
                 json={"retention": "remove_all", "confirmations": {}}, headers=HEADERS)
    assert manager.calls[-1][0] == "teardown"
    assert manager.calls[-1][1]["retention"] == "remove_all"


@pytest.mark.asyncio
async def test_requires_confirmation_409(client):
    c, manager = client
    manager.raise_confirm = True
    r = await c.post("/api/tasks/t1/runs/r1/delivery/push",
                     json={"confirmations": {}}, headers=HEADERS)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "requires_confirmation"
    assert detail["confirmation"] == "allow_force_push" and detail["action"] == "push"
    assert detail["current"]["status"] == "ready"


@pytest.mark.asyncio
async def test_worker_request_delivery(client):
    c, manager = client
    r = await c.post(
        "/api/task-worker/current/delivery/request",
        json={"note": "please ship"},
        headers={**HEADERS, "X-Owlery-Session-ID": "s1",
                 "X-Owlery-Task-ID": "t1", "X-Owlery-Task-Run-ID": "r1"},
    )
    assert r.status_code == 200 and r.json()["requested"] is True
    assert manager.calls[-1][0] == "worker_request"
    assert manager.calls[-1][1]["note"] == "please ship"
