"""Route-level tests for `POST /api/sessions/{sid}/tbexec/decision`
(server/routers/tbexec.py) — the HTTP shim the `tbexec` MCP tool posts to.

Mirrors tests/test_bg_tasks.py's `api_client` fixture shape: real app,
ASGI transport, lifespan not run — only the pieces this route touches are
reset and rebound per test.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from server.agentbeats.manager import AgentBeatsExamManager, agentbeats_exam_manager
from server.main import app

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def api_client():
    agentbeats_exam_manager.__init__()  # type: ignore[misc]  # reset singleton state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    agentbeats_exam_manager.__init__()  # type: ignore[misc]


async def test_decision_exec_requires_command(api_client):
    r = await api_client.post(
        "/api/sessions/s1/tbexec/decision",
        json={"kind": "exec"},
        headers=HEADERS,
    )
    assert r.status_code == 400


async def test_decision_exec_recorded(api_client):
    seen: list[tuple[str, dict]] = []
    agentbeats_exam_manager.record_decision = lambda sid, d: (  # type: ignore[method-assign]
        seen.append((sid, d)) or True
    )
    r = await api_client.post(
        "/api/sessions/s1/tbexec/decision",
        json={"kind": "exec", "command": "ls -la", "timeout": 30},
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert "end your turn" in r.json()["message"].lower()
    assert seen == [("s1", {"kind": "exec", "command": "ls -la", "timeout": 30})]


async def test_decision_exec_defaults_timeout(api_client):
    seen: list[dict] = []
    agentbeats_exam_manager.record_decision = lambda sid, d: seen.append(d) or True  # type: ignore[method-assign]
    r = await api_client.post(
        "/api/sessions/s1/tbexec/decision",
        json={"kind": "exec", "command": "ls"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert seen[0]["timeout"] == 60


async def test_decision_finish_recorded(api_client):
    seen: list[dict] = []
    agentbeats_exam_manager.record_decision = lambda sid, d: seen.append(d) or True  # type: ignore[method-assign]
    r = await api_client.post(
        "/api/sessions/s1/tbexec/decision",
        json={"kind": "finish", "output": "done"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert "finished" in r.json()["message"].lower()
    assert seen == [{"kind": "finish", "output": "done"}]


async def test_decision_conflict_maps_to_409(api_client):
    agentbeats_exam_manager.record_decision = lambda sid, d: False  # type: ignore[method-assign]
    r = await api_client.post(
        "/api/sessions/s1/tbexec/decision",
        json={"kind": "finish", "output": "done"},
        headers=HEADERS,
    )
    assert r.status_code == 409


async def test_decision_requires_auth(api_client):
    r = await api_client.post(
        "/api/sessions/s1/tbexec/decision",
        json={"kind": "finish", "output": "done"},
    )
    assert r.status_code in (401, 403)
