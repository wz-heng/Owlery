"""Unit tests for `AgentBeatsExamManager` (server/agentbeats/manager.py) —
the terminal-bench-shell-v1 round driver.

Follows tests/test_session_manager.py's `ScriptedBackend`/`fake_factory`
convention: `SessionManager._make_run` is monkeypatched to a canned
`HarnessEvent` stream, so each round runs the *real* `SessionManager`/
`AgentManager`/in-memory-DB path end to end with only the CLI subprocess
faked — the same split CLAUDE.md's "fake CLI on PATH" testing philosophy
draws, one layer up (no real subprocess at all, matching the precedent
already used throughout test_session_manager.py).

The `tbexec` MCP tool itself isn't in this loop — in these tests
`record_decision` is called directly from inside the scripted event
stream, standing in for "the model called the tool mid-turn, which POSTed
here" (tests/test_tbexec_router.py covers the POST route itself).
"""

from __future__ import annotations

import json

import pytest

from server.agent_manager import AgentManager
from server.agentbeats.manager import (
    TASK_COST_BUDGET_USD,
    TASK_ROUND_BUDGET,
    AgentBeatsExamManager,
    ExamTaskError,
)
from server.database import Database
from server.harness import HarnessEvent
from server.session_manager import SessionManager


@pytest.fixture
async def ctx():
    db = Database(":memory:")
    await db.initialize()
    session_mgr = SessionManager()
    await session_mgr.initialize(db)
    agent_mgr = AgentManager(db)
    exam_mgr = AgentBeatsExamManager()
    exam_mgr.bind(session_mgr=session_mgr, agent_mgr=agent_mgr)
    try:
        yield session_mgr, agent_mgr, exam_mgr
    finally:
        exam_mgr.shutdown()
        await db.close()


@pytest.mark.asyncio
async def test_full_two_round_task_lifecycle(ctx):
    session_mgr, agent_mgr, exam_mgr = ctx
    calls: list[str] = []

    # Round 1: model "calls" `run` mid-turn (we record the decision
    # ourselves, standing in for the tbexec POST), then the harness turn
    # concludes with `result`.
    def make_round1(session_holder):
        async def _gen():
            yield HarnessEvent(type="text", content="Looking around.")
            exam_mgr.record_decision(
                session_holder["id"], {"kind": "exec", "command": "ls", "timeout": 30}
            )
            yield HarnessEvent(type="result", session_id="x", cost=0.01, num_turns=1)

        return _gen

    def make_round2(session_holder):
        async def _gen():
            exam_mgr.record_decision(
                session_holder["id"], {"kind": "finish", "output": "solved it"}
            )
            yield HarnessEvent(type="result", session_id="x", cost=0.02, num_turns=1)

        return _gen

    session_holder: dict = {}
    round_no = {"n": 0}

    def factory(s, agent=None, connectors=None, **_kw):
        session_holder["id"] = s.id
        calls.append(s.id)
        round_no["n"] += 1
        gen_factory = make_round1(session_holder) if round_no["n"] == 1 else make_round2(session_holder)

        class ScriptedBackend:
            name = "scripted"

            async def start(self, prompt, working_dir, resume_id=None, credential=None):
                pass

            def stream(self):
                return gen_factory()

            async def stop(self):
                pass

        return ScriptedBackend()

    session_mgr._make_run = factory  # type: ignore[method-assign]

    task_msg = json.dumps(
        {"kind": "task", "protocol": "terminal-bench-shell-v1", "instruction": "fix it"}
    )
    reply1 = await exam_mgr.handle_message("ctx-1", task_msg)
    decoded1 = json.loads(reply1)
    assert decoded1 == {"kind": "exec_request", "command": "ls", "timeout": 30}

    # The exam agent is isolated: dedicated backend, only the tbexec MCP
    # server, no production native tools, no credential of its own.
    record = exam_mgr.get_record("ctx-1")
    assert record is not None
    agent_row = await agent_mgr.get_agent(record.agent_id)
    assert agent_row["mcp_servers"] == ["tbexec"]
    assert "Bash" in agent_row["tool_deny"]
    assert agent_row["credential_id"] is None
    assert record.round_no == 1
    assert record.cumulative_cost_usd == pytest.approx(0.01)

    exec_result_msg = json.dumps(
        {"kind": "exec_result", "exit_code": 0, "stdout": "a.txt\n", "stderr": ""}
    )
    reply2 = await exam_mgr.handle_message("ctx-1", exec_result_msg)
    decoded2 = json.loads(reply2)
    assert decoded2 == {"kind": "final", "output": "solved it"}

    # Teardown: both the throwaway agent and session are gone.
    assert exam_mgr.get_record("ctx-1").finished
    assert await agent_mgr.get_agent(record.agent_id) is None
    assert session_mgr.get_session(record.session_id) is None


@pytest.mark.asyncio
async def test_first_message_must_be_kind_task(ctx):
    _session_mgr, _agent_mgr, exam_mgr = ctx
    with pytest.raises(ExamTaskError):
        await exam_mgr.handle_message(
            "ctx-bad", json.dumps({"kind": "exec_result", "exit_code": 0})
        )


@pytest.mark.asyncio
async def test_malformed_json_raises(ctx):
    _session_mgr, _agent_mgr, exam_mgr = ctx
    with pytest.raises(ExamTaskError):
        await exam_mgr.handle_message("ctx-bad2", "not json")


