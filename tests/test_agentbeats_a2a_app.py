"""End-to-end A2A conformance test for the AgentBeats adapter.

Drives the mounted Starlette app (server/agentbeats/app.py) with the real
a2a-sdk *client* — `A2ACardResolver` + `ClientFactory`, the exact classes
RDI-Foundation's own `terminal-bench-green`/`agent-template` `messenger.py`
use — over an in-process ASGI transport (no real network, no real green
agent, no Docker). Only the CLI subprocess is faked (`SessionManager._make_run`
scripted, as in tests/test_agentbeats_manager.py); everything else — the
a2a-sdk server/client wire format, the Executor, the manager, the real
SessionManager/AgentManager/DB — is real. This is the layer the other two
agentbeats test files don't reach: the actual JSON-RPC envelope and agent
card a real green agent would see.
"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, TextPart

from starlette.routing import Route

from server.agent_manager import AgentManager
from server.agentbeats.app import a2a_mount_path_and_card_url, build_a2a_app
from server.agentbeats.manager import AgentBeatsExamManager
from server.database import Database
from server.harness import HarnessEvent
from server.main import app as real_app
from server.session_manager import SessionManager


def test_a2a_mount_path_root_when_no_spa():
    # No `web/dist/` (the real AgentBeats submission image never copies it
    # in) -> root mount. Verified necessary, not just convenient: the real
    # terminal-bench-green reference agent's a2a-sdk client doesn't follow
    # redirects, and Starlette's Mount(path="/a2a") 307s a bare "/a2a"
    # request (no trailing slash) to "/a2a/", which that client treats as a
    # hard failure. A root mount needs no such trailing-slash redirect.
    path, card_url = a2a_mount_path_and_card_url(dist_dir_exists=False, port=9010)
    assert path == "/"
    assert card_url == "http://127.0.0.1:9010"


def test_a2a_mount_registered_after_explicit_routes_in_real_app():
    # Structural regression guard for the ordering bug the full backend
    # suite caught, not a hand-written assumption. This only bites in the
    # root-mounted branch (`a2a_mount_path_and_card_url`'s "/" case, active
    # exactly when `web/dist/` is absent — the real AgentBeats submission
    # image's own condition), since a root Mount's regex matches every
    # path; skip rather than pass-trivially-for-the-wrong-reason if a dev
    # build happens to be sitting in this checkout.
    import server.main as main_module

    if main_module._dist_dir.is_dir():
        pytest.skip("web/dist present -> A2A sub-path-mounted, not the branch this guards")

    routes = real_app.router.routes
    index_by_name = {r.name: i for i, r in enumerate(routes) if getattr(r, "name", None)}
    a2a_index = index_by_name["a2a"]
    # Mount.__init__ does `self.path = path.rstrip("/")`, so root is "".
    assert real_app.router.routes[a2a_index].path == ""

    # Starlette runs Route/Mount entries in registration order and stops at
    # the first match, so every explicit route below must be registered
    # *before* the root A2A mount or it would be swallowed (404 inside the
    # A2A sub-app, which has none of them) -- exactly as /health and
    # /api/backends briefly were.
    health_index = next(
        i for i, r in enumerate(routes) if isinstance(r, Route) and r.path == "/health"
    )
    backends_index = next(
        i for i, r in enumerate(routes) if isinstance(r, Route) and r.path == "/api/backends"
    )
    assert health_index < a2a_index
    assert backends_index < a2a_index

    # And before the SPA catch-all, if one happens to be mounted (it isn't
    # in this branch, since the two are mutually exclusive on `web/dist/`
    # existing) -- it would make the A2A mount permanently unreachable.
    if "spa" in index_by_name:
        assert a2a_index < index_by_name["spa"]


def test_a2a_mount_path_subpath_when_spa_present():
    # Dev checkouts build `web/dist/` for the SPA, which already claims
    # "/" -> A2A must live at a sub-path to coexist. The card URL still
    # carries the trailing slash the mount path argument doesn't need, so
    # calling back on the card's own advertised address lands directly
    # rather than depending on a redirect too.
    path, card_url = a2a_mount_path_and_card_url(dist_dir_exists=True, port=8000)
    assert path == "/a2a"
    assert card_url == "http://127.0.0.1:8000/a2a/"


@pytest.fixture
async def a2a_client():
    db = Database(":memory:")
    await db.initialize()
    session_mgr = SessionManager()
    await session_mgr.initialize(db)
    agent_mgr = AgentManager(db)
    exam_mgr = AgentBeatsExamManager()
    exam_mgr.bind(session_mgr=session_mgr, agent_mgr=agent_mgr)
    # Built standalone here (unprefixed) — unlike the real app, which mounts
    # this same builder's output under "/a2a" (server/main.py). The a2a-sdk
    # client sends every follow-up request to the *card's* advertised `url`,
    # not to the URL the card itself was fetched from, so `card_url` must
    # match where this test's ASGI transport actually serves content: root.
    starlette_app = build_a2a_app(exam_mgr, card_url="http://test")

    transport = httpx.ASGITransport(app=starlette_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        yield hc, session_mgr, exam_mgr
    await exam_mgr.shutdown()
    await db.close()


async def _send(hc: httpx.AsyncClient, text: str, context_id: str | None):
    resolver = A2ACardResolver(httpx_client=hc, base_url="http://test")
    agent_card = await resolver.get_agent_card()
    factory = ClientFactory(ClientConfig(httpx_client=hc, streaming=False))
    client = factory.create(agent_card)
    msg = Message(
        kind="message",
        role=Role.user,
        parts=[Part(TextPart(kind="text", text=text))],
        message_id=uuid4().hex,
        context_id=context_id,
    )
    last_event = None
    async for event in client.send_message(msg):
        last_event = event
    return last_event, agent_card


async def test_agent_card_is_conformant(a2a_client):
    hc, _session_mgr, _exam_mgr = a2a_client
    r = await hc.get("/.well-known/agent-card.json")
    assert r.status_code == 200
    card = r.json()
    for field in ("name", "description", "url", "version", "capabilities", "skills"):
        assert field in card
    assert card["url"].startswith("http://")
    assert card["skills"]


async def test_full_round_trip_over_real_a2a_wire(a2a_client):
    hc, session_mgr, exam_mgr = a2a_client

    def factory(s, agent=None, connectors=None, **_kw):
        class ScriptedBackend:
            name = "scripted"

            async def start(self, prompt, working_dir, resume_id=None, credential=None):
                pass

            def stream(self):
                async def _gen():
                    yield HarnessEvent(type="text", content="checking")
                    exam_mgr.record_decision(
                        s.id, {"kind": "exec", "command": "pwd", "timeout": 10}
                    )
                    yield HarnessEvent(type="result", session_id="x", cost=0.004)

                return _gen()

            async def stop(self):
                pass

        return ScriptedBackend()

    session_mgr._make_run = factory  # type: ignore[method-assign]

    task_payload = json.dumps(
        {"kind": "task", "protocol": "terminal-bench-shell-v1", "instruction": "pwd please"}
    )
    event, _card = await _send(hc, task_payload, context_id=None)

    task, _update = event
    assert task.status.state.value == "completed"
    assert task.artifacts, "exam decision must come back as an artifact"
    text = task.artifacts[0].parts[0].root.text
    assert json.loads(text) == {"kind": "exec_request", "command": "pwd", "timeout": 10}

    context_id = task.context_id
    assert exam_mgr.get_record(context_id) is not None

    # Round 2, same context — the model declares the task finished.
    def factory2(s, agent=None, connectors=None, **_kw):
        class ScriptedBackend:
            name = "scripted"

            async def start(self, prompt, working_dir, resume_id=None, credential=None):
                pass

            def stream(self):
                async def _gen():
                    exam_mgr.record_decision(s.id, {"kind": "finish", "output": "/root"})
                    yield HarnessEvent(type="result", session_id="x", cost=0.001)

                return _gen()

            async def stop(self):
                pass

        return ScriptedBackend()

    session_mgr._make_run = factory2  # type: ignore[method-assign]

    exec_result_payload = json.dumps(
        {"kind": "exec_result", "exit_code": 0, "stdout": "/root\n", "stderr": ""}
    )
    event2, _card = await _send(hc, exec_result_payload, context_id=context_id)
    task2, _update2 = event2
    assert task2.status.state.value == "completed"
    text2 = task2.artifacts[0].parts[0].root.text
    assert json.loads(text2) == {"kind": "final", "output": "/root"}

    # Task finished — torn down, no longer tracked.
    assert exam_mgr.get_record(context_id).finished
