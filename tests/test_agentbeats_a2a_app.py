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
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, TextPart

from server.agent_manager import AgentManager
from server.agentbeats.app import a2a_mount_path_and_card_url, build_a2a_app
from server.agentbeats.manager import AgentBeatsExamManager
from server.database import Database
from server.harness import HarnessEvent
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


@pytest.mark.asyncio
async def test_a2a_mount_ordering_and_reachability_for_both_branches(monkeypatch):
    """Deterministic version of the ordering regression guard — the
    earlier version depended on whether this checkout happened to have
    `web/dist/` built, which flips which branch `server.main.app` is
    actually in and can silently stop testing the one that broke. Forces
    *both* branches by reloading `server.main` with `Path.is_dir` patched
    (scoped to the specific `_dist_dir` check by path suffix, restored
    immediately after the reload) and makes real ASGI HTTP requests
    against each resulting app — not just introspecting the route table,
    which FastAPI's `include_router` makes opaque (`_IncludedRouter`
    objects, not flattened `Route`s) anyway. Also closes the other half
    of Snape's review: the dev-mode (SPA-present) branch's card now
    advertises `/a2a/` (trailing slash) specifically so a client that
    doesn't follow redirects can still reach it using exactly that URL,
    which this test calls with `follow_redirects=False` to prove.
    """
    import importlib

    import server.main as main_module

    real_is_dir = Path.is_dir

    def make_patched_is_dir(dist_present: bool):
        def _patched(self):
            if str(self).endswith("web/dist"):
                return dist_present
            return real_is_dir(self)
        return _patched

    def reload_as(dist_present: bool):
        monkeypatch.setattr(Path, "is_dir", make_patched_is_dir(dist_present))
        try:
            importlib.reload(main_module)
        finally:
            monkeypatch.undo()  # never let the patch leak past this one reload
        return main_module.app

    try:
        # --- Branch 1: no web/dist -> A2A root-mounted ---
        app_root = reload_as(False)
        transport_root = httpx.ASGITransport(app=app_root)
        async with httpx.AsyncClient(
            transport=transport_root, base_url="http://test", follow_redirects=False
        ) as c:
            # Exactly the routes the regression swallowed (404, inside the
            # A2A sub-app, which has neither).
            assert (await c.get("/health")).status_code != 404
            assert (await c.get("/api/backends")).status_code != 404
            card = await c.get("/.well-known/agent-card.json")
            assert card.status_code == 200
            assert "/a2a" not in card.json()["url"]

        # --- Branch 2: web/dist present -> A2A sub-path-mounted at /a2a,
        # card advertises /a2a/ with the trailing slash the whole fix is
        # about. A non-redirect-following client must reach it directly.
        app_dev = reload_as(True)
        transport_dev = httpx.ASGITransport(app=app_dev)
        async with httpx.AsyncClient(
            transport=transport_dev, base_url="http://test", follow_redirects=False
        ) as c:
            assert (await c.get("/health")).status_code != 404
            assert (await c.get("/api/backends")).status_code != 404
            card = await c.get("/a2a/.well-known/agent-card.json")
            assert card.status_code == 200
            advertised_url = card.json()["url"]
            assert advertised_url.endswith("/a2a/")
    finally:
        # Leave the module in its real, filesystem-driven state for every
        # other test file that imports `server.main` after this one.
        importlib.reload(main_module)


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
    await exam_mgr.bind(session_mgr=session_mgr, agent_mgr=agent_mgr)
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


@pytest.fixture
async def a2a_client_subpath():
    """Mirrors the dev-mode (web/dist present) branch exactly: A2A lives
    at a sub-path, and the card advertises it *with* the trailing slash
    (`a2a_mount_path_and_card_url`'s fix) rather than relying on a
    redirect — httpx.AsyncClient defaults to `follow_redirects=False`,
    same as the real green agent's own a2a-sdk client, so this fixture
    only works end to end if the trailing slash is actually right."""
    from starlette.applications import Starlette
    from starlette.routing import Mount

    db = Database(":memory:")
    await db.initialize()
    session_mgr = SessionManager()
    await session_mgr.initialize(db)
    agent_mgr = AgentManager(db)
    exam_mgr = AgentBeatsExamManager()
    await exam_mgr.bind(session_mgr=session_mgr, agent_mgr=agent_mgr)
    inner_app = build_a2a_app(exam_mgr, card_url="http://test/a2a/")
    wrapper = Starlette(routes=[Mount("/a2a", app=inner_app)])

    transport = httpx.ASGITransport(app=wrapper)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as hc:
        yield hc, session_mgr, exam_mgr
    await exam_mgr.shutdown()
    await db.close()


async def test_a2a_subpath_card_advertises_a_url_reachable_without_redirect(
    a2a_client_subpath,
):
    """The actual communication the mount-path fix exists for, not just
    the card's advertised string: resolve the card, then call back
    through a real A2A message using exactly the URL it advertises."""
    hc, session_mgr, exam_mgr = a2a_client_subpath

    def factory(s, agent=None, connectors=None, **_kw):
        class ScriptedBackend:
            name = "scripted"

            async def start(self, prompt, working_dir, resume_id=None, credential=None):
                pass

            def stream(self):
                async def _gen():
                    exam_mgr.record_decision(
                        s.id, {"kind": "exec", "command": "ls", "timeout": 10}
                    )
                    yield HarnessEvent(type="result", session_id="x", cost=0.004)

                return _gen()

            async def stop(self):
                pass

        return ScriptedBackend()

    session_mgr._make_run = factory  # type: ignore[method-assign]

    task_payload = json.dumps(
        {"kind": "task", "protocol": "terminal-bench-shell-v1", "instruction": "do X"}
    )
    event, agent_card = await _send(
        hc, task_payload, context_id=None, base_url="http://test/a2a/"
    )

    assert agent_card.url == "http://test/a2a/"
    task, _update = event
    assert task.status.state.value == "completed"
    decision = json.loads(task.artifacts[0].parts[0].root.text)
    assert decision == {"kind": "exec_request", "command": "ls", "timeout": 10}


async def _send(
    hc: httpx.AsyncClient, text: str, context_id: str | None, *, base_url: str = "http://test"
):
    resolver = A2ACardResolver(httpx_client=hc, base_url=base_url)
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