@pytest.mark.asyncio
async def test_no_decision_falls_back_to_assistant_text(ctx):
    """The model ignored the protocol and just answered in plain text —
    treat that as an implicit `done` rather than dropping the task."""
    session_mgr, _agent_mgr, exam_mgr = ctx

    def factory(s, agent=None, connectors=None, **_kw):
        class ScriptedBackend:
            name = "scripted"

            async def start(self, prompt, working_dir, resume_id=None, credential=None):
                pass

            def stream(self):
                async def _gen():
                    yield HarnessEvent(type="text", content="I think it's done.")
                    yield HarnessEvent(type="result", session_id="x", cost=0.005)

                return _gen()

            async def stop(self):
                pass

        return ScriptedBackend()

    session_mgr._make_run = factory  # type: ignore[method-assign]

    reply = await exam_mgr.handle_message(
        "ctx-2",
        json.dumps({"kind": "task", "protocol": "terminal-bench-shell-v1", "instruction": "x"}),
    )
    assert json.loads(reply) == {"kind": "final", "output": "I think it's done."}


@pytest.mark.asyncio
async def test_harness_error_aborts_task(ctx):
    session_mgr, _agent_mgr, exam_mgr = ctx

    def factory(s, agent=None, connectors=None, **_kw):
        class ScriptedBackend:
            name = "scripted"

            async def start(self, prompt, working_dir, resume_id=None, credential=None):
                pass

            def stream(self):
                # A real harness failure surfaces as an exception raised
                # while consuming the stream, not a `type="error"`
                # HarnessEvent (session_manager.py's conversion function has
                # no branch for that type; `_run_backend`'s own `except
                # Exception` is what builds the `{"type":"error",...}`
                # broadcast). Match that shape here.
                async def _gen():
                    if False:
                        yield HarnessEvent(type="text", content="")
                    raise RuntimeError("boom")

                return _gen()

            async def stop(self):
                pass

        return ScriptedBackend()

    session_mgr._make_run = factory  # type: ignore[method-assign]

    reply = await exam_mgr.handle_message(
        "ctx-3",
        json.dumps({"kind": "task", "protocol": "terminal-bench-shell-v1", "instruction": "x"}),
    )
    decoded = json.loads(reply)
    assert decoded["kind"] == "final"
    assert "aborted" in decoded["output"]


@pytest.mark.asyncio
async def test_round_budget_forces_final_without_spawning_a_turn(ctx):
    session_mgr, agent_mgr, exam_mgr = ctx
    from server.agentbeats.manager import ExamTaskRecord

    agent = await agent_mgr.create_agent(name="pre-seeded-exam")
    session = await session_mgr.create_session(agent_id=agent["id"], origin="agentbeats")
    record = ExamTaskRecord(
        context_id="ctx-budget",
        agent_id=agent["id"],
        session_id=session.id,
        round_no=TASK_ROUND_BUDGET,
    )
    exam_mgr._records["ctx-budget"] = record
    exam_mgr._session_to_context[session.id] = "ctx-budget"

    def factory(*a, **k):  # noqa: ANN001, ARG001
        raise AssertionError("must not spawn a turn once the round budget is exhausted")

    session_mgr._make_run = factory  # type: ignore[method-assign]

    reply = await exam_mgr.handle_message(
        "ctx-budget",
        json.dumps({"kind": "exec_result", "exit_code": 0, "stdout": "", "stderr": ""}),
    )
    decoded = json.loads(reply)
    assert decoded["kind"] == "final"
    assert "budget exceeded" in decoded["output"]


@pytest.mark.asyncio
async def test_cost_budget_forces_final(ctx):
    session_mgr, agent_mgr, exam_mgr = ctx
    from server.agentbeats.manager import ExamTaskRecord

    agent = await agent_mgr.create_agent(name="pre-seeded-exam-2")
    session = await session_mgr.create_session(agent_id=agent["id"], origin="agentbeats")
    record = ExamTaskRecord(
        context_id="ctx-cost",
        agent_id=agent["id"],
        session_id=session.id,
        cumulative_cost_usd=TASK_COST_BUDGET_USD,
    )
    exam_mgr._records["ctx-cost"] = record
    exam_mgr._session_to_context[session.id] = "ctx-cost"

    def factory(*a, **k):  # noqa: ANN001, ARG001
        raise AssertionError("must not spawn a turn once the cost budget is exhausted")

    session_mgr._make_run = factory  # type: ignore[method-assign]

    reply = await exam_mgr.handle_message(
        "ctx-cost",
        json.dumps({"kind": "exec_result", "exit_code": 0, "stdout": "", "stderr": ""}),
    )
    decoded = json.loads(reply)
    assert decoded["kind"] == "final"
    assert "budget exceeded" in decoded["output"]


@pytest.mark.asyncio
async def test_record_decision_conflict_on_second_call(ctx):
    session_mgr, agent_mgr, exam_mgr = ctx
    from server.agentbeats.manager import ExamTaskRecord, _ExamRound

    agent = await agent_mgr.create_agent(name="pre-seeded-exam-3")
    session = await session_mgr.create_session(agent_id=agent["id"], origin="agentbeats")
    record = ExamTaskRecord(context_id="ctx-dup", agent_id=agent["id"], session_id=session.id)
    exam_mgr._records["ctx-dup"] = record
    exam_mgr._session_to_context[session.id] = "ctx-dup"
    exam_mgr._current_round["ctx-dup"] = _ExamRound()

    first = exam_mgr.record_decision(session.id, {"kind": "exec", "command": "ls", "timeout": 10})
    second = exam_mgr.record_decision(session.id, {"kind": "finish", "output": "x"})
    assert first is True
    assert second is False


@pytest.mark.asyncio
async def test_record_decision_unknown_session_returns_false(ctx):
    _session_mgr, _agent_mgr, exam_mgr = ctx
    assert exam_mgr.record_decision("no-such-session", {"kind": "finish", "output": "x"}) is False
