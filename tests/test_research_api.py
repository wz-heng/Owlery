"""REST tests for /api/sessions/{sid}/research (native-deep-research.md §7).

Binds the global singletons to a per-test in-memory DB (like the delegations
route tests) and fakes the pipeline so no CLI runs.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from server.agent_manager import AgentManager
from server.database import Database
from server.main import app
from server.research import manager as rm_mod
from server.research.manager import research_manager
from server.research.orchestrator import ResearchReport
from server.routers import agents as agents_mod
from server.session_manager import session_manager

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
async def client(db, monkeypatch):
    session_manager.sessions.clear()
    await session_manager.initialize(db)
    agents_mod.set_manager(AgentManager(db))
    research_manager.bind(session_mgr=session_manager, db=db)
    monkeypatch.setattr(session_manager, "start_message", lambda *a, **k: asyncio.sleep(0))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _session(client):
    octo = (await client.get("/api/agents", headers=HEADERS)).json()[0]
    r = await client.post(
        "/api/sessions",
        json={"name": "S", "working_dir": "/tmp", "agent_id": octo["id"]},
        headers=HEADERS,
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_start_get_list_research(client, monkeypatch):
    # `list` is now a `running`-only snapshot (see test_list_excludes_terminal_
    # jobs), so the pipeline must still be in flight when we check it — an
    # instantly-resolving fake would race the list GET and flake.
    import asyncio as _asyncio

    async def slow_run(question, *, on_progress=None, **kw):
        await _asyncio.sleep(30)
        return ResearchReport(question=question, report="report", findings=[],
                              sources=[], angles=["a"], claims_examined=0, cost=None)

    monkeypatch.setattr(rm_mod, "run_research", slow_run)

    session = await _session(client)
    r = await client.post(
        f"/api/sessions/{session['id']}/research",
        json={"question": "what is X?"}, headers=HEADERS,
    )
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["status"] == "running"

    try:
        # GET single + list
        got = await client.get(f"/api/sessions/{session['id']}/research/{rid}", headers=HEADERS)
        assert got.status_code == 200 and got.json()["question"] == "what is X?"
        lst = await client.get(f"/api/sessions/{session['id']}/research", headers=HEADERS)
        assert any(j["id"] == rid for j in lst.json())
    finally:
        # Don't leave the 30s sleep task for pytest-asyncio's loop teardown to
        # reap (Snape review).
        await client.post(
            f"/api/sessions/{session['id']}/research/{rid}/cancel", headers=HEADERS
        )


@pytest.mark.asyncio
async def test_list_excludes_terminal_jobs(client, monkeypatch):
    """The list route is a snapshot for session load / WS reconnect, not a
    history log. A job that went terminal long ago must not reappear from a
    refresh once the frontend's own linger timer has (or will have)
    dismissed its card (ResearchCard dismiss task). A job that went terminal
    only moments ago DOES still appear — this covers a client reconnecting
    right after missing the terminal WS broadcast (Snape review), for which
    failed/cancelled/interrupted jobs have no transcript-message fallback."""
    import asyncio as _asyncio
    from datetime import datetime, timedelta, timezone

    async def slow_run(question, *, on_progress=None, **kw):
        await _asyncio.sleep(30)
        return ResearchReport(question=question, report="r", findings=[],
                              sources=[], angles=[], claims_examined=0, cost=None)

    monkeypatch.setattr(rm_mod, "run_research", slow_run)

    session = await _session(client)
    started = await client.post(
        f"/api/sessions/{session['id']}/research",
        json={"question": "still running?"}, headers=HEADERS,
    )
    running_id = started.json()["id"]

    # Bypass the manager for the terminal fixture rows — inserting them
    # directly avoids any race with a background pipeline task completing on
    # its own.
    long_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    stale_id = "stale-finished-job"
    await research_manager.db.create_research_job(
        stale_id, session["id"], "finished ages ago?", "t0",
    )
    await research_manager.db.update_research_job(
        stale_id, status="completed", phase="done", completed_at=long_ago,
    )

    just_now = datetime.now(timezone.utc).isoformat()
    recent_id = "recent-failed-job"
    await research_manager.db.create_research_job(
        recent_id, session["id"], "just failed?", "t0",
    )
    await research_manager.db.update_research_job(
        recent_id, status="failed", error="boom", completed_at=just_now,
    )

    lst = (await client.get(f"/api/sessions/{session['id']}/research", headers=HEADERS)).json()
    ids = {j["id"] for j in lst}
    assert running_id in ids
    assert recent_id in ids
    assert stale_id not in ids

    # The single-job GET is unaffected — a terminal job is still fetchable by id
    # regardless of how long ago it finished.
    got = await client.get(
        f"/api/sessions/{session['id']}/research/{stale_id}", headers=HEADERS
    )
    assert got.status_code == 200
    assert got.json()["status"] == "completed"

    # Cleanup: cancel the still-running job so it doesn't leak into teardown.
    await client.post(
        f"/api/sessions/{session['id']}/research/{running_id}/cancel", headers=HEADERS
    )


@pytest.mark.asyncio
async def test_start_research_empty_question_422(client):
    session = await _session(client)
    r = await client.post(
        f"/api/sessions/{session['id']}/research", json={"question": "  "},
        headers=HEADERS,
    )
    # pydantic min_length on a stripped-empty string → 422, or our 400.
    assert r.status_code in (400, 422)


@pytest.mark.asyncio
async def test_start_research_unknown_session_404(client):
    r = await client.post(
        "/api/sessions/nope/research", json={"question": "q"}, headers=HEADERS
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_research_requires_auth(client):
    r = await client.post("/api/sessions/x/research", json={"question": "q"})
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_cancel_wrong_session_404_does_not_cancel(client, monkeypatch):
    """A cancel scoped to the WRONG session must 404 WITHOUT cancelling the
    real job (Vera review — verify ownership before mutating)."""
    import asyncio as _asyncio

    from server.research.orchestrator import ResearchReport

    async def slow_run(question, *, on_progress=None, **kw):
        await _asyncio.sleep(30)
        return ResearchReport(question=question, report="r", findings=[],
                              sources=[], angles=[], claims_examined=0, cost=None)

    monkeypatch.setattr(rm_mod, "run_research", slow_run)
    session = await _session(client)
    started = await client.post(
        f"/api/sessions/{session['id']}/research",
        json={"question": "q"}, headers=HEADERS,
    )
    rid = started.json()["id"]

    bad = await client.post(
        f"/api/sessions/some-other-session/research/{rid}/cancel", headers=HEADERS
    )
    assert bad.status_code == 404
    # The real job must still be running — not cancelled by the bad request.
    got = await client.get(f"/api/sessions/{session['id']}/research/{rid}", headers=HEADERS)
    assert got.json()["status"] == "running"
