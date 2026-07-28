"""Model routing backend (budget-model-routing.md §4): resolve_model priority,
cross-family backend validation, and the session/fork/delegation/agent
plumbing that carries a per-session model override."""

from __future__ import annotations

import types

import pytest

from server.agent_manager import AgentManager
from server.database import Database
from server.delegations import DelegationError, DelegationManager
from server.model_routing import (
    ModelBackendError,
    resolve_model,
    validate_model_for_backend,
)
from server.session_manager import SessionManager


# --------------------------------------------------------------------------- #
# resolve_model — session.model > agent.model > None
# --------------------------------------------------------------------------- #


def _fake_session(model):
    return types.SimpleNamespace(model=model)


def test_resolve_model_session_wins():
    s = _fake_session("claude-opus-4")
    assert resolve_model(s, {"model": "claude-haiku"}) == "claude-opus-4"


def test_resolve_model_falls_back_to_agent():
    s = _fake_session(None)
    assert resolve_model(s, {"model": "claude-haiku"}) == "claude-haiku"


def test_resolve_model_none_when_neither_set():
    assert resolve_model(_fake_session(None), {"model": None}) is None
    assert resolve_model(_fake_session(None), None) is None
    assert resolve_model(_fake_session(""), {"model": ""}) is None


# --------------------------------------------------------------------------- #
# validate_model_for_backend — blacklist, not whitelist
# --------------------------------------------------------------------------- #


def test_validate_none_and_unknown_backend_pass():
    validate_model_for_backend("codex", None)
    validate_model_for_backend("claude-code", None)
    validate_model_for_backend("claude-code", "")
    # An unknown/other backend is never second-guessed.
    validate_model_for_backend("some-future-backend", "claude-opus-4")


@pytest.mark.parametrize("model", ["claude-opus-4", "Claude-Sonnet", "claude-3-5"])
def test_codex_rejects_claude_family(model):
    with pytest.raises(ModelBackendError):
        validate_model_for_backend("codex", model)


@pytest.mark.parametrize("model", ["gpt-5-codex", "gpt-4o", "o1", "o3-mini", "o4", "codex-mini"])
def test_claude_code_rejects_openai_family(model):
    with pytest.raises(ModelBackendError):
        validate_model_for_backend("claude-code", model)


@pytest.mark.parametrize(
    "backend,model",
    [
        # New/unknown model strings pass — the point of a blacklist.
        ("codex", "gpt-5-codex"),
        ("codex", "gpt-6-future"),
        ("claude-code", "claude-opus-4"),
        ("claude-code", "claude-next"),
        # The trap: "opus" starts with 'o' but is NOT an OpenAI o-series model.
        ("claude-code", "opus"),
        ("claude-code", "sonnet"),
        ("claude-code", "haiku"),
    ],
)
def test_valid_pairings_pass(backend, model):
    validate_model_for_backend(backend, model)


# --------------------------------------------------------------------------- #
# SessionManager: create/persist/reload + fork inheritance
# --------------------------------------------------------------------------- #


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
async def mgr(db):
    m = SessionManager()
    await m.initialize(db)
    yield m


@pytest.mark.asyncio
async def test_sessions_schema_has_model_column(db):
    cols = {row[1] for row in await db._column_info("sessions")}
    assert "model" in cols


@pytest.mark.asyncio
async def test_create_session_persists_and_reloads_model(db):
    agent = await db.get_default_agent()
    m1 = SessionManager()
    await m1.initialize(db)
    s = await m1.create_session(
        agent_id=agent["id"], name="pinned", working_dir="/tmp",
        model="claude-opus-4",
    )
    assert s.model == "claude-opus-4"
    # Reload from the same DB → the override survives.
    m2 = SessionManager()
    await m2.initialize(db)
    assert m2.get_session(s.id).model == "claude-opus-4"


@pytest.mark.asyncio
async def test_run_config_uses_resolved_model(mgr, db):
    """_run_config must prefer session.model over agent.model — the single
    resolution seam (budget-model-routing.md §4.1)."""
    agent = await db.get_default_agent()
    await db.update_agent(agent["id"], model="claude-haiku")
    agent = await db.get_agent(agent["id"])
    s = await mgr.create_session(
        agent_id=agent["id"], name="s", working_dir="/tmp", model="claude-opus-4",
    )
    cfg = mgr._run_config(s, agent)
    assert cfg.model == "claude-opus-4"
    # With no session override, the agent's model is used.
    s2 = await mgr.create_session(agent_id=agent["id"], name="s2", working_dir="/tmp")
    assert mgr._run_config(s2, agent).model == "claude-haiku"


