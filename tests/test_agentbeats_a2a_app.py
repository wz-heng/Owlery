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

from server.agent_manager import AgentManager
from server.agentbeats.app import build_a2a_app
from server.agentbeats.manager import AgentBeatsExamManager
from server.database import Database
from server.harness import HarnessEvent
from server.session_manager import SessionManager


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
    exam_mgr.shutdown()
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