@pytest.mark.asyncio
async def test_fork_inherits_parent_model(mgr, db):
    agent = await db.get_default_agent()
    parent = await mgr.create_session(
        agent_id=agent["id"], name="p", working_dir="/repo", model="claude-opus-4",
    )
    # Seed two user turns so seq=2 (the 2nd user message) exists to rewind to.
    await db.append_message(session_id=parent.id, seq=0, role="user", type="text", content="q0")
    await db.append_message(session_id=parent.id, seq=1, role="assistant", type="text", content="a0")
    await db.append_message(session_id=parent.id, seq=2, role="user", type="text", content="q1")
    await db.flush()
    parent._message_count = 3

    fork = await mgr.fork_session(parent.id, 2)
    assert fork.model == "claude-opus-4"
    # Durable, too.
    rows = await db.load_sessions(include_archived=True)
    raw = next(r for r in rows if r["id"] == fork.id)
    assert raw["model"] == "claude-opus-4"


@pytest.mark.asyncio
async def test_archive_successor_inherits_model(mgr, db):
    agent = await db.get_default_agent()
    s = await mgr.create_session(
        agent_id=agent["id"], name="s", working_dir="/tmp", model="claude-opus-4",
    )
    successor = await mgr.archive_session(s.id)
    assert successor.model == "claude-opus-4"


# --------------------------------------------------------------------------- #
# Agent CRUD validation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_agent_rejects_cross_family_model(db):
    am = AgentManager(db)
    with pytest.raises(ModelBackendError):
        await am.create_agent(name="BadCodex", backend="codex", model="claude-opus-4")


@pytest.mark.asyncio
async def test_update_agent_backend_switch_rechecks_existing_model(db):
    am = AgentManager(db)
    agent = await am.create_agent(name="Switcher", backend="claude-code", model="claude-opus-4")
    # Flipping only the backend to codex must be rejected against the kept model.
    with pytest.raises(ModelBackendError):
        await am.update_agent(agent["id"], backend="codex")
    # And a codex agent gaining a claude model (only `model` in the PATCH) is
    # rejected against its existing backend.
    cod = await am.create_agent(name="Cod", backend="codex")
    with pytest.raises(ModelBackendError):
        await am.update_agent(cod["id"], model="claude-opus-4")


@pytest.mark.asyncio
async def test_update_agent_valid_change_passes(db):
    am = AgentManager(db)
    agent = await am.create_agent(name="Ok", backend="claude-code", model="claude-opus-4")
    updated = await am.update_agent(agent["id"], model="claude-haiku")
    assert updated["model"] == "claude-haiku"


# --------------------------------------------------------------------------- #
# Delegation model passthrough + rejection
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_start_delegation_passes_model_to_child(db, monkeypatch):
    m = SessionManager()
    await m.initialize(db)
    dm = DelegationManager()
    dm.bind(session_mgr=m, db=db)

    async def _noop_start_message(sid, prompt, attachment_ids=None, injection_id=None):
        return None

    monkeypatch.setattr(m, "start_message", _noop_start_message)
    octo = await db.get_default_agent()
    await AgentManager(db).create_agent(name="Vera", backend="claude-code")
    parent = await m.create_session(agent_id=octo["id"], name="p", working_dir="/tmp")
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
        model="claude-opus-4",
    )
    child = m.get_session(rec.delegation_id)
    assert child.model == "claude-opus-4"
    dm.shutdown()


@pytest.mark.asyncio
async def test_start_delegation_rejects_cross_family_model(db, monkeypatch):
    m = SessionManager()
    await m.initialize(db)
    dm = DelegationManager()
    dm.bind(session_mgr=m, db=db)
    octo = await db.get_default_agent()
    await AgentManager(db).create_agent(name="Cod", backend="codex")
    parent = await m.create_session(agent_id=octo["id"], name="p", working_dir="/tmp")
    with pytest.raises(DelegationError) as exc:
        await dm.start_delegation(
            parent_session_id=parent.id, agent_name="cod", request="r",
            model="claude-opus-4",
        )
    assert exc.value.status_code == 422
    dm.shutdown()
